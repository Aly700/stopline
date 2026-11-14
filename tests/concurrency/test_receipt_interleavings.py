"""P4: durable receipt closure and consistency of the public receipt reader."""

import pytest
from pymongo.collection import Collection

from stopline.contract import EffectState, OPERATOR, TERMINAL_STATES
from concurrency_helpers import (
    at_method, evidence, interleave, interleave_until_write_or_conflict, root, sent, snapshot,
)


@pytest.mark.parametrize("winner", ["stop", "outcome"])
def test_last_outcome_and_stop_cannot_miss_each_other(concurrency_gate, monkeypatch, winner):
    gate = concurrency_gate
    mandate = root(gate)
    proposed = sent(gate, mandate)
    outcome = lambda: gate.record_outcome(proposed.action_id, EffectState.APPLIED,
                                          evidence(proposed.action_id))
    stop = lambda: gate.stop(mandate_id=mandate["_id"], by=OPERATOR)
    if winner == "stop":
        pause = at_method(monkeypatch, gate.db.effects, "find_one")
        finished, receipt = interleave(pause, outcome, stop)
        assert receipt["status"] == "open", "the effect was still sent when stop returned"
    else:
        pause = snapshot(monkeypatch, gate, mandate)
        receipt, finished = interleave(pause, stop, outcome)
        assert receipt["status"] == "closed"
    assert pause.calls >= 2, "the loser must re-read after a real root write conflict"
    assert finished["state"] == EffectState.APPLIED
    stored = gate.db.receipts.find_one({"_id": receipt["_id"]})
    assert stored["covered"] == [proposed.action_id]
    assert stored["status"] == "closed" and stored["closed_at"] is not None


def test_two_final_outcomes_close_only_after_both_commit(concurrency_gate, monkeypatch):
    gate = concurrency_gate
    mandate = root(gate)
    first, second = sent(gate, mandate, "one"), sent(gate, mandate, "two")
    receipt = gate.stop(mandate_id=mandate["_id"], by=OPERATOR)
    assert receipt["status"] == "open"
    pause = at_method(monkeypatch, gate.db.effects, "find_one",
                      lambda args, kwargs: args[0] == {"_id": first.action_id})

    def finish_second():
        result = gate.record_outcome(second.action_id, EffectState.REJECTED, evidence(second.action_id))
        assert gate.db.effects.find_one({"_id": first.action_id})["state"] == EffectState.SENT
        assert gate.db.receipts.find_one({"_id": receipt["_id"]})["status"] == "open"
        return result

    applied, rejected = interleave(
        pause, lambda: gate.record_outcome(first.action_id, EffectState.APPLIED, evidence(first.action_id)),
        finish_second)
    assert pause.calls >= 2
    assert {applied["state"], rejected["state"]} <= TERMINAL_STATES
    stored = gate.db.receipts.find_one({"_id": receipt["_id"]})
    assert set(stored["covered"]) == {first.action_id, second.action_id}
    assert stored["status"] == "closed" and stored["closed_at"] is not None


def test_receipt_view_is_closed_when_all_its_rows_are_terminal(concurrency_gate, monkeypatch):
    """The receipt reader must not combine an old open status with new terminal rows."""
    gate = concurrency_gate
    mandate = root(gate)
    proposed = sent(gate, mandate)
    receipt = gate.stop(mandate_id=mandate["_id"], by=OPERATOR)
    pause = at_method(monkeypatch, gate.db.receipts, "find_one",
                      lambda args, kwargs: args[0] == {"_id": receipt["_id"]})
    view, _ = interleave(
        pause, lambda: gate.receipt(receipt["_id"]),
        lambda: gate.record_outcome(proposed.action_id, EffectState.APPLIED, evidence(proposed.action_id)))
    assert gate.db.receipts.find_one({"_id": receipt["_id"]})["status"] == "closed"
    assert len(view.rows) == 1 and view.rows[0].action_id == proposed.action_id
    has_nonterminal = any(row.state not in TERMINAL_STATES for row in view.rows)
    assert view.open == has_nonterminal, "P4: returned receipt is open although every returned covered row is terminal"


@pytest.mark.parametrize("competitor", ["stop", "outcome"])
def test_equal_clock_outcome_still_serializes_receipt_closure(concurrency_gate, monkeypatch, competitor):
    """Model equal millisecond clock values, retaining real server no-op semantics.

    Only the outcome update's $$NOW expression is frozen at a persisted server
    timestamp. All queries, writes, sessions, commits and conflicts are real.
    This is explicit clock fault injection, not a claim about race frequency.
    """
    gate = concurrency_gate
    mandate = root(gate)
    first = sent(gate, mandate, "one")
    if competitor == "outcome":
        second = sent(gate, mandate, "two")
        receipt = gate.stop(mandate_id=mandate["_id"], by=OPERATOR)
    fixed_clock = gate.db.mandates.find_one({"_id": mandate["_id"]})["clock"]
    original = Collection.find_one_and_update
    frozen_calls = []

    def equal_clock(current, query, update, *args, **kwargs):
        if (current.full_name == gate.db.mandates.full_name
                and query == {"_id": mandate["_id"]}
                and isinstance(update, list) and "clock" in update[0].get("$set", {})
                and "seq" not in update[0]["$set"]):
            update = [{"$set": {**update[0]["$set"], "clock": {"$literal": fixed_clock}}}, *update[1:]]
            frozen_calls.append(kwargs.get("session"))
        return original(current, query, update, *args, **kwargs)

    monkeypatch.setattr(Collection, "find_one_and_update", equal_clock)
    pause = at_method(monkeypatch, gate.db.mandates, "find_one_and_update")
    outcome = lambda: gate.record_outcome(first.action_id, EffectState.APPLIED, evidence(first.action_id))
    if competitor == "stop":
        _, receipt = interleave_until_write_or_conflict(
            monkeypatch, pause, gate.db.mandates, outcome,
            lambda: gate.stop(mandate_id=mandate["_id"], by=OPERATOR))
    else:
        interleave_until_write_or_conflict(
            monkeypatch, pause, gate.db.mandates, outcome,
            lambda: gate.record_outcome(second.action_id, EffectState.REJECTED, evidence(second.action_id)))
    assert frozen_calls and all(session is not None for session in frozen_calls)
    effects = list(gate.db.effects.find({"_id": {"$in": receipt["covered"]}}))
    assert effects and all(effect["state"] in TERMINAL_STATES for effect in effects)
    stored = gate.db.receipts.find_one({"_id": receipt["_id"]})
    assert stored["status"] == "closed", "P4: no-op root clock update let terminal effects leave a durable open receipt"
