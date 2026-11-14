import multiprocessing
import os
import threading
import time

import pytest

from stopline.contract import DEMO_HOLDS, HoldPoint, Mode, OPERATOR, WORKER_ONE, WORKER_TWO, action_id_for
from .fakes import Database, Gate, Sender, Target


def eventually(check, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("condition did not become true before the deadline")


class ProcessHandle:
    def __init__(self, process):
        self.process = process
        self.pid = process.pid

    def poll(self):
        return self.process.exitcode

    def kill(self):
        self.process.kill()

    def wait(self, timeout=None):
        self.process.join(timeout)
        if self.process.is_alive():
            raise TimeoutError("worker did not exit")


@pytest.fixture
def rig(monkeypatch):
    from stopline.agents.stage import Stage
    from stopline.agents.worker import run_worker

    context = multiprocessing.get_context("spawn")
    with context.Manager() as manager:
        db = Database(manager.dict(), manager.RLock())
        gate = Gate(db)
        targets = {name: Target(db, name) for name in ("cluster", "router")}
        sender = Sender(db, gate, targets)
        stage = Stage(db, gate, targets, sender, cluster="mock")
        processes = []

        def transaction(callback):
            with db.lock:
                before = dict(db.data.items())
                try:
                    return callback(None), 1
                except Exception:
                    db.data.clear()
                    db.data.update(before)
                    raise

        monkeypatch.setattr("stopline.agents.stage.run_transaction", transaction)

        def launch(run_id, principal):
            process = context.Process(target=run_worker,
                                      args=(db, gate, targets, sender, run_id, principal))
            process.start()
            handle = ProcessHandle(process)
            processes.append(handle)
            return handle

        stage._launch_worker = launch
        yield stage, db, targets
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=2)


def task(db, run_id, principal):
    return db.tasks.find_one({"_id": f"{run_id}:{principal}"})


def held(db, run_id, principal):
    row = task(db, run_id, principal)
    return row if row and row["status"] == "held" else None


def start_held(stage, db, mode):
    result = stage.start_run(mode)
    assert result.ok, result
    run_id = result.run_id
    first = eventually(lambda: held(db, run_id, WORKER_ONE))
    second = eventually(lambda: held(db, run_id, WORKER_TWO))
    assert first["pid"] != second["pid"] and first["pid"] != os.getpid()
    return run_id


@pytest.mark.parametrize("mode", [Mode.FLAG, Mode.FIXED])
def test_baseline_restart_is_in_target_log_after_stop_twice(rig, mode):
    stage, db, targets = rig
    for _ in range(2):
        run_id = start_held(stage, db, mode)
        assert stage.press_stop().ok
        restart = action_id_for(run_id, WORKER_ONE, 1)
        evidence = eventually(lambda: db.target_log.find_one({"_id": restart}))
        run = db.runs.find_one({"_id": run_id})
        assert evidence["at_epoch"] > run["stop_at_local"]
        effect = db.effects.find_one({"_id": restart})
        assert effect["admitted_seq"] is None and effect["chain"] == []
        eventually(lambda: task(db, run_id, WORKER_ONE)["status"] == "done")
        eventually(lambda: task(db, run_id, WORKER_TWO)["status"] in {"done", "stopped"})
        eventually(lambda: all(p.poll() is not None for p in stage._processes.values()))


def test_stopline_restart_refusal_names_stopper_and_time(rig):
    stage, db, targets = rig
    run_id = start_held(stage, db, Mode.STOPLINE)
    assert stage.press_stop().ok
    first = eventually(lambda: (row if (row := task(db, run_id, WORKER_ONE))["status"] == "stopped" else None))
    run = db.runs.find_one({"_id": run_id})
    receipt = db.receipts.find_one({"_id": run["receipt_id"]})
    assert OPERATOR in first["note"]
    assert receipt["stopped_at"].isoformat() in first["note"]
    assert not db.target_log.find_one({"_id": action_id_for(run_id, WORKER_ONE, 1)})
    assert db.mandates.find_one({"_id": run["root_mandate_id"]})["status"] == "active"
    assert db.mandates.find_one({"_id": run["agent_mandate_id"]})["status"] == "revoked"


def test_kill_resume_skips_sent_shift_and_gets_refused_for_fifty(rig):
    for _ in range(2):
        kill_resume_sequence(rig)


