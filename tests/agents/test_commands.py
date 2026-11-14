import threading
import time

import pytest

from stopline.contract import Admission, EffectState, Mode, OPERATOR, Reason, StageResult, WORKER_ONE, action_id_for
from .test_stage import eventually, rig, start_held, task


@pytest.mark.parametrize("method,implementation,args", [
    ("start_run", "_start", (Mode.FLAG,)),
    ("press_stop", "_stop", ()),
    ("cut_and_kill", "_cut_and_kill", ()),
    ("resume_worker", "_resume", ()),
    ("restore_and_reconcile", "_reconcile", ()),
    ("undo", "_undo", ()),
    ("validation_rejection", "_validate", ()),
    ("overreach", "_overreach", ()),
    ("status", None, ()),
])
def test_every_stage_call_returns_pending_within_two_seconds(rig, monkeypatch, method, implementation, args):
    stage, db, targets = rig
    event = threading.Event()

    def blocked(*args, **kwargs):
        event.wait(5)
        return StageResult(True, "Finished the command.")

    if implementation:
        monkeypatch.setattr(stage, implementation, blocked)
    else:
        stage.run_id = "waiting"
        monkeypatch.setattr(db, "runs", type("BlockedCollection", (), {
            "find_one": lambda *args, **kwargs: (event.wait(5) and None),
        })())
    try:
        before = time.monotonic()
        result = getattr(stage, method)(*args)
        assert time.monotonic() - before < 2
        assert result["pending"] if method == "status" else result.pending
    finally:
        event.set()
        eventually(lambda: all(job.done() for job in stage._jobs.values()))


def test_pending_stop_is_not_submitted_twice_and_holds_wait_for_commit(rig, monkeypatch):
    stage, db, targets = rig
    run_id = start_held(stage, db, Mode.STOPLINE)
    event = threading.Event()
    calls = []
    original = stage.gate.stop

    def slow_stop(**kwargs):
        calls.append(kwargs)
        event.wait(5)
        return original(**kwargs)

    monkeypatch.setattr(stage.gate, "stop", slow_stop)
    stage.wait_seconds = 0.02
    try:
        assert stage.press_stop().pending
        assert stage.press_stop().pending
        assert len(calls) == 1
        assert not any(h["released"] for h in db.runs.find_one({"_id": run_id})["holds"])
    finally:
        event.set()
    eventually(lambda: db.runs.find_one({"_id": run_id})["stop_flag"])
    eventually(lambda: all(h["released"] for h in db.runs.find_one({"_id": run_id})["holds"]))
    stage.press_stop()
    assert len(calls) == 1


def test_cut_pending_message_tells_the_operator_to_press_k_again(rig, monkeypatch):
    stage, db, targets = rig
    monkeypatch.setattr(stage, "_spawn", lambda *args: None)
    run_id = stage.start_run(Mode.STOPLINE).run_id

    class NotYetSent:
        def poll(self):
            return None

        def kill(self):
            raise AssertionError("must not kill before the target took the action")

    from stopline.contract import WORKER_TWO
    stage._processes[run_id, WORKER_TWO] = NotYetSent()
    before = time.monotonic()
    result = stage.cut_and_kill()
    assert time.monotonic() - before < 2
    expected = "Worker two has not sent yet. Press K again in a moment."
    job = stage._jobs[run_id, "cut_and_kill"]
    eventually(job.done)
    assert result.pending and result.message == expected
    assert job.result().pending and job.result().message == expected


