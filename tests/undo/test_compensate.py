from dataclasses import replace

import pytest

from stopline.contract import EffectState, OPERATOR, Reason, Tool, UndoMark
from stopline.gate.compensate import settle, undo


def request_undo(scenario, **kwargs):
    return undo(scenario.gate, scenario.db, scenario.action.action_id,
                mandate_id=scenario.root, principal=OPERATOR, targets=scenario.targets, **kwargs)


def test_undo_shift_returns_to_zero_with_own_evidence_and_receipt(scenario):
    s = scenario
    result = request_undo(s)

    assert result.allowed and result.reason == Reason.OK
    reversing = s.db.effects.find_one({"_id": result.action_id})
    assert reversing["reverses"] == s.action.action_id
    assert reversing["mandate_id"] == s.root and reversing["principal"] == OPERATOR
    assert reversing["state"] == EffectState.ADMITTED
    assert reversing["args"] == {"percent": 0}
    assert s.router.percent == 25  # admission does not send
    assert s.gate.receipt(s.receipt_id).rows[0].undo == UndoMark.UNKNOWN

    assert s.sender.claim(result.action_id).claimed
    applied = s.sender.send(result.action_id)
    settle(s.db, result.action_id)

    assert s.router.percent == 0
    assert applied["state"] == EffectState.APPLIED
    assert applied["evidence"] == s.router.log[-1]
    assert applied["evidence"]["action_id"] == result.action_id
    assert applied["evidence"]["system"] == "router"
    assert isinstance(applied["evidence"]["at_epoch"], float)
    receipt = s.gate.receipt(s.receipt_id)
    assert receipt.rows[0].undo == UndoMark.UNDONE
    assert receipt.receipt["covered"] == [s.action.action_id]
    assert receipt.rows[0].evidence["percent"] == 25  # keep the original evidence


def test_undo_refuses_an_intervening_weight_change(scenario):
    s = scenario
    intervening = replace(s.action, action_id=f"{s.action.action_id}-later", args={"percent": 50})
    s.router.send(intervening)
    admissions_before = len(s.gate.admissions)

    result = request_undo(s)

    assert not result.allowed and result.reason == Reason.STATE_CHANGED
    assert len(s.gate.admissions) == admissions_before
    assert s.db.effects.find_one({"_id": result.action_id}) is None
    assert s.router.percent == 50
    assert s.gate.receipt(s.receipt_id).rows[0].undo is None


@pytest.mark.parametrize("tool", [Tool.RESTART, Tool.ROLLBACK])
def test_irreversible_cluster_action_is_marked_no_undo(scenario, tool):
    s = scenario
    s.db.effects.update_one({"_id": s.action.action_id}, {"$set": {
        "tool": tool, "target": "cluster/checkout-worker",
        "args": {} if tool == Tool.RESTART else {"to_version": "v1"},
    }})
    admissions_before = len(s.gate.admissions)

    result = request_undo(s)

    assert not result.allowed and result.reason == Reason.NO_UNDO
    assert s.gate.receipt(s.receipt_id).rows[0].undo == UndoMark.NO_UNDO
    assert len(s.gate.admissions) == admissions_before
    assert s.db.effects.find_one({"_id": result.action_id}) is None
