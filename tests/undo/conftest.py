"""Contract fakes for the gate and targets being built in other worktrees."""

from dataclasses import fields
from datetime import datetime, timedelta, timezone
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from stopline import db as stopline_db
from stopline.contract import (
    Action, Admission, Claim, EffectState, Inverse, Lookup, MandateStatus,
    Mode, NotClaimed, OPERATOR, Reason, ReceiptRow, ReceiptView, ROUTER,
    SendResult, TARGET_REGION_B, TargetTimeout, TargetUnreachable, Tool,
    args_hash, system_of,
)


class Scenario(SimpleNamespace):
    def __repr__(self):
        return f"Scenario(action_id={self.action.action_id!r})"


def as_action(effect):
    return Action(**{
        field.name: effect["_id"] if field.name == "action_id" else effect[field.name]
        for field in fields(Action)
    })


class FakeRouter:
    name = ROUTER

    def __init__(self):
        self.percent = 0
        self.log = []
        self.reachable = True
        self.lose_reply = False
        self.fenced = set()

    def send(self, action):
        if not self.reachable:
            raise TargetUnreachable()
        previous = self.lookup(action.action_id)
        if previous.found:
            return SendResult(previous.applied, previous.evidence)
        evidence = {
            "action_id": action.action_id, "system": self.name,
            "at_epoch": time.time(), "before_percent": self.percent,
            "percent": action.args["percent"],
        }
        if action.action_id in self.fenced:
            return SendResult(False, evidence, Reason.FENCED)
        self.percent = action.args["percent"]
        self.log.append(evidence)
        if self.lose_reply:
            self.lose_reply = False
            self.reachable = False
            raise TargetTimeout()
        return SendResult(True, evidence)

    def lookup(self, action_id):
        if not self.reachable:
            raise TargetUnreachable()
        evidence = next((row for row in self.log if row["action_id"] == action_id), None)
        return Lookup(evidence is not None, evidence is not None, evidence or {})

    def fence(self, action_id):
        if not self.reachable:
            raise TargetUnreachable()
        self.fenced.add(action_id)
        return {"action_id": action_id, "system": self.name, "at_epoch": time.time()}

    def state(self):
        return {
            "system": self.name, "reachable": self.reachable,
            "weights": {"region-a": 100 - self.percent, "region-b": self.percent},
            "last_action_id": self.log[-1]["action_id"] if self.log else None,
            "log": list(self.log),
        }

    def inverse_of(self, action):
        if action.tool != Tool.SHIFT:
            return None
        evidence = self.lookup(action.action_id).evidence
        if not evidence:
            return None
        return Inverse(
            Tool.SHIFT, action.target, {"percent": evidence["before_percent"]},
            {"weights": {"region-b": evidence["percent"]}, "last_action_id": action.action_id},
        )

    def cut(self):
        self.lose_reply = True

    def restore(self):
        self.reachable = True
        self.lose_reply = False


class FakeCluster:
    name = "cluster"

    def inverse_of(self, action):
        return None


class FakeGate:
    """Small persistence fake; it does not implement gate transactions or policy."""

    def __init__(self, db):
        self.db = db
        self.admissions = []
        self.refuse = None

    def admit(self, action):
        self.admissions.append(action)
        if self.refuse is not None:
            return Admission(False, action.action_id, self.refuse)
        mandate = self.db.mandates.find_one({"_id": action.mandate_id})
        if mandate["status"] != MandateStatus.ACTIVE:
            return Admission(False, action.action_id, Reason.REVOKED)
        previous = self.db.effects.find_one({"_id": action.action_id})
        if previous:
            same = as_action(previous) == action
            return Admission(same, action.action_id,
                             Reason.OK if same else Reason.SAME_ID_DIFFERENT_ARGS,
                             previous["admitted_seq"], replayed=same)
        root = self.db.mandates.find_one_and_update(
            {"_id": mandate["root_id"]},
            [{"$set": {"seq": {"$add": ["$seq", 1]}, "clock": "$$NOW"}}],
            return_document=True,
        )
        chain = [mandate["_id"]]
        parent_id = mandate["parent_id"]
        while parent_id:
            chain.insert(0, parent_id)
            parent_id = self.db.mandates.find_one({"_id": parent_id})["parent_id"]
        self.db.effects.insert_one({
            "_id": action.action_id, "run_id": action.run_id, "mode": Mode.STOPLINE,
            "root_id": root["_id"], "mandate_id": action.mandate_id, "chain": chain,
            "principal": action.principal, "tool": action.tool, "target": action.target,
            "args": action.args, "args_hash": args_hash(action.args),
            "admitted_seq": root["seq"], "state": EffectState.ADMITTED,
            "evidence": None, "reverses": action.reverses, "undo": None,
            "policy_version": 1, "policy_hash": "test-policy", "claimed_by": None,
            "admitted_at": root["clock"], "sent_at": None, "finished_at": None,
        })
        return Admission(True, action.action_id, Reason.OK, root["seq"])

    def claim(self, action_id, by):
        effect = self.db.effects.find_one_and_update(
            {"_id": action_id, "state": EffectState.ADMITTED},
            {"$set": {"state": EffectState.SENT, "claimed_by": by}, "$currentDate": {"sent_at": True}},
            return_document=True,
        )
        return Claim(effect is not None, action_id,
                     effect["state"] if effect else EffectState.CANCELLED)

    def record_outcome(self, action_id, state, evidence):
        return self.db.effects.find_one_and_update(
            {"_id": action_id, "state": EffectState.SENT},
            {"$set": {"state": state, "evidence": evidence}, "$currentDate": {"finished_at": True}},
            return_document=True,
        )

    def receipt(self, receipt_id):
        receipt = self.db.receipts.find_one({"_id": receipt_id})
        rows = [ReceiptRow(
            action_id=effect["_id"], principal=effect["principal"], tool=effect["tool"],
            target=effect["target"], args=effect["args"], outcome="completed",
            state=effect["state"], reason=Reason.OK, chain=[],
            evidence=effect["evidence"], undo=effect["undo"],
        ) for effect in self.db.effects.find({"_id": {"$in": receipt["covered"]}})]
        return ReceiptView(receipt, rows, receipt["status"] == "open")