def test_undo_uses_the_operators_root_and_the_sender(rig, monkeypatch):
    stage, db, targets = rig
    run_id = start_held(stage, db, Mode.STOPLINE)
    assert stage.press_stop().ok
    run = db.runs.find_one({"_id": run_id})
    original_id = action_id_for(run_id, WORKER_ONE, 0)
    calls = []

    def undo(action_id, *, mandate_id, principal, targets, undo_id):
        calls.append((action_id, mandate_id, principal, undo_id))
        original = db.effects.find_one({"_id": action_id})
        db.effects.insert_one({**original, "_id": undo_id, "mandate_id": mandate_id,
                               "principal": principal, "reverses": action_id, "state": "admitted"})
        return Admission(True, undo_id, Reason.OK)

    monkeypatch.setattr(stage.gate, "undo", undo, raising=False)
    assert stage.undo().ok
    assert (original_id, run["root_mandate_id"], OPERATOR, f"{original_id}-undo") in calls
    assert db.target_log.find_one({"_id": f"{original_id}-undo"})
    assert db.effects.find_one({"_id": original_id})["undo"] == "undone"
    assert stage.undo().ok
    assert len([call for call in calls if call[0] == original_id]) == 1


def test_undo_marks_tools_without_an_inverse(rig, monkeypatch):
    stage, db, targets = rig
    run_id = start_held(stage, db, Mode.STOPLINE)
    assert stage.press_stop().ok
    monkeypatch.setattr(stage.gate, "undo", lambda action_id, **kwargs: Admission(False, action_id, Reason.NO_UNDO), raising=False)
    assert stage.undo().ok
    applied = db.effects.find({"run_id": run_id, "state": EffectState.APPLIED})
    assert applied and all(row["undo"] == "no_undo" for row in applied)


def test_validation_rejection_reports_error_121_without_an_audit_insert(rig):
    stage, db, targets = rig
    before = db.audit.find()
    result = stage.validation_rejection()
    assert result.ok and not result.pending
    assert result.detail["code"] == 121
    assert "rejected" in result.message.lower()
    assert db.audit.find() == before


def test_missing_validator_rolls_back_the_probe(rig, monkeypatch):
    from .fakes import Collection

    stage, db, targets = rig
    original = Collection.insert_one

    def no_validator(collection, doc, **kwargs):
        if collection.name == "audit":
            db.data["audit", doc["_id"]] = doc
        else:
            return original(collection, doc, **kwargs)

    monkeypatch.setattr(Collection, "insert_one", no_validator)
    result = stage.validation_rejection()
    assert not result.ok and "rolled back" in result.message
    assert db.audit.find() == []


def test_rejected_undo_is_a_known_refusal_not_an_unknown_outcome(rig, monkeypatch):
    stage, db, targets = rig
    run_id = start_held(stage, db, Mode.STOPLINE)
    assert stage.press_stop().ok
    original_id = action_id_for(run_id, WORKER_ONE, 0)

    def undo(action_id, **kwargs):
        return Admission(True, kwargs["undo_id"], Reason.OK)

    from stopline.contract import Claim
    monkeypatch.setattr(stage.gate, "undo", undo, raising=False)
    monkeypatch.setattr(stage.sender, "claim", lambda action_id: Claim(True, action_id, "sent"))
    monkeypatch.setattr(stage.sender, "send", lambda action_id: {
        "_id": action_id, "state": "rejected", "evidence": {"reason": Reason.STATE_CHANGED},
    })
    result = stage.undo()
    assert not result.ok and not result.pending
    assert result.detail["refusals"][0]["reason"] == Reason.STATE_CHANGED
    assert db.effects.find_one({"_id": original_id}).get("undo") is None


def test_reconcile_updates_the_original_undo_mark(rig):
    stage, db, targets = rig
    run_id = start_held(stage, db, Mode.STOPLINE)
    assert stage.press_stop().ok
    original_id = action_id_for(run_id, WORKER_ONE, 0)
    original = db.effects.find_one({"_id": original_id})
    undo_id = original_id + "-undo"
    db.effects.insert_one({**original, "_id": undo_id, "state": "sent", "reverses": original_id})
    db.effects.update_one({"_id": original_id}, {"$set": {"undo": "undo_unknown"}})
    db.target_log.insert_one({"_id": undo_id, "action_id": undo_id, "system": "cluster", "at_epoch": time.time()})
    assert stage.restore_and_reconcile().ok
    assert db.effects.find_one({"_id": original_id})["undo"] == "undone"
