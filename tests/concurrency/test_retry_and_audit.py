"""P7 and P8: refreshed decisions, atomic audit records and server validation."""

import pytest
from pymongo.collection import Collection
from pymongo.errors import OperationFailure, WriteError

from stopline.contract import EffectState, OPERATOR, Reason
from concurrency_helpers import (
    Pause, action, at_method, audit, delegation, interleave, is_held_actor, root, snapshot,
)


def test_real_write_conflict_rechecks_expiry(concurrency_gate, monkeypatch):
    gate = concurrency_gate
    mandate = root(gate)
    proposed = action(mandate)
    pause = snapshot(monkeypatch, gate, mandate)

    def expire():
        return gate.db.mandates.update_one({"_id": mandate["_id"]},
                                           [{"$set": {"expires_at": "$$NOW"}}])

    refused, update = interleave(pause, lambda: gate.admit(proposed), expire)
    assert update.modified_count == 1
    assert not refused.allowed and refused.reason == Reason.EXPIRED
    assert refused.attempts >= 2
    assert pause.values[0]["expires_at"] > pause.values[-1]["expires_at"]
    assert gate.db.effects.find_one({"_id": proposed.action_id}) is None
    assert len(audit(gate, proposed, "deny")) == 1 and not audit(gate, proposed, "allow")


def test_retry_refuses_expiry_exactly_equal_to_decision_clock(concurrency_gate, monkeypatch):
    """Freeze only the tick's time expression to test equality, without a wall-clock race."""
    gate = concurrency_gate
    mandate = root(gate)
    boundary = mandate["clock"]
    proposed = action(mandate)
    original = Collection.find_one_and_update
    clocks = []

    def fixed_tick(current, query, update, *args, **kwargs):
        if current.full_name == gate.db.mandates.full_name and query == {"_id": mandate["_id"]}:
            update = [{"$set": {**update[0]["$set"], "clock": {"$literal": boundary}}}, *update[1:]]
        value = original(current, query, update, *args, **kwargs)
        if current.full_name == gate.db.mandates.full_name and value is not None:
            clocks.append(value["clock"])
        return value

    monkeypatch.setattr(Collection, "find_one_and_update", fixed_tick)
    pause = snapshot(monkeypatch, gate, mandate)
    refused, _ = interleave(
        pause, lambda: gate.admit(proposed),
        lambda: gate.db.mandates.update_one({"_id": mandate["_id"]}, {"$set": {"expires_at": boundary}}))
    assert refused.attempts >= 2
    assert clocks == [boundary] and pause.values[-1]["expires_at"] == boundary
    assert not refused.allowed and refused.reason == Reason.EXPIRED
    assert gate.db.effects.find_one({"_id": proposed.action_id}) is None


@pytest.mark.parametrize("operation", ["admit", "hand_down"])
def test_driver_retry_discards_a_fully_written_allow(concurrency_gate, monkeypatch, operation):
    """Abort after real writes, inject a labelled transient failure, then let the driver retry.

    Explicit abort releases the root before the other actor stops it. No
    callback, session, read result or retry machinery is replaced.
    """
    gate = concurrency_gate
    mandate = root(gate)
    proposed = action(mandate)
    identity = f'{mandate["run_id"]}-child'
    pause = Pause()
    original = Collection.insert_one
    observations = []

    def abort_first(current, document, *args, **kwargs):
        result = original(current, document, *args, **kwargs)
        if (is_held_actor() and current.full_name == gate.db.audit.full_name
                and document["kind"] == ("allow" if operation == "admit" else "hand_down")
                and not observations):
            observations.append(document.copy())
            kwargs["session"].abort_transaction()
            pause.hold()
            raise OperationFailure("injected retry after an aborted decision", code=112,
                                   details={"errorLabels": ["TransientTransactionError"]})
        return result

    monkeypatch.setattr(Collection, "insert_one", abort_first)
    held = (lambda: gate.admit(proposed)) if operation == "admit" else (
        lambda: gate.hand_down(**delegation(mandate, mandate_id=identity)))
    refused, receipt = interleave(
        pause, held, lambda: gate.stop(mandate_id=mandate["_id"], by=OPERATOR))
    assert len(observations) == 1
    assert not refused.allowed and refused.reason == Reason.REVOKED
    assert refused.attempts == 2
    assert receipt["covered"] == []
    assert gate.db.effects.find_one({"_id": proposed.action_id}) is None
    assert gate.db.mandates.find_one({"_id": identity}) is None
    assert gate.db.audit.count_documents({"run_id": mandate["run_id"], "kind": {"$in": ["allow", "hand_down"]}}) == 0
    assert gate.db.audit.count_documents({"run_id": mandate["run_id"], "kind": "deny"}) == 1
    assert gate.db.mandates.find_one({"_id": mandate["_id"]})["seq"] == receipt["stop_seq"] == 1


