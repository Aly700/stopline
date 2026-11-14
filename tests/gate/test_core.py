from concurrent.futures import ThreadPoolExecutor
import time

import pytest
from pymongo.collection import Collection
from pymongo.errors import WriteError

from stopline.contract import EffectState, Lookup, NotAuthorised, OPERATOR, Reason, TargetTimeout, TargetUnreachable
from conftest import Pause, action, call, child, root


def evidence(action_id, **extra):
    return {"action_id": action_id, "system": "router", "at_epoch": time.time(), **extra}


@pytest.mark.parametrize("winner", ["stop", "admit"])
def test_1_admit_and_stop_same_mandate(gate, run_id, monkeypatch, winner):
    mandate = root(gate, run_id)
    proposed = action(mandate)
    pause = Pause()
    original = Collection.find_one
    armed = True

    def hold_snapshot(collection, query=None, *args, **kwargs):
        nonlocal armed
        result = original(collection, query, *args, **kwargs)
        if (armed and collection.full_name == gate.db.mandates.full_name
                and query == {"_id": mandate["_id"]} and kwargs.get("session")):
            armed = False
            pause.hold()
        return result

    monkeypatch.setattr(Collection, "find_one", hold_snapshot)
    with ThreadPoolExecutor(1) as pool:
        loser = pool.submit(call, "admit" if winner == "stop" else "stop",
                            gate.admit if winner == "stop" else lambda: gate.stop(mandate_id=mandate["_id"], by=OPERATOR),
                            *([proposed] if winner == "stop" else []))
        try:
            pause.wait()
            if winner == "stop":
                receipt = call("stop", gate.stop, mandate_id=mandate["_id"], by=OPERATOR)
            else:
                admission = call("admit", gate.admit, proposed)
        finally:
            pause.release.set()
        result = loser.result(timeout=20)
    if winner == "stop":
        admission = result
        assert not admission.allowed and admission.reason == Reason.REVOKED
        assert admission.attempts == 2
    else:
        receipt = result
        assert admission.allowed
        assert proposed.action_id in receipt["covered"]
    assert not admission.allowed or proposed.action_id in receipt["covered"]


@pytest.mark.parametrize("winner", ["stop", "claim"])
def test_4_stop_and_claim(gate, run_id, monkeypatch, winner):
    mandate = root(gate, run_id)
    proposed = action(mandate)
    assert call("admit", gate.admit, proposed).allowed
    pause = Pause()
    if winner == "stop":
        original = Collection.find_one_and_update

        def held_claim(collection, query, *args, **kwargs):
            if collection.full_name == gate.db.effects.full_name and query.get("state") == "admitted":
                pause.hold()
            return original(collection, query, *args, **kwargs)

        monkeypatch.setattr(Collection, "find_one_and_update", held_claim)
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(call, "claim", gate.claim, proposed.action_id, "sender")
            try:
                pause.wait()
                receipt = call("stop", gate.stop, mandate_id=mandate["_id"], by=OPERATOR)
            finally:
                pause.release.set()
            claim = pending.result(timeout=20)
        assert not claim.claimed and claim.state == EffectState.CANCELLED
    else:
        original = Collection.update_many
        armed = True

        def held_stop(collection, query, *args, **kwargs):
            nonlocal armed
            if armed and collection.full_name == gate.db.effects.full_name:
                armed = False
                pause.hold()
            return original(collection, query, *args, **kwargs)

        monkeypatch.setattr(Collection, "update_many", held_stop)
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(call, "stop", gate.stop, mandate_id=mandate["_id"], by=OPERATOR)
            try:
                pause.wait()
                claim = call("claim", gate.claim, proposed.action_id, "sender")
            finally:
                pause.release.set()
            receipt = pending.result(timeout=20)
        assert claim.claimed
        assert gate.db.effects.find_one({"_id": proposed.action_id})["state"] == EffectState.SENT
        assert receipt["status"] == "open"
    assert proposed.action_id in receipt["covered"]
    assert not call("claim_after_stop", gate.claim, proposed.action_id, "sender").claimed


