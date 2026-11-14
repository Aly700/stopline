"""Receipt closure and evidence races beyond the nine acceptance schedules."""
from concurrent.futures import ThreadPoolExecutor

import pytest
from pymongo.collection import Collection

from stopline.contract import NotAuthorised, NotClaimed, OPERATOR, Reason, TargetTimeout
from conftest import Pause, action, call, commands, root
from test_core import Target, evidence
from test_interleavings import assert_root_conflict, hold_first_mandate_read


def sent_action(gate, run_id, suffix="a"):
    mandate = root(gate, run_id)
    proposed = action(mandate, suffix)
    assert call("admit", gate.admit, proposed).allowed
    assert call("claim", gate.claim, proposed.action_id, "sender").claimed
    return mandate, proposed


def hold_effect_snapshot(monkeypatch, gate, action_id):
    pause = Pause()
    original = Collection.find_one
    armed = True

    def reading(collection, query=None, *args, **kwargs):
        nonlocal armed
        result = original(collection, query, *args, **kwargs)
        if (armed and collection.full_name == gate.db.effects.full_name
                and query == {"_id": action_id} and kwargs.get("session")):
            armed = False
            pause.hold()
        return result

    monkeypatch.setattr(Collection, "find_one", reading)
    return pause


@pytest.mark.parametrize("winner", ["stop", "outcome"])
def test_stop_and_outcome_cannot_strand_open_receipt(gate, run_id, monkeypatch, winner):
    mandate, proposed = sent_action(gate, run_id)
    pause = (hold_effect_snapshot(monkeypatch, gate, proposed.action_id) if winner == "stop"
             else hold_first_mandate_read(monkeypatch, gate, mandate["_id"]))
    with ThreadPoolExecutor(1) as pool:
        if winner == "stop":
            pending = pool.submit(call, "outcome", gate.record_outcome, proposed.action_id,
                                  "applied", evidence(proposed.action_id))
        else:
            pending = pool.submit(call, "stop", gate.stop, mandate_id=mandate["_id"], by=OPERATOR)
        try:
            pause.wait()
            if winner == "stop":
                receipt = call("stop", gate.stop, mandate_id=mandate["_id"], by=OPERATOR)
                assert receipt["status"] == "open"
            else:
                call("outcome", gate.record_outcome, proposed.action_id, "applied", evidence(proposed.action_id))
        finally:
            pause.release.set()
        result = pending.result(timeout=20)
    if winner == "outcome":
        receipt = result
        assert_root_conflict("stop", mandate["_id"])
    else:
        assert any(f["label"] == "outcome" and f["collection"] == "mandates" and f["code"] == 112
                   for f in commands.failures)
    assert not call("receipt", gate.receipt, receipt["_id"]).open
    assert gate.db.mandates.find_one({"_id": mandate["_id"]})["seq"] == 2


def test_two_final_outcomes_close_receipt(gate, run_id, monkeypatch):
    mandate, first = sent_action(gate, run_id)
    second = action(mandate, "second")
    assert call("admit", gate.admit, second).allowed
    assert call("claim", gate.claim, second.action_id, "sender").claimed
    receipt = call("stop", gate.stop, mandate_id=mandate["_id"], by=OPERATOR)
    pause = hold_effect_snapshot(monkeypatch, gate, first.action_id)
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(call, "first_outcome", gate.record_outcome, first.action_id,
                              "applied", evidence(first.action_id))
        try:
            pause.wait()
            call("second_outcome", gate.record_outcome, second.action_id, "rejected", evidence(second.action_id, fenced=True))
            assert call("receipt", gate.receipt, receipt["_id"]).open
        finally:
            pause.release.set()
        pending.result(timeout=20)
    view = call("receipt", gate.receipt, receipt["_id"])
    assert not view.open and len(view.rows) == 2
    assert any(f["label"] == "first_outcome" and f["collection"] == "mandates" and f["code"] == 112
               for f in commands.failures)


@pytest.mark.parametrize("timeout_at", ["first_lookup", "fence", "second_lookup"])
def test_9_timeout_never_closes_receipt(gate, run_id, timeout_at):
    mandate, proposed = sent_action(gate, run_id)
    receipt = call("stop", gate.stop, mandate_id=mandate["_id"], by=OPERATOR)

    class TimedOut(Target):
        lookups = 0

        def lookup(self, action_id):
            self.lookups += 1
            if (self.lookups == 1 and timeout_at == "first_lookup"
                    or self.lookups == 2 and timeout_at == "second_lookup"):
                raise TargetTimeout()
            return super().lookup(action_id)

        def fence(self, action_id):
            if timeout_at == "fence":
                raise TargetTimeout()
            return super().fence(action_id)

    result = call("reconcile_timeout", gate.reconcile, proposed.action_id, TimedOut())
    assert result["state"] == "sent" and result["evidence"] is None
    assert call("receipt", gate.receipt, receipt["_id"]).open
    assert gate.db.audit.count_documents({"action_id": proposed.action_id, "kind": "outcome"}) == 0