def kill_resume_sequence(rig):
    stage, db, targets = rig
    run_id = start_held(stage, db, Mode.STOPLINE)
    pid = task(db, run_id, WORKER_TWO)["pid"]
    result = stage.cut_and_kill()
    assert result.ok, result
    killed = eventually(lambda: (row if (row := task(db, run_id, WORKER_TWO))["status"] == "killed" else None))
    assert killed["position"] == 0
    twenty_five = action_id_for(run_id, WORKER_TWO, 0)
    assert db.effects.find_one({"_id": twenty_five})["state"] == "sent"
    assert db.target_log.find_one({"_id": twenty_five})
    assert stage.press_stop().ok
    assert stage.resume_worker().ok
    resumed = eventually(lambda: (row if (row := task(db, run_id, WORKER_TWO))["status"] == "stopped" else None))
    assert resumed["pid"] != pid and resumed["position"] == 1
    assert OPERATOR in resumed["note"]
    assert not db.effects.find_one({"_id": action_id_for(run_id, WORKER_TWO, 1)})
    assert len([row for row in db.target_log.find({"system": "router"})
                if row["action_id"].startswith(run_id)]) == 1
    assert len(db.send_calls.find({"action_id": twenty_five})) == 1
    assert stage.restore_and_reconcile().ok
    assert db.effects.find_one({"_id": twenty_five})["state"] == "applied"
    eventually(lambda: all(p.poll() is not None for p in stage._processes.values()))


def test_factory_is_exported_and_default_holds_are_not_mutated(rig):
    from stopline.agents import build_stage
    stage, db, targets = rig
    assert callable(build_stage)
    start_held(stage, db, Mode.FLAG)
    assert stage.press_stop().ok
    assert all(not hold["released"] for hold in DEMO_HOLDS[Mode.FLAG])


def test_another_start_does_not_lose_the_active_run(rig):
    stage, db, targets = rig
    run_id = start_held(stage, db, Mode.FLAG)
    assert not stage.start_run(Mode.STOPLINE).ok
    assert stage.run_id == run_id
    assert stage.press_stop().ok
    assert db.runs.find_one({"_id": run_id})["stop_flag"]


@pytest.mark.parametrize("point", list(HoldPoint))
def test_each_hold_is_reached_saved_and_released_by_stop(rig, point):
    stage, db, targets = rig
    result = stage.start_run(Mode.STOPLINE, holds=[{
        "principal": WORKER_ONE, "step": 0, "point": point, "released": False,
    }])
    row = eventually(lambda: held(db, result.run_id, WORKER_ONE))
    assert row["held_at"] == {"step": 0, "point": point}
    assert row["position"] == 0 and row["plan"]
    assert stage.press_stop().ok
    eventually(lambda: task(db, result.run_id, WORKER_ONE)["status"] in {"stopped", "done"})
    assert all(h["released"] for h in db.runs.find_one({"_id": result.run_id})["holds"])


def test_production_launcher_passes_database_and_sender_url(rig, monkeypatch):
    from stopline.agents.stage import Stage
    stage, db, targets = rig
    calls = []
    monkeypatch.delenv("STOPLINE_SENDER_URL", raising=False)
    monkeypatch.setattr("subprocess.Popen", lambda argv, **kwargs: calls.append((argv, kwargs)))
    Stage._launch_worker(stage, "r-test", WORKER_ONE)
    argv, options = calls[0]
    assert argv[1:] == ["-m", "stopline.agents.worker", "--run", "r-test", "--principal", WORKER_ONE]
    assert options["env"]["STOPLINE_DATABASE"] == db.name
    assert options["env"]["STOPLINE_SENDER_URL"] == "http://127.0.0.1:8765"


def test_overlapping_starts_reserve_one_run_before_scheduling_work(rig, monkeypatch):
    stage, db, targets = rig
    entered = threading.Event()
    release = threading.Event()
    original = stage._command
    results = []

    def delayed(name, *args, **kwargs):
        if name == "start_run":
            entered.set()
            release.wait(3)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(stage, "_command", delayed)
    first = threading.Thread(target=lambda: results.append(stage.start_run(Mode.FLAG)))
    first.start()
    assert entered.wait(1)
    reserved = stage.run_id
    # Release from another thread so a broken second start cannot block this test.
    timer = threading.Timer(0.2, release.set)
    timer.start()
    second = stage.start_run(Mode.STOPLINE)
    first.join(3)
    timer.join()
    assert second.pending and second.run_id == reserved
    assert stage.run_id == reserved
    assert results[0].run_id == reserved
    assert len(db.runs.find()) == 1
