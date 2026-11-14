"""Authority decisions and stop accounting on one MongoDB replica set.

All authority reads are inside retried callbacks. The root's seq is the shared
write conflict point for admission, delegation and stop. Outcome transactions
also write its clock (without consuming a sequence) so receipt creation and
closure cannot miss each other from separate snapshots.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any, Mapping
from uuid import uuid4

from pymongo import IndexModel, ReturnDocument
from pymongo.errors import CollectionInvalid, DuplicateKeyError

from stopline import db as storage
from stopline.contract import (
    Action, Admission, AuditKind, ChainView, Claim, EffectState, HandDown, Limits,
    MAX_DEPTH, MAX_TREE_SIZE, MandateStatus, Mode, NotAuthorised, NotClaimed,
    Reason, ReceiptRow, ReceiptView, TargetAdapter, TargetTimeout, TargetUnreachable,
    TERMINAL_STATES, Tool, args_hash, system_of,
)
from .schema import AUDIT_VALIDATOR, POLICY_HASH, POLICY_TEXT, POLICY_VERSION

TICK = [{"$set": {"seq": {"$add": ["$seq", 1]}, "clock": "$$NOW"}}]


@dataclass
class Context:
    mandate: dict
    root: dict
    tree: list[dict]
    chain: list[dict]


class Refused(Exception):
    def __init__(self, reason, context=None, detail=None):
        self.reason = reason
        self.context = context
        self.detail = detail or {}
        super().__init__(reason)


class Gate:
    def __init__(self, db):
        self.db = db

    def setup(self) -> None:
        names = set(self.db.list_collection_names())
        for name in ("principals", "mandates", "policies", "effects", "audit", "receipts", "tasks", "runs"):
            if name not in names:
                try:
                    options = {"validator": AUDIT_VALIDATOR, "validationLevel": "strict", "validationAction": "error"} if name == "audit" else {}
                    self.db.create_collection(name, **options)
                except CollectionInvalid:
                    pass  # another process completed the same setup
        self.db.command("collMod", "audit", validator=AUDIT_VALIDATOR,
                        validationLevel="strict", validationAction="error")
        indexes = {
            "mandates": [[("root_id", 1)], [("parent_id", 1)], [("run_id", 1)]],
            "effects": [[("root_id", 1), ("admitted_seq", 1)], [("chain", 1)],
                        [("mandate_id", 1), ("state", 1)], [("run_id", 1), ("admitted_at", 1)]],
            "audit": [[("action_id", 1), ("at", 1)], [("run_id", 1), ("at", 1)],
                      [("mandate_id", 1), ("kind", 1), ("at", 1)]],
            "receipts": [[("covered", 1), ("status", 1)], [("run_id", 1)]],
        }
        for collection, specs in indexes.items():
            self.db[collection].create_indexes([IndexModel(spec) for spec in specs])
        self.db.policies.update_one({"_id": POLICY_VERSION}, {"$setOnInsert": {
            "text": POLICY_TEXT, "hash": POLICY_HASH, "created_at": self._now(),
        }}, upsert=True)

    def _now(self):
        return self.db.command("hello")["localTime"]

    @staticmethod
    def _valid_limits(limits):
        return (set(limits) == {"max_actions", "max_shift_percent"}
                and all(type(v) is int for v in limits.values())
                and limits["max_actions"] >= 0 and 0 <= limits["max_shift_percent"] <= 100)

    @staticmethod
    def _mandate(*, mandate_id, run_id, root_id, parent_id, depth, grantor, holder,
                 tools, targets, limits, expires_at, now):
        return {"_id": mandate_id, "run_id": run_id, "root_id": root_id, "parent_id": parent_id,
                "depth": depth, "grantor": grantor, "holder": holder, "tools": list(tools),
                "targets": list(targets), "limits": dict(limits), "expires_at": expires_at,
                "status": MandateStatus.ACTIVE, "revoked_by": None, "revoked_at_seq": None,
                "revoked_at": None, "created_at": now}

    def grant_root(self, *, run_id: str, grantor: str, holder: str, tools: list[str],
                   targets: list[str], limits: Limits, ttl_seconds: int,
                   mandate_id: str | None = None):
        if not self._valid_limits(limits) or type(ttl_seconds) is not int or ttl_seconds <= 0:
            raise ValueError("invalid root limits or lifetime")
        now = self._now()
        identity = mandate_id or uuid4().hex
        mandate = self._mandate(mandate_id=identity, run_id=run_id, root_id=identity,
                                parent_id=None, depth=0, grantor=grantor, holder=holder,
                                tools=tools, targets=targets, limits=limits,
                                expires_at=now + timedelta(seconds=ttl_seconds), now=now)
        mandate.update(seq=0, clock=now)
        self.db.mandates.insert_one(mandate)
        return mandate

    def _context(self, session, mandate_id):
        # This first read is repeated on retry; no cached authority is trusted.
        mandate = self.db.mandates.find_one({"_id": mandate_id}, session=session)
        if mandate is None:
            raise Refused(Reason.UNKNOWN_MANDATE)
        root = self.db.mandates.find_one_and_update(
            {"_id": mandate["root_id"]}, TICK, return_document=ReturnDocument.AFTER, session=session)
        if root is None:
            raise Refused(Reason.UNKNOWN_MANDATE)
        tree = list(self.db.mandates.find({"root_id": root["_id"]}, session=session).limit(MAX_TREE_SIZE + 1))
        context = Context(mandate, root, tree, [])
        if len(tree) > MAX_TREE_SIZE:
            raise Refused(Reason.TREE_TOO_LARGE, context)
        by_id = {item["_id"]: item for item in tree}
        current = mandate
        visited = set()
        while current is not None:
            if current["_id"] in visited or len(visited) > MAX_DEPTH:
                raise Refused(Reason.TREE_TOO_LARGE, context)
            visited.add(current["_id"])
            context.chain.append(current)
            if current["parent_id"] is None:
                break
            current = by_id.get(current["parent_id"])
        context.chain.reverse()
        if current is None or context.chain[0]["_id"] != root["_id"]:
            raise Refused(Reason.UNKNOWN_MANDATE, context)
        return context

    @staticmethod
    def _check_chain(context):
        for mandate in context.chain:
            if mandate["status"] != MandateStatus.ACTIVE:
                raise Refused(Reason.REVOKED, context, {key: mandate[key] for key in
                              ("revoked_by", "revoked_at", "revoked_at_seq")})
            if mandate["expires_at"] <= context.root["clock"]:
                raise Refused(Reason.EXPIRED, context, {"expired_mandate": mandate["_id"]})

    @staticmethod
    def _audit(*, run_id, kind, principal, mandate_id, root_id, action_id=None,
               reason=Reason.OK, seq=None, at, detail=None):
        return {"run_id": run_id, "kind": kind, "principal": principal, "mandate_id": mandate_id,
                "root_id": root_id, "action_id": action_id, "policy_version": POLICY_VERSION,
                "policy_hash": POLICY_HASH, "reason": reason, "seq": seq, "at": at,
                "detail": detail or {}}

    def _deny(self, refusal, *, run_id, principal, mandate_id, action_id=None, detail=None):
        context = refusal.context
        root = context.root if context else {}
        detail = {**(detail or {}), **refusal.detail}
        if context:
            detail["chain"] = [m["_id"] for m in context.chain]
        entry = self._audit(run_id=run_id, kind=AuditKind.DENY, principal=principal,
                            mandate_id=mandate_id, root_id=root.get("_id"), action_id=action_id,
                            reason=refusal.reason, seq=root.get("seq"),
                            at=root.get("clock") or self._now(), detail=detail)
        # Called only after with_transaction has aborted the rejected decision.
        self.db.audit.insert_one(entry)
        return entry

    def hand_down(self, *, parent_id: str, holder: str, tools: list[str], targets: list[str],
                  limits: Limits, ttl_seconds: int, mandate_id: str | None = None) -> HandDown:
        identity = mandate_id or uuid4().hex
        attempts = 0

        def delegate(session):
            nonlocal attempts
            attempts += 1
            context = self._context(session, parent_id)
            self._check_chain(context)
            parent = context.mandate
            if len(context.tree) >= MAX_TREE_SIZE or parent["depth"] >= MAX_DEPTH:
                raise Refused(Reason.TREE_TOO_LARGE, context)
            if (not set(tools) <= set(parent["tools"]) or not set(targets) <= set(parent["targets"])
                    or not self._valid_limits(limits)
                    or any(limits[k] > parent["limits"][k] for k in limits)
                    or type(ttl_seconds) is not int or ttl_seconds <= 0):
                raise Refused(Reason.WIDER_THAN_PARENT, context)
            allocated = sum(m["limits"]["max_actions"] for m in context.tree if m["parent_id"] == parent_id)
            if allocated + limits["max_actions"] > parent["limits"]["max_actions"]:
                raise Refused(Reason.LIMIT_EXCEEDED, context)
            now = context.root["clock"]
            child = self._mandate(mandate_id=identity, run_id=parent["run_id"], root_id=parent["root_id"],
                                  parent_id=parent_id, depth=parent["depth"] + 1, grantor=parent["holder"],
                                  holder=holder, tools=tools, targets=targets, limits=limits,
                                  expires_at=min(parent["expires_at"], now + timedelta(seconds=ttl_seconds)), now=now)
            self.db.mandates.insert_one(child, session=session)
            self.db.audit.insert_one(self._audit(
                run_id=parent["run_id"], kind=AuditKind.HAND_DOWN, principal=parent["holder"],
                mandate_id=identity, root_id=parent["root_id"], seq=context.root["seq"], at=now,
                detail={"parent_id": parent_id, "holder": holder}), session=session)
            return HandDown(True, identity, Reason.OK, context.root["seq"])

        try:
            result, attempts = storage.run_transaction(delegate)
            return replace(result, attempts=attempts)
        except Refused as refusal:
            parent = refusal.context.mandate if refusal.context else {}
            entry = self._deny(refusal, run_id=parent.get("run_id", ""), principal=parent.get("holder", ""),
                               mandate_id=parent_id, detail={"holder": holder})
            return HandDown(False, None, refusal.reason, entry["seq"], refusal.detail, attempts)

    @staticmethod
    def _same_action(effect, action):
        return (all(effect[key] == getattr(action, key) for key in
                    ("run_id", "mandate_id", "principal", "tool", "target", "reverses"))
                and effect["args_hash"] == args_hash(action.args))

    def admit(self, action: Action) -> Admission:
        attempts = 0

        def admission(session):
            nonlocal attempts
            attempts += 1
            existing = self.db.effects.find_one({"_id": action.action_id}, session=session)
            if existing is not None:
                if not self._same_action(existing, action):
                    raise Refused(Reason.SAME_ID_DIFFERENT_ARGS)
                return Admission(True, action.action_id, Reason.OK, existing["admitted_seq"], replayed=True)
            context = self._context(session, action.mandate_id)
            self._check_chain(context)
            mandate, root = context.mandate, context.root
            if mandate["holder"] != action.principal or mandate["run_id"] != action.run_id:
                raise Refused(Reason.NOT_AUTHORISED, context)
            for ancestor in context.chain:
                if action.tool not in ancestor["tools"]:
                    raise Refused(Reason.TOOL_NOT_ALLOWED, context)
                if action.target not in ancestor["targets"]:
                    raise Refused(Reason.TARGET_NOT_ALLOWED, context)
                if action.tool == Tool.SHIFT:
                    percent = action.args.get("percent")
                    if type(percent) is not int or not 0 <= percent <= ancestor["limits"]["max_shift_percent"]:
                        raise Refused(Reason.LIMIT_EXCEEDED, context)
            chain = [m["_id"] for m in context.chain]
            counts = {row["_id"]: row["count"] for row in self.db.effects.aggregate([
                {"$match": {"root_id": root["_id"]}}, {"$unwind": "$chain"},
                {"$match": {"chain": {"$in": chain}}},
                {"$group": {"_id": "$chain", "count": {"$sum": 1}}},
            ], session=session)}
            if any(counts.get(m["_id"], 0) >= m["limits"]["max_actions"] for m in context.chain):
                raise Refused(Reason.LIMIT_EXCEEDED, context)
            effect = {"_id": action.action_id, "run_id": action.run_id, "mode": Mode.STOPLINE,
                      "root_id": root["_id"], "mandate_id": action.mandate_id, "chain": chain,
                      "principal": action.principal, "tool": action.tool, "target": action.target,
                      "args": dict(action.args), "args_hash": args_hash(action.args),
                      "admitted_seq": root["seq"], "state": EffectState.ADMITTED, "evidence": None,
                      "reverses": action.reverses, "undo": None, "policy_version": POLICY_VERSION,
                      "policy_hash": POLICY_HASH, "claimed_by": None, "admitted_at": root["clock"],
                      "sent_at": None, "finished_at": None}
            self.db.effects.insert_one(effect, session=session)
            self.db.audit.insert_one(self._audit(
                run_id=action.run_id, kind=AuditKind.ALLOW, principal=action.principal,
                mandate_id=action.mandate_id, root_id=root["_id"], action_id=action.action_id,
                seq=root["seq"], at=root["clock"], detail={"tool": action.tool, "target": action.target,
                "args": action.args, "chain": chain}), session=session)
            return Admission(True, action.action_id, Reason.OK, root["seq"])

        try:
            result, attempts = storage.run_transaction(admission)
            return replace(result, attempts=attempts)
        except DuplicateKeyError:
            # Two independent roots can race to use one caller-supplied action id.
            existing = self.db.effects.find_one({"_id": action.action_id})
            if existing is None:
                raise
            if self._same_action(existing, action):
                return Admission(True, action.action_id, Reason.OK, existing["admitted_seq"],
                                 attempts=attempts, replayed=True)
            refusal = Refused(Reason.SAME_ID_DIFFERENT_ARGS)
        except Refused as error:
            refusal = error
        entry = self._deny(refusal, run_id=action.run_id, principal=action.principal,
                           mandate_id=action.mandate_id, action_id=action.action_id,
                           detail={"tool": action.tool, "target": action.target, "args": action.args})
        return Admission(False, action.action_id, refusal.reason, entry["seq"], refusal.detail, attempts)

    def claim(self, action_id: str, by: str) -> Claim:
        effect = self.db.effects.find_one_and_update(
            {"_id": action_id, "state": EffectState.ADMITTED},
            [{"$set": {"state": EffectState.SENT, "claimed_by": {"$literal": by}, "sent_at": "$$NOW"}}],
            return_document=ReturnDocument.AFTER)
        if effect is not None:
            self.db.audit.insert_one(self._audit(
                run_id=effect["run_id"], kind=AuditKind.CLAIM, principal=by,
                mandate_id=effect["mandate_id"], root_id=effect["root_id"], action_id=action_id,
                seq=effect["admitted_seq"], at=effect["sent_at"]))
            return Claim(True, action_id, EffectState.SENT)
        effect = self.db.effects.find_one({"_id": action_id})
        state = effect["state"] if effect else "missing"
        detail = {}
        reason = Reason.NOT_ADMITTED
        if state == EffectState.CANCELLED:
            reason = Reason.CANCELLED_BY_STOP
            mandate = self.db.mandates.find_one({"_id": effect["mandate_id"]})
            if mandate:
                detail = {key: mandate[key] for key in ("revoked_by", "revoked_at", "revoked_at_seq")}
        return Claim(False, action_id, state, reason, detail)

    def stop(self, *, mandate_id: str, by: str):
        identity = uuid4().hex

        def stopping(session):
            try:
                context = self._context(session, mandate_id)
            except Refused as error:
                raise NotAuthorised(str(error.reason)) from None
            if by not in {m["holder"] for m in context.chain}:
                raise NotAuthorised("caller holds no mandate in this chain")
            root = context.root
            found = list(self.db.mandates.aggregate([
                {"$match": {"_id": mandate_id}},
                {"$graphLookup": {"from": "mandates", "startWith": "$_id", "connectFromField": "_id",
                                  "connectToField": "parent_id", "as": "below",
                                  "maxDepth": MAX_DEPTH}},
            ], session=session))[0]
            revoked = [mandate_id] + sorted(m["_id"] for m in found["below"])
            if len(revoked) > MAX_TREE_SIZE:
                raise NotAuthorised("mandate tree exceeds the size cap")
            self.db.mandates.update_many({"_id": {"$in": revoked}, "status": MandateStatus.ACTIVE},
                {"$set": {"status": MandateStatus.REVOKED, "revoked_by": by,
                          "revoked_at_seq": root["seq"], "revoked_at": root["clock"]}}, session=session)
            self.db.effects.update_many(
                {"root_id": root["_id"], "mandate_id": {"$in": revoked}, "state": EffectState.ADMITTED},
                {"$set": {"state": EffectState.CANCELLED, "finished_at": root["clock"]}}, session=session)
            covered = list(self.db.effects.find(
                {"root_id": root["_id"], "mandate_id": {"$in": revoked},
                 "admitted_seq": {"$lt": root["seq"]}}, session=session).sort("admitted_seq", 1))
            closed = all(effect["state"] in TERMINAL_STATES for effect in covered)
            receipt = {"_id": identity, "run_id": context.mandate["run_id"], "root_id": root["_id"],
                       "mandate_id": mandate_id, "stopped_by": by, "stop_seq": root["seq"],
                       "stopped_at": root["clock"], "revoked": revoked, "covered": [e["_id"] for e in covered],
                       "status": "closed" if closed else "open", "closed_at": root["clock"] if closed else None}
            self.db.receipts.insert_one(receipt, session=session)
            self.db.audit.insert_one(self._audit(
                run_id=receipt["run_id"], kind=AuditKind.STOP, principal=by, mandate_id=mandate_id,
                root_id=root["_id"], seq=root["seq"], at=root["clock"],
                detail={"receipt_id": identity, "revoked": revoked, "covered": receipt["covered"]}), session=session)
            return receipt

        receipt, _ = storage.run_transaction(stopping)
        return receipt

    def record_outcome(self, action_id: str, state: str, evidence: dict[str, Any]):
        if state not in (EffectState.APPLIED, EffectState.REJECTED):
            raise ValueError("outcome must be applied or rejected")
        if (evidence.get("action_id") != action_id or not isinstance(evidence.get("system"), str)
                or type(evidence.get("at_epoch")) not in (int, float)):
            raise ValueError("outcome requires target evidence for this action")

        def recording(session):
            effect = self.db.effects.find_one({"_id": action_id}, session=session)
            if effect is None:
                raise KeyError(action_id)
            if evidence["system"] != system_of(effect["target"]):
                raise ValueError("evidence is from a different target system")
            if effect["state"] in TERMINAL_STATES:
                if effect["state"] == state:
                    return effect
                raise ValueError("a terminal outcome cannot be changed")
            if effect["state"] != EffectState.SENT:
                raise NotClaimed(action_id)
            # Prevent snapshot write skew between outcomes and receipt creation/closure.
            root = self.db.mandates.find_one_and_update(
                {"_id": effect["root_id"]}, [{"$set": {"clock": "$$NOW"}}],
                return_document=ReturnDocument.AFTER, session=session)
            if root is None:
                raise NotClaimed("effect has no authority root")
            now = root["clock"]
            finished = self.db.effects.find_one_and_update(
                {"_id": action_id, "state": EffectState.SENT},
                {"$set": {"state": state, "evidence": evidence, "finished_at": now}},
                return_document=ReturnDocument.AFTER, session=session)
            self.db.audit.insert_one(self._audit(
                run_id=effect["run_id"], kind=AuditKind.OUTCOME, principal=effect["principal"],
                mandate_id=effect["mandate_id"], root_id=effect["root_id"], action_id=action_id,
                seq=root["seq"], at=now, detail={"state": state, "evidence": evidence}), session=session)
            receipts = list(self.db.receipts.find({"covered": action_id, "status": "open"}, session=session))
            if receipts:
                ids = list({identity for receipt in receipts for identity in receipt["covered"]})
                states = {e["_id"]: e["state"] for e in self.db.effects.find(
                    {"_id": {"$in": ids}}, {"state": 1}, session=session)}
                closed = [r["_id"] for r in receipts if all(states.get(i) in TERMINAL_STATES for i in r["covered"])]
                if closed:
                    self.db.receipts.update_many({"_id": {"$in": closed}, "status": "open"},
                                                {"$set": {"status": "closed", "closed_at": now}}, session=session)
            return finished

        result, _ = storage.run_transaction(recording)
        return result

    def reconcile(self, action_id: str, target: TargetAdapter):
        effect = self.db.effects.find_one({"_id": action_id})
        if effect is None:
            raise KeyError(action_id)
        if effect["state"] != EffectState.SENT:
            return effect
        if target.name != system_of(effect["target"]):
            raise ValueError("adapter is for a different target system")
        try:
            result = target.lookup(action_id)
            if result.found and result.applied:
                return self.record_outcome(action_id, EffectState.APPLIED, result.evidence)
            fenced = target.fence(action_id)
            # An in-flight send may have landed between the first lookup and fence.
            # Once fenced, a second lookup distinguishes that from a true rejection.
            result = target.lookup(action_id)
            if result.found and result.applied:
                return self.record_outcome(action_id, EffectState.APPLIED, result.evidence)
            return self.record_outcome(action_id, EffectState.REJECTED, fenced)
        except (TargetUnreachable, TargetTimeout):
            return self.db.effects.find_one({"_id": action_id})

    def undo(self, action_id: str, *, mandate_id: str, principal: str,
             targets: Mapping[str, TargetAdapter], undo_id: str | None = None) -> Admission:
        try:
            from . import compensate
        except ImportError as error:
            raise NotImplementedError("undo module is not installed") from error
        return compensate.undo(self, self.db, action_id, mandate_id=mandate_id,
                               principal=principal, targets=targets, undo_id=undo_id)

    @staticmethod
    def _chain_ids(mandate_id, mandates):
        ids = []
        while mandate_id in mandates and mandate_id not in ids:
            ids.append(mandate_id)
            mandate_id = mandates[mandate_id]["parent_id"]
        return list(reversed(ids))

    def receipt(self, receipt_id: str) -> ReceiptView:
        receipt = self.db.receipts.find_one({"_id": receipt_id})
        if receipt is None:
            raise KeyError(receipt_id)
        mandates = {m["_id"]: m for m in self.db.mandates.find({"root_id": receipt["root_id"]})}
        effects = list(self.db.effects.find({"_id": {"$in": receipt["covered"]}}).sort("admitted_seq", 1))
        refusals = list(self.db.audit.find({"run_id": receipt["run_id"], "kind": AuditKind.DENY,
            "mandate_id": {"$in": receipt["revoked"]}, "action_id": {"$ne": None},
            "at": {"$gte": receipt["stopped_at"]}}).sort([("at", 1), ("_id", 1)]))

        def chain(ids):
            return [{"mandate_id": identity, "holder": mandates[identity]["holder"],
                     "grantor": mandates[identity]["grantor"]} for identity in ids if identity in mandates]

        outcomes = {"applied": "completed", "sent": "unknown", "admitted": "unknown",
                    "cancelled": "blocked", "rejected": "no effect"}
        rows = [ReceiptRow(e["_id"], e["principal"], e["tool"], e["target"], e["args"],
                           outcomes[e["state"]], e["state"],
                           Reason.CANCELLED_BY_STOP if e["state"] == EffectState.CANCELLED else Reason.OK,
                           chain(e["chain"]), e["evidence"], e["undo"]) for e in effects]
        for entry in refusals:
            detail = entry["detail"]
            ids = detail.get("chain") or self._chain_ids(entry["mandate_id"], mandates)
            rows.append(ReceiptRow(entry["action_id"], entry["principal"], detail.get("tool", ""),
                                   detail.get("target", ""), detail.get("args", {}), "blocked", None,
                                   entry["reason"], chain(ids), None, None))
        return ReceiptView(receipt, rows, receipt["status"] == "open")

    def chain(self, action_id: str) -> ChainView:
        effect = self.db.effects.find_one({"_id": action_id})
        audit = list(self.db.audit.find({"action_id": action_id}).sort([("at", 1), ("_id", 1)]))
        root_id = effect["root_id"] if effect else next((e["root_id"] for e in audit if e["root_id"]), None)
        mandates = {m["_id"]: m for m in self.db.mandates.find({"root_id": root_id})} if root_id else {}
        ids = effect["chain"] if effect else self._chain_ids(audit[-1]["mandate_id"], mandates) if audit else []
        version = effect["policy_version"] if effect else audit[-1]["policy_version"] if audit else None
        policy = self.db.policies.find_one({"_id": version}) if version is not None else None
        return ChainView(action_id, effect, [mandates[i] for i in ids if i in mandates], policy, audit)