def test_send_between_lookup_and_fence_is_accounted_as_applied(gate, run_id):
    mandate, proposed = sent_action(gate, run_id)
    receipt = call("stop", gate.stop, mandate_id=mandate["_id"], by=OPERATOR)

    class LateSend(Target):
        def fence(self, action_id):
            self.applied = True  # the held send lands just before the target installs its fence
            return super().fence(action_id)

    result = call("reconcile_late_send", gate.reconcile, proposed.action_id, LateSend())
    assert result["state"] == "applied"
    assert not call("receipt", gate.receipt, receipt["_id"]).open


def test_record_outcome_requires_claim_and_matching_evidence(gate, run_id):
    mandate = root(gate, run_id)
    proposed = action(mandate)
    assert call("admit", gate.admit, proposed).allowed
    with pytest.raises(NotClaimed):
        call("outcome_before_claim", gate.record_outcome, proposed.action_id, "applied", evidence(proposed.action_id))
    assert call("claim", gate.claim, proposed.action_id, "sender").claimed
    with pytest.raises(ValueError):
        call("outcome_wrong_id", gate.record_outcome, proposed.action_id, "applied", evidence("wrong"))
    with pytest.raises(ValueError):
        call("outcome_wrong_system", gate.record_outcome, proposed.action_id, "applied", evidence(proposed.action_id, system="cluster"))
    first = call("outcome", gate.record_outcome, proposed.action_id, "applied", evidence(proposed.action_id))
    again = call("outcome_replay", gate.record_outcome, proposed.action_id, "applied", evidence(proposed.action_id))
    assert again == first
    with pytest.raises(ValueError):
        call("outcome_contradiction", gate.record_outcome, proposed.action_id, "rejected", evidence(proposed.action_id))
    assert gate.db.audit.count_documents({"action_id": proposed.action_id, "kind": "outcome"}) == 1


def test_failed_claim_and_stop_are_audited(gate, run_id):
    mandate, proposed = sent_action(gate, run_id)
    denied = call("claim_again", gate.claim, proposed.action_id, "another-sender")
    assert not denied.claimed
    assert gate.db.audit.find_one({"action_id": proposed.action_id, "kind": "claim", "reason": Reason.NOT_ADMITTED})
    with pytest.raises(NotAuthorised):
        call("stop_refused", gate.stop, mandate_id=mandate["_id"], by="stranger")
    assert gate.db.audit.find_one({"run_id": run_id, "kind": "deny", "reason": Reason.NOT_AUTHORISED})
    assert gate.db.mandates.find_one({"_id": mandate["_id"]})["seq"] == 1


def test_undo_delegates_lazily(gate, run_id, monkeypatch):
    import sys
    from types import SimpleNamespace
    from stopline.contract import Admission

    captured = []
    expected = Admission(False, "undo-action", Reason.NO_UNDO)

    def undo(*args, **kwargs):
        captured.append((args, kwargs))
        return expected

    monkeypatch.setitem(sys.modules, "stopline.gate.compensate", SimpleNamespace(undo=undo))
    result = gate.undo("original", mandate_id="operator-mandate", principal=OPERATOR, targets={}, undo_id="undo-action")
    assert result == expected
    assert captured == [((gate, gate.db, "original"), {"mandate_id": "operator-mandate", "principal": OPERATOR,
                                                      "targets": {}, "undo_id": "undo-action"})]


def test_unauthorised_stop_leaves_deny_after_abort(gate, run_id):
    mandate = root(gate, run_id)
    with pytest.raises(NotAuthorised):
        call("stop_refused", gate.stop, mandate_id=mandate["_id"], by="stranger")
    assert gate.db.audit.find_one({"run_id": run_id, "kind": "deny", "reason": Reason.NOT_AUTHORISED})
    assert gate.db.mandates.find_one({"_id": mandate["_id"]})["seq"] == 0


def test_outcome_lock_still_works_if_two_server_timestamps_are_equal(gate, run_id, monkeypatch):
    from pymongo.errors import OperationFailure
    from stopline.gate.core import TICK

    mandate, proposed = sent_action(gate, run_id)
    frozen_clock = gate.db.mandates.find_one({"_id": mandate["_id"]})["clock"]
    pause = Pause()
    original = Collection.find_one_and_update
    armed = True

    def same_timestamp(collection, query, update, *args, **kwargs):
        nonlocal armed
        outcome_write = (collection.full_name == gate.db.mandates.full_name
                         and isinstance(update, list) and "seq" not in update[0]["$set"])
        if outcome_write:
            update = [{"$set": {**update[0]["$set"], "clock": frozen_clock}}]
        try:
            result = original(collection, query, update, *args, **kwargs)
        except OperationFailure:
            # A real root write retains a lock. Release it on the stop's first conflict.
            if update == TICK:
                pause.release.set()
            raise
        if outcome_write and armed:
            armed = False
            pause.hold()
        return result

    monkeypatch.setattr(Collection, "find_one_and_update", same_timestamp)
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(call, "outcome", gate.record_outcome, proposed.action_id,
                              "applied", evidence(proposed.action_id))
        try:
            pause.wait()
            receipt = call("stop", gate.stop, mandate_id=mandate["_id"], by=OPERATOR)
        finally:
            pause.release.set()
        pending.result(timeout=20)
    assert not call("receipt", gate.receipt, receipt["_id"]).open