def test_8_validator_and_aborted_deny(gate, run_id):
    mandate = root(gate, run_id)
    before = gate.db.mandates.find_one({"_id": mandate["_id"]})["seq"]
    proposed = action(mandate, tool="unpermitted")
    denied = call("admit_denied", gate.admit, proposed)
    assert not denied.allowed and denied.reason == Reason.TOOL_NOT_ALLOWED
    assert gate.db.effects.find_one({"_id": proposed.action_id}) is None
    assert gate.db.mandates.find_one({"_id": mandate["_id"]})["seq"] == before
    entry = gate.db.audit.find_one({"action_id": proposed.action_id, "kind": "deny"})
    assert entry and entry["reason"] == Reason.TOOL_NOT_ALLOWED
    for bad in ({"kind": "allow"}, {**entry, "_id": "bad-enum", "kind": "maybe"},
                {**entry, "_id": "bad-reason", "reason": "maybe"},
                {**entry, "_id": "bad-date", "at": 1}):
        with pytest.raises(WriteError) as rejected:
            call("malformed_audit", gate.db.audit.insert_one, bad)
        assert rejected.value.code == 121
    call("setup_repeat", gate.setup)
    assert gate.db.policies.count_documents({}) == 1


class Target:
    name = "router"

    def __init__(self, *, applied=False, unavailable=False):
        self.applied = applied
        self.unavailable = unavailable
        self.fenced = set()

    def lookup(self, action_id):
        if self.unavailable:
            raise TargetUnreachable()
        return Lookup(self.applied or action_id in self.fenced, self.applied, evidence(action_id))

    def fence(self, action_id):
        self.fenced.add(action_id)
        return evidence(action_id, fenced=True)


def test_9_receipt_closes_only_after_all_terminal(gate, run_id):
    parent = root(gate, run_id)
    mandate = child(gate, parent)
    actions = [action(mandate, str(i)) for i in range(3)]
    for proposed in actions:
        assert call("admit", gate.admit, proposed).allowed
    for proposed in actions[:2]:
        assert call("claim", gate.claim, proposed.action_id, "sender").claimed
    receipt = call("stop", gate.stop, mandate_id=mandate["_id"], by=OPERATOR)
    assert set(receipt["covered"]) == {a.action_id for a in actions}
    assert receipt["status"] == "open"
    call("record_outcome", gate.record_outcome, actions[0].action_id, "applied", evidence(actions[0].action_id))
    assert call("receipt", gate.receipt, receipt["_id"]).open
    target = Target(unavailable=True)
    unknown = call("reconcile_unreachable", gate.reconcile, actions[1].action_id, target)
    assert unknown["state"] == "sent"
    assert call("receipt", gate.receipt, receipt["_id"]).open
    target.unavailable = False
    resolved = call("reconcile_fence", gate.reconcile, actions[1].action_id, target)
    assert resolved["state"] == "rejected" and actions[1].action_id in target.fenced
    view = call("receipt", gate.receipt, receipt["_id"])
    assert not view.open and view.receipt["closed_at"] is not None
    assert [row.state for row in view.rows] == ["applied", "rejected", "cancelled"]
    refused = action(mandate, "after-stop")
    assert not call("admit_denied", gate.admit, refused).allowed
    after = call("receipt", gate.receipt, receipt["_id"])
    assert after.receipt["covered"] == receipt["covered"]
    assert after.rows[-1].state is None and after.rows[-1].outcome == "blocked"
    chain = call("chain", gate.chain, actions[0].action_id)
    assert [m["_id"] for m in chain.mandates] == [parent["_id"], mandate["_id"]]
    assert chain.policy and {row["kind"] for row in chain.audit} == {"allow", "claim", "outcome"}


def test_identity_and_stop_authorisation(gate, run_id):
    parent = root(gate, run_id)
    mandate = child(gate, parent)
    proposed = action(mandate)
    first = call("admit", gate.admit, proposed)
    again = call("admit_replay", gate.admit, proposed)
    assert first.allowed and again.allowed and again.replayed and first.seq == again.seq
    changed = call("admit_changed", gate.admit, action(mandate, args={"percent": 30}))
    assert not changed.allowed and changed.reason == Reason.SAME_ID_DIFFERENT_ARGS
    with pytest.raises(NotAuthorised):
        call("stop_unauthorised", gate.stop, mandate_id=mandate["_id"], by="stranger")
    receipt = call("stop", gate.stop, mandate_id=mandate["_id"], by=OPERATOR)
    assert receipt["status"] == "closed"
    assert call("admit_replay_after_stop", gate.admit, proposed).replayed
    assert not call("claim", gate.claim, proposed.action_id, "sender").claimed
