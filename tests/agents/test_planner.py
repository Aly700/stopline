from copy import deepcopy
import threading
import time

from stopline.contract import DEMO_PLAN, Mode, WORKER_ONE, WORKER_TWO, action_id_for
from .test_stage import eventually, rig, start_held, task


def test_saved_planner_reads_tasks_and_rebinds_action_ids(rig):
    from stopline.agents.planner import SavedPlanner

    stage, db, targets = rig
    source = start_held(stage, db, Mode.FLAG)
    saved = SavedPlanner(db, source_run_id=source).plan("next-run", {})
    assert saved.source == "saved"
    assert saved.steps[WORKER_TWO][1]["args"] == {"percent": 50}
    assert saved.steps[WORKER_ONE][0]["action_id"] == action_id_for("next-run", WORKER_ONE, 0)


def test_model_result_is_used_and_recorded(rig):
    from stopline.agents.planner import ModelPlanner, SavedPlanner

    stage, db, targets = rig
    calls = []

    def model(**request):
        calls.append(request)
        return deepcopy(DEMO_PLAN)

    stage.planner = ModelPlanner(model, SavedPlanner(db))
    run_id = start_held(stage, db, Mode.STOPLINE)
    assert calls[0]["timeout_seconds"] == 10
    assert db.runs.find_one({"_id": run_id})["planner"] == "model"
    assert task(db, run_id, WORKER_ONE)["planner"] == "model"


def test_default_ten_second_timeout_uses_saved_plan_and_records_why(rig):
    from stopline.agents.planner import ModelPlanner, SavedPlanner

    stage, db, targets = rig
    release = threading.Event()
    started = threading.Event()

    def model(**request):
        started.set()
        release.wait(15)
        return deepcopy(DEMO_PLAN)

    stage.planner = ModelPlanner(model, SavedPlanner(db))
    before = time.monotonic()
    try:
        result = stage.start_run(Mode.STOPLINE)
        assert result.ok and result.pending
        assert time.monotonic() - before < 2
        assert started.is_set()
        run = eventually(lambda: (row if (row := db.runs.find_one({"_id": result.run_id}))["planner"] == "saved" else None),
                         timeout=12)
        elapsed = time.monotonic() - before
        assert 10 <= elapsed < 12
        assert "timed out" in run["planner_note"]
        assert "saved" in run["planner_note"]
        assert task(db, result.run_id, WORKER_TWO)["planner"] == "saved"
    finally:
        release.set()
    eventually(lambda: task(db, result.run_id, WORKER_ONE)["status"] == "held")
    assert db.runs.find_one({"_id": result.run_id})["planner"] == "saved", "late model result must not replace fallback"


def test_bad_model_output_falls_back_without_widening_worker_scope(rig):
    from stopline.agents.planner import ModelPlanner, SavedPlanner

    stage, db, targets = rig
    wrong = deepcopy(DEMO_PLAN)
    wrong[WORKER_ONE][0]["target"] = "cluster/unrelated-service"
    stage.planner = ModelPlanner(lambda **kwargs: wrong, SavedPlanner(db))
    run_id = start_held(stage, db, Mode.STOPLINE)
    assert db.runs.find_one({"_id": run_id})["planner"] == "saved"
    first = task(db, run_id, WORKER_ONE)
    mandate = db.mandates.find_one({"_id": first["mandate_id"]})
    assert set(mandate["targets"]) == {"cluster/checkout-api", "cluster/checkout-worker"}
