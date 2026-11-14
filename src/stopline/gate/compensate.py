"""Admit a compensating action; delivery still belongs to the sender.

The gate delegates undo here. After sending or reconciling the returned action,
the caller calls settle so the original receipt row reflects the outcome.
"""

from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from pymongo.database import Database

from stopline.contract import (
    Action, Admission, AuditKind, EffectState, GateAPI, MandateStatus, Reason,
    TargetAdapter, TargetTimeout, TargetUnreachable, UndoMark, system_of,
)


def _action(effect: Mapping[str, Any]) -> Action:
    return Action(
        action_id=effect["_id"], run_id=effect["run_id"],
        mandate_id=effect["mandate_id"], principal=effect["principal"],
        tool=effect["tool"], target=effect["target"], args=effect["args"],
        reverses=effect["reverses"],
    )


def _matches(state: Mapping[str, Any], expect: Mapping[str, Any]) -> bool:
    """An inverse names the state fields it relies on, not the whole page view."""
    for key, expected in expect.items():
        if key not in state:
            return False
        actual = state[key]
        if isinstance(expected, Mapping):
            if not isinstance(actual, Mapping) or not _matches(actual, expected):
                return False
        elif actual != expected:
            return False
    return True


def undo(
    gate: GateAPI,
    db: Database,
    action_id: str,
    *,
    mandate_id: str,
    principal: str,
    targets: Mapping[str, TargetAdapter],
    undo_id: str | None = None,
) -> Admission:
    """Check authority and the target precondition, then use ordinary admission.

    The immutable admission chain establishes ancestry. The gate rechecks live
    authority, expiry, scope and limits in its admission transaction. Target
    reads happen outside that transaction. This function never claims or sends.
    Supply the same undo_id when retrying an admission or recovering its reply.
    """
    undo_id = undo_id if undo_id is not None else f"undo-{uuid4().hex}"
    # Read both ids and the server clock in one round trip. A retry must use
    # the persisted inverse even after that inverse has changed the target.
    effects = {effect["_id"]: effect for effect in db.effects.aggregate([
        {"$match": {"_id": {"$in": [action_id, undo_id]}}},
        {"$set": {"decision_at": "$$NOW"}},
    ])}
    original = effects.get(action_id)
    previous = effects.get(undo_id)
    mandate = db.mandates.find_one({"_id": mandate_id})

    def audit(kind: str, reason: str, seq: int | None = None) -> None:
        source = original or mandate or {}
        policy_version, policy_hash = source.get("policy_version"), source.get("policy_hash")
        if policy_version is None or policy_hash is None:
            policy = db.policies.find_one(sort=[("_id", -1)])
            if policy is None:
                raise RuntimeError("call gate.setup before undo")
            policy_version, policy_hash = policy["_id"], policy["hash"]
        at = original["decision_at"] if original else db.command("hello")["localTime"]
        db.audit.insert_one({
            "run_id": source.get("run_id", ""), "kind": kind,
            "principal": principal, "mandate_id": mandate_id,
            "root_id": source.get("root_id"), "action_id": undo_id,
            "policy_version": policy_version, "policy_hash": policy_hash,
            "reason": reason, "seq": seq, "at": at,
            "detail": {"reverses": action_id, "tool": source.get("tool"),
                       "target": source.get("target"), "args": source.get("args", {})},
        })

    def refuse(reason: str) -> Admission:
        audit(AuditKind.DENY, reason)
        return Admission(False, undo_id, reason, detail={"reverses": action_id})

    if original is None:
        return refuse(Reason.NOT_ADMITTED)
    if (
        mandate is None
        or mandate["status"] != MandateStatus.ACTIVE
        or mandate["holder"] != principal
        or mandate_id not in original["chain"][:-1]
    ):
        return refuse(Reason.NOT_AUTHORISED)

    def admit(reversing: Action) -> Admission:
        admission = gate.admit(reversing)
        if admission.allowed:
            mark = UndoMark.UNKNOWN
            if previous and previous["state"] == EffectState.APPLIED:
                mark = UndoMark.UNDONE
            db.effects.update_one(
                {"_id": action_id, "undo": {"$ne": UndoMark.UNDONE}},
                {"$set": {"undo": mark}},
            )
            audit(AuditKind.UNDO, Reason.OK, admission.seq)
        # Ordinary admission already audits its own refusals.
        return admission

    if previous:
        if (
            previous["reverses"] != action_id
            or previous["mandate_id"] != mandate_id
            or previous["principal"] != principal
        ):
            return refuse(Reason.SAME_ID_DIFFERENT_ARGS)
        if previous["state"] != EffectState.ADMITTED:
            return admit(_action(previous))

    action = _action(original)
    target = targets[system_of(action.target)]
    try:
        inverse = target.inverse_of(action)
    except (TargetUnreachable, TargetTimeout):
        return refuse(Reason.TARGET_UNREACHABLE)
    if inverse is None:
        db.effects.update_one(
            {"_id": action_id, "undo": {"$ne": UndoMark.UNDONE}},
            {"$set": {"undo": UndoMark.NO_UNDO}},
        )
        return refuse(Reason.NO_UNDO)
    if original["state"] != EffectState.APPLIED:
        return refuse(Reason.NOT_ADMITTED)
    state = target.state()
    if not state.get("reachable", False):
        return refuse(Reason.TARGET_UNREACHABLE)
    if not _matches(state, inverse.expect):
        return refuse(Reason.STATE_CHANGED)

    return admit(Action(
        action_id=undo_id, run_id=action.run_id, mandate_id=mandate_id,
        principal=principal, tool=inverse.tool, target=inverse.target,
        args=inverse.args, reverses=action_id,
    ))


def settle(db: Database, undo_action_id: str) -> None:
    """Only confirmed application proves the original action was undone.

    A sent action is unknown even if a target read happens to show the desired
    weight. Reconciliation must record this action's own evidence first.
    """
    effect = db.effects.find_one({"_id": undo_action_id})
    if effect and effect["reverses"] and effect["state"] == EffectState.APPLIED:
        db.effects.update_one(
            {"_id": effect["reverses"]}, {"$set": {"undo": UndoMark.UNDONE}},
        )