def test_effect_and_allow_are_visible_together(concurrency_gate, monkeypatch):
    gate = concurrency_gate
    mandate = root(gate)
    proposed = action(mandate)
    pause = at_method(monkeypatch, gate.db.effects, "insert_one")

    def observe_uncommitted():
        assert gate.db.effects.find_one({"_id": proposed.action_id}) is None
        assert not audit(gate, proposed, "allow")

    admitted, _ = interleave(pause, lambda: gate.admit(proposed), observe_uncommitted)
    assert admitted.allowed
    effect = gate.db.effects.find_one({"_id": proposed.action_id})
    entries = audit(gate, proposed, "allow")
    assert effect is not None and len(entries) == 1
    assert effect["admitted_seq"] == entries[0]["seq"] == admitted.seq


def test_refusal_audit_commits_after_abort_while_stop_runs(concurrency_gate, monkeypatch):
    gate = concurrency_gate
    mandate = root(gate)
    proposed = action(mandate, tool="unpermitted")
    pause = at_method(monkeypatch, gate.db.audit, "insert_one",
                      lambda args, kwargs: args[0]["kind"] == "deny", before=True)
    denied, receipt = interleave(
        pause, lambda: gate.admit(proposed),
        lambda: gate.stop(mandate_id=mandate["_id"], by=OPERATOR))
    assert not denied.allowed and denied.reason == Reason.TOOL_NOT_ALLOWED
    assert gate.db.effects.find_one({"_id": proposed.action_id}) is None
    entries = audit(gate, proposed, "deny")
    assert len(entries) == 1 and entries[0]["reason"] == denied.reason
    assert not audit(gate, proposed, "allow")
    assert receipt["covered"] == [] and receipt["stop_seq"] == 1


@pytest.mark.parametrize("kind", ["allow", "deny"])
def test_validator_rejects_gate_own_audit_writes(concurrency_gate, monkeypatch, kind):
    """Remove one required field at the wire boundary, retain actual server validation."""
    gate = concurrency_gate
    bad_root, good_root = root(gate), root(gate)
    bad = action(bad_root, tool="unpermitted" if kind == "deny" else "shift_traffic")
    good = action(good_root)
    pause = Pause()
    original = Collection.insert_one
    sessions = []

    def malformed(current, document, *args, **kwargs):
        if (is_held_actor() and current.full_name == gate.db.audit.full_name
                and document.get("action_id") == bad.action_id):
            sessions.append(kwargs.get("session"))
            document = {key: value for key, value in document.items() if key != "policy_hash"}
            pause.hold()
        return original(current, document, *args, **kwargs)

    monkeypatch.setattr(Collection, "insert_one", malformed)

    def attempt():
        try:
            gate.admit(bad)
        except WriteError as error:
            return error.code
        return None

    code, allowed = interleave(pause, attempt, lambda: gate.admit(good))
    assert code == 121, "the server must reject the gate's malformed entry"
    assert allowed.allowed and len(audit(gate, good, "allow")) == 1
    assert len(sessions) == 1 and (sessions[0] is not None) == (kind == "allow")
    assert gate.db.effects.find_one({"_id": bad.action_id}) is None
    assert not audit(gate, bad, kind)
    assert gate.db.mandates.find_one({"_id": bad_root["_id"]})["seq"] == 0


def test_refused_claim_has_a_deny_entry(concurrency_gate, monkeypatch):
    gate = concurrency_gate
    mandate = root(gate)
    proposed = action(mandate)
    assert gate.admit(proposed).allowed
    pause = at_method(monkeypatch, gate.db.effects, "find_one_and_update", before=True)
    refused, receipt = interleave(
        pause, lambda: gate.claim(proposed.action_id, "sender"),
        lambda: gate.stop(mandate_id=mandate["_id"], by=OPERATOR))
    assert receipt["status"] == "closed"
    assert not refused.claimed and refused.state == EffectState.CANCELLED
    assert refused.reason == Reason.CANCELLED_BY_STOP
    entries = audit(gate, proposed, "deny")
    assert any(entry["reason"] == refused.reason for entry in entries), "P8: refused claim has no deny audit entry"
