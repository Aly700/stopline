from stopline.contract import EffectState, Mode, WORKER_ONE, WORKER_TWO, action_id_for
from stopline.agents.worker import Worker
import httpx
from .test_stage import rig, task


def prepared_worker(stage, db, targets, monkeypatch, principal=WORKER_ONE, mode=Mode.STOPLINE):
    monkeypatch.setattr(stage, "_spawn", lambda *args: None)
    result = stage.start_run(mode, holds=[])
    assert result.ok and not result.pending
    return Worker(db, stage.gate, targets, stage.sender, result.run_id, principal)


def test_stop_stream_hint_halts_before_the_next_step(rig, monkeypatch):
    stage, db, targets = rig
    worker = prepared_worker(stage, db, targets, monkeypatch)
    original = stage.sender.send

    def send(action_id):
        result = original(action_id)
        worker.listener.stopped.set()
        return result

    monkeypatch.setattr(stage.sender, "send", send)
    worker.run_saved()
    row = task(db, worker.run_id, WORKER_ONE)
    assert row["status"] == "stopped" and row["position"] == 1
    assert not db.effects.find_one({"_id": action_id_for(worker.run_id, WORKER_ONE, 1)})


def test_unknown_send_is_named_in_the_saved_note(rig, monkeypatch):
    stage, db, targets = rig
    worker = prepared_worker(stage, db, targets, monkeypatch)
    notes = []
    original = worker._save

    def save(note, **fields):
        notes.append(note)
        return original(note, **fields)

    monkeypatch.setattr(worker, "_save", save)
    monkeypatch.setattr(stage.sender, "send", lambda action_id: {"_id": action_id, "state": EffectState.SENT})
    worker.run_saved()
    assert any("pending reconciliation" in note for note in notes)


def test_fixed_rules_are_not_refreshed_after_stop(rig, monkeypatch):
    stage, db, targets = rig
    worker = prepared_worker(stage, db, targets, monkeypatch, mode=Mode.FIXED)
    assert stage.press_stop().ok
    # Fixed mode deliberately ignores changes after its initial rule snapshot.
    worker.listener.stopped.set()
    worker.run_saved()
    assert task(db, worker.run_id, WORKER_ONE)["status"] == "done"
    assert len(db.target_log.find({"system": "cluster"})) == 2


def test_stopline_worker_never_calls_a_target_directly(rig, monkeypatch):
    stage, db, targets = rig
    worker = prepared_worker(stage, db, {}, monkeypatch)
    worker.run_saved()
    assert task(db, worker.run_id, WORKER_ONE)["status"] == "done"
    assert len(db.target_log.find({"system": "cluster"})) == 2


def test_claim_connection_failure_keeps_the_unsent_step_saved(rig, monkeypatch):
    stage, db, targets = rig
    worker = prepared_worker(stage, db, targets, monkeypatch)

    def unavailable(action_id):
        raise httpx.ConnectError("sender unavailable")

    monkeypatch.setattr(stage.sender, "claim", unavailable)
    worker.run_saved()
    row = task(db, worker.run_id, WORKER_ONE)
    assert row["position"] == 0 and row["status"] == "stopped"
    assert db.effects.find_one({"_id": action_id_for(worker.run_id, WORKER_ONE, 0)})["state"] == "admitted"
    assert not db.effects.find_one({"_id": action_id_for(worker.run_id, WORKER_ONE, 1)})
    assert db.target_log.find() == []
