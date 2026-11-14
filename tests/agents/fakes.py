"""Small shared-memory fakes, usable from spawned worker processes.

These exercise orchestration, not MongoDB transactions or target adapters.
"""

from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
import time
import uuid

from pymongo.errors import DuplicateKeyError, WriteError

from stopline.contract import (
    Admission, Claim, EffectState, HandDown, Lookup, Reason, SendResult,
    TargetTimeout, TargetUnreachable, args_hash, system_of,
)


def now():
    return datetime.now(timezone.utc)


def matches(doc, query):
    for key, value in query.items():
        got = doc.get(key)
        if isinstance(value, dict):
            if "$in" in value and got not in value["$in"]:
                return False
            if "$ne" in value and got == value["$ne"]:
                return False
        elif got != value:
            return False
    return True


class Collection:
    def __init__(self, db, name):
        self.db, self.name = db, name

    def find(self, query=None, *args, **kwargs):
        with self.db.lock:
            return [deepcopy(v) for (collection, _), v in self.db.data.items()
                    if collection == self.name and matches(v, query or {})]

    def find_one(self, query=None, *args, **kwargs):
        return next(iter(self.find(query)), None)

    def insert_one(self, doc, **kwargs):
        with self.db.lock:
            if self.name == "audit" and not all(k in doc for k in ("kind", "principal", "at")):
                raise WriteError("Document failed validation", 121)
            doc = deepcopy(doc)
            doc.setdefault("_id", uuid.uuid4().hex)
            key = self.name, doc["_id"]
            if key in self.db.data:
                raise DuplicateKeyError("duplicate")
            self.db.data[key] = doc
            return SimpleNamespace(inserted_id=doc["_id"])

    def insert_many(self, docs):
        for doc in docs:
            self.insert_one(doc)

    def update_one(self, query, update, upsert=False, **kwargs):
        with self.db.lock:
            doc = self.find_one(query)
            existed = doc is not None
            if not existed and not upsert:
                return SimpleNamespace(modified_count=0, matched_count=0)
            if doc is None:
                doc = deepcopy(query)
                doc.update(deepcopy(update.get("$setOnInsert", {})))
            doc.update(deepcopy(update.get("$set", {})))
            for key, value in update.get("$inc", {}).items():
                doc[key] = doc.get(key, 0) + value
            for key in update.get("$currentDate", {}):
                doc[key] = now()
            self.db.data[self.name, doc["_id"]] = doc
            return SimpleNamespace(modified_count=1, matched_count=int(existed))

    def update_many(self, query, update):
        with self.db.lock:
            for doc in self.find(query):
                self.update_one({"_id": doc["_id"]}, update)

    def delete_one(self, query):
        with self.db.lock:
            doc = self.find_one(query)
            if doc:
                del self.db.data[self.name, doc["_id"]]

    def watch(self, pipeline, **kwargs):
        run_id = pipeline[0]["$match"]["documentKey._id"]
        return Stream(self, run_id)


class Stream:
    def __init__(self, collection, run_id):
        self.collection, self.run_id = collection, run_id
        self.previous = collection.find_one({"_id": run_id})

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def try_next(self):
        time.sleep(0.02)
        current = self.collection.find_one({"_id": self.run_id})
        if current != self.previous:
            self.previous = current
            return {"fullDocument": current}


class Database:
    name = "stopline_test_agents_fake"

    def __init__(self, data, lock):
        self.data, self.lock = data, lock

    def __getitem__(self, name):
        return Collection(self, name)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self[name]


