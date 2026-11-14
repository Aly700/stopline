"""P1, P2 and P3: revocation, fixed coverage and the nontransactional claim."""

import pytest

from stopline.contract import EffectState, OPERATOR, Reason
from concurrency_helpers import (
    Pause, action, at_method, child, delegation, evidence, interleave, root, sent, snapshot,
)


@pytest.mark.parametrize("stopped,winner", [("root", "stop"), ("branch", "admit")])
def test_new_admission_is_refused_or_covered(concurrency_gate, monkeypatch, winner, stopped):
    gate = concurrency_gate
    parent = root(gate)
    branch = child(gate, parent)
    leaf = child(gate, branch)
    cutoff = parent if stopped == "root" else branch
    proposed = action(leaf)
    held_mandate = leaf if winner == "stop" else cutoff
    pause = snapshot(monkeypatch, gate, held_mandate)
    admit = lambda: gate.admit(proposed)
    stop = lambda: gate.stop(mandate_id=cutoff["_id"], by=OPERATOR)
    if winner == "stop":
        admission, receipt = interleave(pause, admit, stop)
        assert not admission.allowed and admission.reason == Reason.REVOKED
        assert admission.attempts >= 2
    else:
        receipt, admission = interleave(pause, stop, admit)
        assert admission.allowed
        assert admission.seq < receipt["stop_seq"]
        assert pause.calls >= 2, "stop must retry its stale root snapshot"
    assert admission.allowed == (proposed.action_id in receipt["covered"])
    assert gate.db.effects.count_documents({"_id": proposed.action_id}) == int(admission.allowed)
    for index, mandate in enumerate((cutoff, leaf)):
        after = gate.admit(action(mandate, f"later-{index}"))
        assert not after.allowed and after.reason == Reason.REVOKED
        handed = gate.hand_down(**delegation(mandate, budget=0))
        assert not handed.allowed and handed.reason == Reason.REVOKED


@pytest.mark.parametrize("winner", ["stop", "hand_down"])
def test_concurrent_child_cannot_escape_stop(concurrency_gate, monkeypatch, winner):
    gate = concurrency_gate
    parent = root(gate)
    branch = child(gate, parent)
    pause = snapshot(monkeypatch, gate, branch if winner == "stop" else parent)
    delegate = lambda: gate.hand_down(**delegation(branch))
    stop = lambda: gate.stop(mandate_id=parent["_id"], by=OPERATOR)
    if winner == "stop":
        result, receipt = interleave(pause, delegate, stop)
        assert not result.allowed and result.reason == Reason.REVOKED
        assert result.attempts >= 2
    else:
        receipt, result = interleave(pause, stop, delegate)
        assert result.allowed and result.mandate_id in receipt["revoked"]
        created = gate.db.mandates.find_one({"_id": result.mandate_id})
        assert created["status"] == "revoked"
        assert not gate.admit(action(created)).allowed
        assert not gate.hand_down(**delegation(created, budget=0)).allowed
    assert gate.db.mandates.count_documents({"root_id": parent["_id"], "status": "active"}) == 0


@pytest.mark.parametrize("checkpoint", ["before_call", "after_read"])
def test_replay_is_refused_after_stop_returns(concurrency_gate, monkeypatch, checkpoint):
    """A historical replay must not return a fresh allowed result after revocation."""
    gate = concurrency_gate
    parent = root(gate)
    mandate = child(gate, parent)
    proposed = action(mandate)
    assert gate.admit(proposed).allowed
    if checkpoint == "before_call":
        pause = Pause()

        def replay_action():
            pause.hold()
            return gate.admit(proposed)
    else:
        pause = at_method(monkeypatch, gate.db.effects, "find_one",
                          lambda args, kwargs: args[0] == {"_id": proposed.action_id})
        replay_action = lambda: gate.admit(proposed)
    replay, receipt = interleave(
        pause, replay_action,
        lambda: gate.stop(mandate_id=parent["_id"], by=OPERATOR))
    assert proposed.action_id in receipt["covered"]
    assert gate.db.effects.find_one({"_id": proposed.action_id})["state"] == EffectState.CANCELLED
    assert not gate.claim(proposed.action_id, "late-sender").claimed
    assert not replay.allowed, "P1: the replay returned allowed=True after stop completed"


@pytest.mark.parametrize("winner", ["stop", "claim"])
def test_claim_never_changes_cancelled_effect_to_sent(concurrency_gate, monkeypatch, winner):
    gate = concurrency_gate
    parent = root(gate)
    mandate = child(gate, parent)
    proposed = action(mandate)
    assert gate.admit(proposed).allowed
    claim = lambda: gate.claim(proposed.action_id, "sender")
    stop = lambda: gate.stop(mandate_id=parent["_id"], by=OPERATOR)
    if winner == "stop":
        pause = at_method(monkeypatch, gate.db.effects, "find_one_and_update", before=True)
        claimed, receipt = interleave(pause, claim, stop)
        assert not claimed.claimed and claimed.state == EffectState.CANCELLED
    else:
        pause = at_method(monkeypatch, gate.db.effects, "update_many", before=True)
        receipt, claimed = interleave(pause, stop, claim)
        assert claimed.claimed and receipt["status"] == "open"
        # The cancel is the stop's first statement, so a claim that commits before it is simply
        # seen as sent. No retry is needed, and the properties below are what matter.
    assert proposed.action_id in receipt["covered"]
    assert not gate.claim(proposed.action_id, "late-sender").claimed
    final = gate.db.effects.find_one({"_id": proposed.action_id})
    assert final["state"] == (EffectState.SENT if winner == "claim" else EffectState.CANCELLED)


def test_second_stop_of_revoked_subtree_keeps_coverage(concurrency_gate, monkeypatch):
    gate = concurrency_gate
    parent = root(gate)
    mandate = child(gate, parent)
    in_flight = sent(gate, mandate, "sent")
    unsent = action(mandate, "unsent")
    assert gate.admit(unsent).allowed
    first = gate.stop(mandate_id=mandate["_id"], by=OPERATOR)
    pause = snapshot(monkeypatch, gate, mandate)
    stop = lambda: gate.stop(mandate_id=mandate["_id"], by=OPERATOR)
    second, _ = interleave(
        pause, stop,
        lambda: gate.record_outcome(in_flight.action_id, EffectState.APPLIED, evidence(in_flight.action_id)))
    assert first["stop_seq"] < second["stop_seq"]
    assert pause.calls >= 2
    expected = {in_flight.action_id, unsent.action_id}
    assert first["status"] == "open" and second["status"] == "closed"
    for receipt in (first, second):
        assert set(receipt["covered"]) == expected
        assert gate.db.receipts.find_one({"_id": receipt["_id"]})["status"] == "closed"
    assert not gate.admit(action(mandate, "after-second-stop")).allowed
    assert gate.db.mandates.find_one({"_id": mandate["_id"]})["revoked_at_seq"] == first["stop_seq"]