class FakeSender:
    def __init__(self, gate, targets):
        self.gate, self.targets = gate, targets

    def claim(self, action_id):
        return self.gate.claim(action_id, "test-sender")

    def send(self, action_id):
        effect = self.gate.db.effects.find_one({"_id": action_id})
        if effect["state"] != EffectState.SENT or effect["claimed_by"] != "test-sender":
            raise NotClaimed(action_id)
        try:
            result = self.targets[system_of(effect["target"])].send(as_action(effect))
        except (TargetTimeout, TargetUnreachable):
            return effect
        return self.gate.record_outcome(
            action_id, EffectState.APPLIED if result.applied else EffectState.REJECTED, result.evidence,
        )

    def reconcile(self, action_id):
        effect = self.gate.db.effects.find_one({"_id": action_id})
        if effect["state"] != EffectState.SENT:
            return effect
        target = self.targets[system_of(effect["target"])]
        try:
            found = target.lookup(action_id)
            evidence = found.evidence if found.found else target.fence(action_id)
        except TargetUnreachable:
            return effect
        return self.gate.record_outcome(
            action_id, EffectState.APPLIED if found.applied else EffectState.REJECTED, evidence,
        )


@pytest.fixture(scope="module")
def db():
    database = stopline_db.test_database("undo")
    try:
        database.policies.insert_one({
            "_id": 1, "text": "test policy", "hash": "test-policy",
            "created_at": datetime.now(timezone.utc),
        })
        yield database
    finally:
        stopline_db.drop_test_database(database)


@pytest.fixture
def scenario(db):
    run_id = uuid4().hex
    root, repair, worker = (f"{run_id}-{suffix}" for suffix in ("root", "repair", "worker"))
    now = datetime.now(timezone.utc)
    mandates = []
    for depth, (mid, parent, holder) in enumerate([
        (root, None, OPERATOR), (repair, root, "agent:repair"), (worker, repair, "agent:worker-2"),
    ]):
        mandates.append({
            "_id": mid, "run_id": run_id, "root_id": root, "parent_id": parent,
            "depth": depth, "grantor": OPERATOR if depth < 2 else "agent:repair",
            "holder": holder, "tools": list(Tool), "targets": [TARGET_REGION_B, "cluster/checkout-worker"],
            "limits": {"max_actions": 20, "max_shift_percent": 100},
            "expires_at": now + timedelta(hours=1), "status": MandateStatus.ACTIVE,
            "revoked_by": None, "revoked_at_seq": None, "revoked_at": None, "created_at": now,
            **({"seq": 0} if depth == 0 else {}),
        })
    db.mandates.insert_many(mandates)
    gate, router = FakeGate(db), FakeRouter()
    targets = {ROUTER: router, "cluster": FakeCluster()}
    sender = FakeSender(gate, targets)
    action = Action(f"{run_id}-shift", run_id, worker, "agent:worker-2",
                    Tool.SHIFT, TARGET_REGION_B, {"percent": 25})
    assert gate.admit(action).allowed
    assert sender.claim(action.action_id).claimed
    assert sender.send(action.action_id)["state"] == EffectState.APPLIED
    db.mandates.update_many({"_id": {"$in": [repair, worker]}}, {
        "$set": {"status": MandateStatus.REVOKED, "revoked_by": OPERATOR, "revoked_at_seq": 2},
        "$currentDate": {"revoked_at": True},
    })
    receipt_id = f"{run_id}-receipt"
    db.receipts.insert_one({
        "_id": receipt_id, "run_id": run_id, "root_id": root, "mandate_id": repair,
        "stopped_by": OPERATOR, "stop_seq": 2, "stopped_at": now,
        "revoked": [repair, worker], "covered": [action.action_id],
        "status": "closed", "closed_at": now,
    })
    return Scenario(db=db, gate=gate, router=router, targets=targets, sender=sender,
                    action=action, root=root, repair=repair, worker=worker, receipt_id=receipt_id)