class Gate:
    def __init__(self, db):
        self.db = db

    def grant_root(self, *, run_id, mandate_id=None, **scope):
        mid = mandate_id or uuid.uuid4().hex
        root = dict(_id=mid, root_id=mid, parent_id=None, run_id=run_id,
                    status="active", seq=0, **scope)
        self.db.mandates.insert_one(root)
        return root

    def hand_down(self, *, parent_id, mandate_id=None, **scope):
        parent = self.db.mandates.find_one({"_id": parent_id})
        mid = mandate_id or uuid.uuid4().hex
        self.db.mandates.insert_one(dict(_id=mid, root_id=parent["root_id"],
                                        parent_id=parent_id, run_id=parent["run_id"],
                                        status="active", **scope))
        return HandDown(True, mid, Reason.OK)

    def admit(self, action):
        with self.db.lock:
            mandate = self.db.mandates.find_one({"_id": action.mandate_id})
            if mandate["status"] == "revoked":
                detail = {key: mandate[key] for key in ("revoked_by", "revoked_at", "revoked_at_seq")}
                self.db.audit.insert_one(dict(kind="deny", principal=action.principal,
                                              at=now(), action_id=action.action_id, detail=detail))
                return Admission(False, action.action_id, Reason.REVOKED, detail=detail)
            if not self.db.effects.find_one({"_id": action.action_id}):
                chain = [mandate["root_id"], mandate["parent_id"], mandate["_id"]]
                self.db.effects.insert_one(dict(
                    _id=action.action_id, run_id=action.run_id, mode="stopline",
                    mandate_id=action.mandate_id, root_id=mandate["root_id"],
                    principal=action.principal, tool=action.tool, target=action.target,
                    args=action.args, args_hash=args_hash(action.args), chain=chain,
                    admitted_seq=1, state="admitted", evidence=None,
                ))
            return Admission(True, action.action_id, Reason.OK)

    def claim(self, action_id, by):
        with self.db.lock:
            effect = self.db.effects.find_one({"_id": action_id})
            if effect["state"] != "admitted":
                mandate = self.db.mandates.find_one({"_id": effect["mandate_id"]})
                detail = {k: mandate.get(k) for k in ("revoked_by", "revoked_at", "revoked_at_seq")}
                return Claim(False, action_id, effect["state"], Reason.CANCELLED_BY_STOP, detail)
            self.db.effects.update_one({"_id": action_id}, {"$set": {"state": "sent", "claimed_by": by}})
            return Claim(True, action_id, "sent")

    def stop(self, *, mandate_id, by):
        with self.db.lock:
            agent = self.db.mandates.find_one({"_id": mandate_id})
            at = now()
            revoked = []
            for mandate in self.db.mandates.find({"run_id": agent["run_id"]}):
                if mandate["_id"] != agent["root_id"]:
                    revoked.append(mandate["_id"])
                    self.db.mandates.update_one({"_id": mandate["_id"]}, {"$set": {
                        "status": "revoked", "revoked_by": by, "revoked_at": at, "revoked_at_seq": 9,
                    }})
            effects = self.db.effects.find({"run_id": agent["run_id"]})
            for effect in effects:
                if effect["state"] == EffectState.ADMITTED:
                    self.db.effects.update_one({"_id": effect["_id"]}, {"$set": {"state": "cancelled"}})
            receipt = dict(_id=uuid.uuid4().hex, run_id=agent["run_id"], mandate_id=mandate_id,
                           root_id=agent["root_id"], stopped_by=by, stopped_at=at,
                           stop_seq=9, covered=[e["_id"] for e in effects], revoked=revoked,
                           status="open", closed_at=None)
            self.db.receipts.insert_one(receipt)
            return receipt

    def record_outcome(self, action_id, state, evidence):
        self.db.effects.update_one({"_id": action_id}, {"$set": {"state": state, "evidence": evidence}})
        return self.db.effects.find_one({"_id": action_id})


class Target:
    def __init__(self, db, name):
        self.db, self.name = db, name
        self.db.target_state.update_one({"_id": name}, {"$setOnInsert": {
            "reachable": True, "armed": False, "last_action_id": None,
        }}, upsert=True)

    def send(self, action):
        with self.db.lock:
            state = self.db.target_state.find_one({"_id": self.name})
            if not state["reachable"]:
                raise TargetUnreachable()
            existing = self.db.target_log.find_one({"_id": action.action_id})
            if existing:
                return SendResult(True, existing)
            evidence = dict(_id=action.action_id, action_id=action.action_id,
                            system=self.name, at_epoch=time.time(), args=action.args,
                            tool=action.tool, target=action.target)
            self.db.target_log.insert_one(evidence)
            self.db.target_state.update_one({"_id": self.name}, {"$set": {
                "last_action_id": action.action_id, "reachable": not state["armed"],
            }})
            if state["armed"]:
                raise TargetTimeout()
            return SendResult(True, evidence)

    def state(self):
        return {**self.db.target_state.find_one({"_id": self.name}),
                "log": self.db.target_log.find({"system": self.name})}

    def cut(self):
        self.db.target_state.update_one({"_id": self.name}, {"$set": {"armed": True}})

    def restore(self):
        self.db.target_state.update_one({"_id": self.name}, {"$set": {"armed": False, "reachable": True}})

    def lookup(self, action_id):
        evidence = self.db.target_log.find_one({"_id": action_id})
        return Lookup(bool(evidence), bool(evidence), evidence or {})


class Sender:
    def __init__(self, db, gate, targets):
        self.db, self.gate, self.targets = db, gate, targets

    def claim(self, action_id):
        return self.gate.claim(action_id, "sender")

    def send(self, action_id):
        from stopline.contract import Action
        self.db.send_calls.insert_one({"action_id": action_id})
        effect = self.db.effects.find_one({"_id": action_id})
        assert effect["state"] == "sent"
        action = Action(action_id, effect["run_id"], effect["mandate_id"],
                        effect["principal"], effect["tool"], effect["target"], effect["args"])
        try:
            answer = self.targets[system_of(effect["target"])].send(action)
        except (TargetTimeout, TargetUnreachable):
            # Keep the request in flight long enough to prove a real process kill.
            time.sleep(5)
            return effect
        return self.gate.record_outcome(action_id, "applied" if answer.applied else "rejected", answer.evidence)

    def sweep(self, action_ids):
        return []   # this fake world has no fences; the real sweep is covered end to end

    def reconcile(self, action_id):
        effect = self.db.effects.find_one({"_id": action_id})
        found = self.targets[system_of(effect["target"])].lookup(action_id)
        return self.gate.record_outcome(action_id, "applied" if found.applied else "rejected", found.evidence)