def test_claim_and_its_audit_commit_together(gate, run_id, monkeypatch):
    mandate = root(gate, run_id)
    proposed = action(mandate)
    assert call("admit", gate.admit, proposed).allowed
    original = Collection.insert_one
    armed = True

    def fail_claim_audit(collection, document, *args, **kwargs):
        nonlocal armed
        if armed and collection.full_name == gate.db.audit.full_name and document["kind"] == "claim":
            armed = False
            raise RuntimeError("injected audit insert failure")
        return original(collection, document, *args, **kwargs)

    monkeypatch.setattr(Collection, "insert_one", fail_claim_audit)
    with pytest.raises(RuntimeError, match="injected audit insert failure"):
        call("claim_audit_failure", gate.claim, proposed.action_id, "sender")
    assert gate.db.effects.find_one({"_id": proposed.action_id})["state"] == "admitted"
    assert call("claim_recovered", gate.claim, proposed.action_id, "sender").claimed
    assert gate.db.audit.count_documents({"action_id": proposed.action_id, "kind": "claim", "reason": "ok"}) == 1


@pytest.mark.parametrize("reason", ["tool_not_allowed", "same_id_different_args"])
def test_receipt_refusals_follow_sequence_when_clocks_are_equal(gate, run_id, monkeypatch, reason):
    from dataclasses import replace
    from stopline.gate.core import TICK

    mandate = root(gate, run_id)
    proposed = action(mandate)
    if reason == "same_id_different_args":
        assert call("admit_original", gate.admit, proposed).allowed
        refused_before = replace(proposed, args={"percent": 40})
    else:
        refused_before = replace(proposed, tool="unpermitted")
    fixed_time = gate.db.mandates.find_one({"_id": mandate["_id"]})["clock"]
    original = Collection.find_one_and_update

    def same_time(collection, query, update, *args, **kwargs):
        if collection.full_name == gate.db.mandates.full_name and update == TICK:
            update = [{"$set": {**TICK[0]["$set"], "clock": fixed_time}}]
        return original(collection, query, update, *args, **kwargs)

    monkeypatch.setattr(Collection, "find_one_and_update", same_time)
    monkeypatch.setattr(gate, "_now", lambda: fixed_time)
    assert not call("deny_before_stop", gate.admit, refused_before).allowed
    receipt = call("stop", gate.stop, mandate_id=mandate["_id"], by=OPERATOR)
    initial = call("receipt", gate.receipt, receipt["_id"])
    assert not [row for row in initial.rows if row.state is None]
    assert not call("deny_after_stop", gate.admit, refused_before).allowed
    later = call("receipt", gate.receipt, receipt["_id"])
    assert len([row for row in later.rows if row.state is None]) == 1


def test_8_deny_survives_abort_and_an_intervening_stop(gate, run_id, monkeypatch):
    from pymongo.errors import WriteError

    mandate = root(gate, run_id)
    proposed = action(mandate, tool="unpermitted")
    pause = Pause()
    original = Collection.insert_one

    def hold_deny_after_abort(collection, document, *args, **kwargs):
        if collection.full_name == gate.db.audit.full_name and document["kind"] == "deny":
            assert kwargs.get("session") is None, "deny must be its own insert after abort"
            pause.hold()
        return original(collection, document, *args, **kwargs)

    monkeypatch.setattr(Collection, "insert_one", hold_deny_after_abort)
    with gate.db.client.start_session() as validator_session:
        call("second_session", gate.db.command, "ping", session=validator_session)
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(call, "denied_admission", gate.admit, proposed)
            try:
                pause.wait()
                assert gate.db.mandates.find_one({"_id": mandate["_id"]})["seq"] == 0
                assert gate.db.effects.find_one({"_id": proposed.action_id}) is None
                with pytest.raises(WriteError) as rejected:
                    call("invalid_audit_other_session", gate.db.audit.insert_one,
                         {"kind": "allow"}, session=validator_session)
                assert rejected.value.code == 121
                receipt = call("stop_before_deny_insert", gate.stop, mandate_id=mandate["_id"], by=OPERATOR)
            finally:
                pause.release.set()
            refused = pending.result(timeout=20)
    assert not refused.allowed and refused.reason == Reason.TOOL_NOT_ALLOWED
    assert gate.db.audit.find_one({"action_id": proposed.action_id, "kind": "deny"})
    assert receipt["covered"] == [] and receipt["stop_seq"] == 1
    assert not call("receipt", gate.receipt, receipt["_id"]).rows
