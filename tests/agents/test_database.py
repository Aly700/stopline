"""Optional bounded Atlas smoke checks; never touch the demo database.

Run with STOPLINE_TEST_ATLAS=1 uv run pytest tests/agents/test_database.py -o addopts=""
Gate and target implementation coverage still belongs to their packages.
"""

import os
import threading
import time

import pytest

from stopline import db as database_module
from stopline.agents import build_stage
from stopline.agents.planner import ScriptedPlanner
from stopline.agents.worker import run_worker, utcnow
from stopline.contract import HoldPoint, Mode, SendResult, WORKER_ONE


pytestmark = pytest.mark.skipif(os.environ.get("STOPLINE_TEST_ATLAS") != "1", reason="opt-in Atlas smoke check")


@pytest.fixture(scope="module")
def atlas():
    db = database_module.test_database("agents")
    try:
        db.create_collection("runs")
        db.create_collection("tasks")
        db.create_collection("audit", validator={"$jsonSchema": {
            "bsonType": "object", "required": ["kind", "principal", "at"],
        }})
        yield db
    finally:
        database_module.drop_test_database(db)


def wait_for(check, seconds=10):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if value := check():
            return value
        time.sleep(0.1)
    raise AssertionError("Atlas smoke check exceeded its deadline")


def test_saved_flag_worker_and_hold_release_use_real_mongo_updates(atlas):
    class Cluster:
        def __init__(self):
            self.log = []

        def send(self, action):
            evidence = {"action_id": action.action_id, "system": "cluster", "at_epoch": time.time()}
            self.log.append(evidence)
            return SendResult(True, evidence)

    run_id = "atlas-flag"
    target = Cluster()
    plan = ScriptedPlanner().plan(run_id, {}).steps[WORKER_ONE]
    atlas.runs.insert_one({
        "_id": run_id, "mode": Mode.FLAG, "cluster": "mock", "status": "running",
        "root_mandate_id": None, "agent_mandate_id": None, "stop_flag": False,
        "stop_at": None, "stop_at_local": None, "receipt_id": None,
        "planner": "scripted", "created_at": utcnow(),
        "holds": [{"principal": WORKER_ONE, "step": 1, "point": HoldPoint.AFTER_CHECK, "released": False}],
    })
    atlas.tasks.insert_one({
        "_id": f"{run_id}:{WORKER_ONE}", "run_id": run_id, "principal": WORKER_ONE,
        "mandate_id": None, "plan": plan, "position": 0, "status": "running",
        "planner": "scripted", "pid": None, "held_at": None, "note": "Ready.", "updated_at": utcnow(),
        "fixed_rules": {"tools": ["rollback", "restart"],
                        "targets": ["cluster/checkout-api", "cluster/checkout-worker"],
                        "limits": {"max_actions": 2, "max_shift_percent": 0}},
    })
    worker = threading.Thread(target=run_worker, args=(atlas, None, {"cluster": target}, None, run_id, WORKER_ONE), daemon=True)
    stage = build_stage(atlas, None, {"cluster": target}, None, cluster="mock")
    stage.run_id = run_id
    worker.start()
    try:
        wait_for(lambda: atlas.tasks.find_one({"run_id": run_id, "status": "held"}))
        assert stage.press_stop().ok
        wait_for(lambda: atlas.tasks.find_one({"run_id": run_id, "status": "done"}))
        run = atlas.runs.find_one({"_id": run_id})
        assert target.log[1]["at_epoch"] > run["stop_at_local"]
        assert run["stop_at"].tzinfo is not None
    finally:
        stage.press_stop()
        worker.join(timeout=3)
    assert not worker.is_alive()


def test_validation_probe_really_gets_mongo_error_121(atlas):
    stage = build_stage(atlas, None, {}, None, cluster="mock")
    result = stage.validation_rejection()
    if result.pending:
        result = wait_for(lambda: (job.result() if (job := stage._jobs[None, "validation_rejection"]).done() else None))
    assert result.ok and result.detail["code"] == 121
    assert atlas.audit.count_documents({}) == 0
