import threading

import pytest

from stopline.contract import Admission, Mode, Reason, TARGET_REGION_B, Tool, WORKER_TWO
from .test_stage import eventually, rig, task


def prepare(rig, monkeypatch, mode=Mode.STOPLINE):
    stage, db, targets = rig
    monkeypatch.setattr(stage, "_spawn", lambda *args: None)
    result = stage.start_run(mode)
    assert result.ok and not result.pending
    return stage, db, result.run_id


def test_overreach_is_denied_by_the_gate_for_the_workers_limit(rig, monkeypatch):
    stage, db, run_id = prepare(rig, monkeypatch)
    worker = task(db, run_id, WORKER_TWO)
    result = stage.overreach()
    assert result.ok and not result.pending
    assert result.message == "Worker two asked to shift 90%. Refused: its permission allows at most 50%."
    action_id = result.detail["action_id"]
    assert action_id.startswith(f"{run_id}-worker-2-over-")
    denied = db.audit.find_one({"action_id": action_id, "kind": "deny"})
    assert denied["reason"] == Reason.LIMIT_EXCEEDED
    assert denied["principal"] == WORKER_TWO and denied["mandate_id"] == worker["mandate_id"]
    assert denied["detail"]["tool"] == Tool.SHIFT and denied["detail"]["target"] == TARGET_REGION_B
    assert denied["detail"]["args"] == {"percent": 90}
    assert db.effects.find() == [] and db.send_calls.find() == [] and db.target_log.find() == []
    assert task(db, run_id, WORKER_TWO)["note"] == result.message
    assert task(db, run_id, WORKER_TWO)["plan"] == worker["plan"]


def test_overreach_after_stop_reports_the_gates_stopper_and_time(rig, monkeypatch):
    stage, db, run_id = prepare(rig, monkeypatch)
    assert stage.press_stop().ok
    result = stage.overreach()
    assert result.ok and result.detail["reason"] == Reason.REVOKED
    permission = db.mandates.find_one({"_id": task(db, run_id, WORKER_TWO)["mandate_id"]})
    assert "Worker two asked to shift 90%. Refused:" in result.message
    assert "Operator" in result.message and permission["revoked_at"].isoformat() in result.message
    assert "person:" not in result.message and "mandate" not in result.message
    assert db.audit.find_one({"action_id": result.detail["action_id"], "kind": "deny"})
    assert db.effects.find() == [] and db.send_calls.find() == []


def test_overreach_twice_creates_two_distinct_refusals(rig, monkeypatch):
    stage, db, run_id = prepare(rig, monkeypatch)
    first, second = stage.overreach(), stage.overreach()
    assert first.ok and second.ok
    assert first.detail["action_id"] != second.detail["action_id"]
    assert len(db.audit.find({"run_id": run_id, "kind": "deny"})) == 2


@pytest.mark.parametrize("mode", [Mode.FLAG, Mode.FIXED])
def test_overreach_baseline_explains_why_there_is_no_refusal(rig, monkeypatch, mode):
    stage, db, run_id = prepare(rig, monkeypatch, mode)
    result = stage.overreach()
    assert not result.ok and not result.pending
    assert result.message == "Start a StopLine run first: a baseline run has no check to refuse it."
    assert db.audit.find() == [] and db.effects.find() == []


def test_unexpected_allow_is_reported_without_sending_or_cancelling(rig, monkeypatch):
    stage, db, run_id = prepare(rig, monkeypatch)

    def allow(action):
        db.effects.insert_one({"_id": action.action_id, "state": "admitted"})
        return Admission(True, action.action_id, Reason.OK)

    monkeypatch.setattr(stage.gate, "admit", allow)
    result = stage.overreach()
    assert not result.ok and result.detail["allowed"]
    assert result.message == "Worker two asked to shift 90%. The gate allowed it. It was not sent."
    assert db.effects.find_one({"_id": result.detail["action_id"]})["state"] == "admitted"
    assert db.send_calls.find() == [] and db.target_log.find() == []


def test_two_presses_while_pending_still_create_distinct_actions(rig, monkeypatch):
    stage, db, run_id = prepare(rig, monkeypatch)
    original = stage.gate.admit
    release = threading.Event()

    def slow_admit(action):
        release.wait(3)
        return original(action)

    monkeypatch.setattr(stage.gate, "admit", slow_admit)
    stage.wait_seconds = 0.02
    try:
        assert stage.overreach().pending
        assert stage.overreach().pending
    finally:
        release.set()
        eventually(lambda: all(job.done() for job in stage._jobs.values()))
    denied = db.audit.find({"run_id": run_id, "kind": "deny"})
    assert len(denied) == 2 and denied[0]["action_id"] != denied[1]["action_id"]
