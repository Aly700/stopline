from dataclasses import replace

import pytest

from stopline.contract import EffectState, MandateStatus, OPERATOR, Reason, Tool, UndoMark
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


@pytest.mark.parametrize("caller", ["stopped_worker", "stopped_repair", "stolen_root", "revoked_root"])
def test_only_the_stoppers_active_mandate_can_undo(scenario, caller):
    s = scenario
    mandate_id, principal = s.root, OPERATOR
    if caller == "stopped_worker":
        mandate_id, principal = s.worker, s.action.principal
    elif caller == "stopped_repair":
        mandate_id, principal = s.repair, "agent:repair"
    elif caller == "stolen_root":
        principal = s.action.principal
    else:
        s.db.mandates.update_one({"_id": s.root}, {"$set": {"status": MandateStatus.REVOKED}})
    admissions_before = len(s.gate.admissions)

    result = undo(s.gate, s.db, s.action.action_id, mandate_id=mandate_id,
                  principal=principal, targets={})  # must refuse before target access

    assert not result.allowed and result.reason == Reason.NOT_AUTHORISED
    assert len(s.gate.admissions) == admissions_before
    assert s.router.percent == 25
    assert s.db.effects.find_one({"_id": result.action_id}) is None
    assert s.gate.receipt(s.receipt_id).rows[0].undo is None


def test_an_active_sibling_mandate_has_no_authority_over_the_original(scenario):
    s = scenario
    sibling = s.db.mandates.find_one({"_id": s.worker})
    sibling.update(_id=f"{s.worker}-sibling", status=MandateStatus.ACTIVE, holder=OPERATOR)
    s.db.mandates.insert_one(sibling)

    result = undo(s.gate, s.db, s.action.action_id, mandate_id=sibling["_id"],
                  principal=OPERATOR, targets={})

    assert not result.allowed and result.reason == Reason.NOT_AUTHORISED


def test_undo_uses_the_same_gate_checks_as_any_other_action(scenario):
    s = scenario
    s.gate.refuse = Reason.LIMIT_EXCEEDED

    result = request_undo(s)

    assert not result.allowed and result.reason == Reason.LIMIT_EXCEEDED
    assert s.db.effects.find_one({"_id": result.action_id}) is None
    assert s.gate.receipt(s.receipt_id).rows[0].undo is None
    assert s.router.percent == 25


def test_a_lost_undo_reply_stays_unknown_until_reconciled(scenario):
    s = scenario
    result = request_undo(s)
    assert result.allowed
    assert s.sender.claim(result.action_id).claimed
    s.router.cut()

    unknown = s.sender.send(result.action_id)
    settle(s.db, result.action_id)

    assert s.router.percent == 0  # the target did apply it, but its answer was lost
    assert unknown["state"] == EffectState.SENT and unknown["evidence"] is None
    assert s.gate.receipt(s.receipt_id).rows[0].undo == UndoMark.UNKNOWN
    assert s.sender.reconcile(result.action_id)["state"] == EffectState.SENT
    settle(s.db, result.action_id)
    assert s.gate.receipt(s.receipt_id).rows[0].undo == UndoMark.UNKNOWN

    s.router.restore()
    reconciled = s.sender.reconcile(result.action_id)
    settle(s.db, result.action_id)

    assert reconciled["state"] == EffectState.APPLIED
    assert reconciled["evidence"] == s.router.log[-1]
    assert reconciled["evidence"]["action_id"] == result.action_id
    assert s.gate.receipt(s.receipt_id).rows[0].undo == UndoMark.UNDONE
    assert len(s.router.log) == 2  # reconciliation never sends again


def test_same_weight_written_by_another_action_is_still_a_state_change(scenario):
    s = scenario
    s.router.send(replace(s.action, action_id=f"{s.action.action_id}-later"))

    result = request_undo(s)

    assert not result.allowed and result.reason == Reason.STATE_CHANGED
    assert s.router.percent == 25


def test_unreachable_target_does_not_mark_the_action_irreversible(scenario):
    s = scenario
    s.router.reachable = False

    result = request_undo(s)

    assert not result.allowed and result.reason == Reason.TARGET_UNREACHABLE
    assert s.gate.receipt(s.receipt_id).rows[0].undo is None


def test_settle_before_send_does_not_claim_the_undo_completed(scenario):
    s = scenario
    result = request_undo(s)

    settle(s.db, result.action_id)

    assert s.gate.receipt(s.receipt_id).rows[0].undo == UndoMark.UNKNOWN
    assert s.db.effects.find_one({"_id": result.action_id})["state"] == EffectState.ADMITTED
    assert s.router.percent == 25


def test_undo_decisions_are_audited_with_server_time_and_original_policy(scenario):
    s = scenario
    admitted = request_undo(s)
    allowed = s.db.audit.find_one({"action_id": admitted.action_id, "kind": "undo"})
    assert allowed is not None
    assert allowed["principal"] == OPERATOR and allowed["mandate_id"] == s.root
    assert allowed["reason"] == Reason.OK and allowed["seq"] == admitted.seq
    assert allowed["detail"]["reverses"] == s.action.action_id
    assert allowed["policy_version"] == 1 and allowed["policy_hash"] == "test-policy"
    assert allowed["at"].tzinfo is not None

    s.router.send(replace(s.action, action_id=f"{s.action.action_id}-later", args={"percent": 50}))
    refused = request_undo(s)
    denied = s.db.audit.find_one({"action_id": refused.action_id, "kind": "deny"})
    assert denied is not None
    assert denied["reason"] == Reason.STATE_CHANGED
    assert denied["detail"]["reverses"] == s.action.action_id
    assert denied["at"] >= allowed["at"]


def test_repeated_undo_id_returns_the_admitted_action_after_delivery(scenario):
    s = scenario
    undo_id = f"{s.action.action_id}-undo"
    first = request_undo(s, undo_id=undo_id)
    assert s.sender.claim(first.action_id).claimed
    s.sender.send(first.action_id)
    settle(s.db, first.action_id)

    replay = request_undo(s, undo_id=undo_id)

    assert replay.allowed and replay.replayed
    assert replay.action_id == first.action_id and replay.seq == first.seq
    assert s.gate.receipt(s.receipt_id).rows[0].undo == UndoMark.UNDONE
    assert len(s.router.log) == 2


def test_retry_of_an_unsent_undo_rechecks_the_target(scenario):
    s = scenario
    undo_id = f"{s.action.action_id}-undo"
    assert request_undo(s, undo_id=undo_id).allowed
    s.router.send(replace(s.action, action_id=f"{s.action.action_id}-later", args={"percent": 50}))

    retry = request_undo(s, undo_id=undo_id)

    assert not retry.allowed and retry.reason == Reason.STATE_CHANGED
    assert s.router.percent == 50


def test_retry_of_an_unknown_undo_does_not_need_the_unreachable_target(scenario):
    s = scenario
    undo_id = f"{s.action.action_id}-undo"
    assert request_undo(s, undo_id=undo_id).allowed
    assert s.sender.claim(undo_id).claimed
    s.router.cut()
    assert s.sender.send(undo_id)["state"] == EffectState.SENT

    retry = request_undo(s, undo_id=undo_id)

    assert retry.allowed and retry.replayed
    assert s.gate.receipt(s.receipt_id).rows[0].undo == UndoMark.UNKNOWN
    assert not s.sender.claim(undo_id).claimed
    assert len(s.router.log) == 2


def test_undo_id_cannot_reuse_the_original_action_id(scenario):
    s = scenario

    result = request_undo(s, undo_id=s.action.action_id)

    assert not result.allowed and result.reason == Reason.SAME_ID_DIFFERENT_ARGS
    assert s.gate.receipt(s.receipt_id).rows[0].undo is None
    assert s.router.percent == 25


def test_missing_original_is_an_audited_refusal(scenario):
    s = scenario
    missing_id = f"{s.action.action_id}-missing"

    result = undo(s.gate, s.db, missing_id, mandate_id=s.root,
                  principal=OPERATOR, targets=s.targets)

    assert not result.allowed and result.reason == Reason.NOT_ADMITTED
    denied = s.db.audit.find_one({"action_id": result.action_id, "kind": "deny"})
    assert denied["detail"]["reverses"] == missing_id
    assert denied["run_id"] == s.action.run_id
