"""Random stops against one long-lived system.

Run: uv run pytest tests/soak -s -o addopts=""
STOPLINE_SOAK_RUNS defaults to 12; STOPLINE_SOAK_SEED defaults to 20261114.
Replay one StopLine choice with STOPLINE_SOAK_RUNS=1 STOPLINE_SOAK_SEED=<seed>
and -k stopline. Seeds reproduce choices, not operating-system scheduling.
For a flag choice, set STOPLINE_SOAK_SEED to its printed seed minus 100000
and select -k flag-<printed-seed>.
"""

import os

# Set these before importing any stopline module, including through the helper.
os.environ["STOPLINE_PAGE_PORT"] = "8805"
os.environ["STOPLINE_ROUTER_PORT"] = "8806"

import json
import random
import time
from collections import Counter

import pytest

from soak_helpers import (
    assert_accounting, assert_reset, assert_targets, press, snapshot, system,
    until, worker_handles,
)
from stopline.contract import (
    DEMO_PLAN, EFFECTS, TASKS, HoldPoint, Mode, WORKER_ONE, WORKER_TWO,
)
from stopline.targets import reset_all


RUNS = int(os.environ.get("STOPLINE_SOAK_RUNS", "12"))
SEED = int(os.environ.get("STOPLINE_SOAK_SEED", "20261114"))
if RUNS < 1:
    raise ValueError("STOPLINE_SOAK_RUNS must be positive")


def choice(mode, seed):
    rng = random.Random(seed)
    holds = []
    if rng.choice([False, True]):
        worker = rng.choice([WORKER_ONE, WORKER_TWO])
        points = list(HoldPoint)
        if mode == Mode.FLAG:
            points.remove(HoldPoint.AFTER_CLAIM)
        holds.append({"principal": worker, "step": rng.randrange(len(DEMO_PLAN[worker])),
                      "point": rng.choice(points), "released": False})
    return holds, rng.uniform(0, 6)


CASES = [(Mode.STOPLINE, SEED + i) for i in range(RUNS)]
CASES += [(Mode.FLAG, SEED + 100_000 + i) for i in range(4)]


@pytest.mark.parametrize("mode,seed", CASES, ids=[f"{mode}-{seed}" for mode, seed in CASES])
def test_any_moment(system, mode, seed):
    holds, delay = choice(mode, seed)
    hold_text = ",".join(f"{h['principal']}/{h['step']}/{h['point']}" for h in holds) or "none"
    row = {"mode": str(mode), "seed": seed, "delay": delay, "hold": hold_text,
           "run_id": None, "effects": 0, "states": {}, "denies": 0,
           "late": 0, "in_flight": [], "result": "FAIL"}
    system.results.append(row)
    print(f"\nchoice mode={mode} seed={seed} delay={delay:.6f}s hold={hold_text}", flush=True)
    started = time.monotonic()
    data = None
    tasks_before_reconcile = None
    try:
        assert all(p.poll() is not None for p in system.stage._processes.values()), "a previous worker leaked"
        assert_reset(system.targets)
        wait_seconds = system.stage.wait_seconds
        system.stage.wait_seconds = 0.01
        try:
            result = system.stage.start_run(mode, holds=holds)
        finally:
            system.stage.wait_seconds = wait_seconds
        assert result.ok, result.message
        run_id = row["run_id"] = result.run_id
        # start_run returns pending quickly, so task persistence is observed during startup.
        until(lambda: system.db[TASKS].count_documents({"run_id": run_id}) == 3,
              "three saved tasks", system.deadline)
        saved_at = time.monotonic()
        time.sleep(max(0, saved_at + delay - time.monotonic()))
        row["stop_requested_after"] = time.monotonic() - saved_at
        press(system.stage.press_stop, "stop", system.deadline)

        def exited():
            handles = worker_handles(system, run_id)
            return len(handles) == 2 and all(p.poll() is not None for p in handles.values())

        until(exited, "both worker processes exited", system.deadline)
        assert all(p.returncode == 0 for p in worker_handles(system, run_id).values()), "worker crashed"
        # Reconciliation changes worker two's note; retain its persisted refusal first.
        tasks_before_reconcile = list(system.db[TASKS].find({"run_id": run_id}))
        press(system.stage.restore_and_reconcile, "restore and reconcile", system.deadline)
        until(lambda: system.db[EFFECTS].count_documents({"run_id": run_id, "state": "sent"}) == 0,
              "no outstanding sends", system.deadline)
        data = snapshot(system, run_id, tasks_before_reconcile)
        row["effects"] = len(data["effects"])
        row["states"] = dict(Counter(e["state"] for e in data["effects"]))
        row["denies"] = sum(a["kind"] == "deny" for a in data["audit"])
        assert_accounting(data, mode)
        row["late"], row["in_flight"] = assert_targets(data, mode)
        assert len(worker_handles(system, run_id)) == 2
        assert all(p.poll() is not None for p in worker_handles(system, run_id).values())
        assert data["targets"]["router"]["reachable"], "router did not recover"
        # Workers are gone: verify quiescence before erasing target evidence for the next run.
        time.sleep(0.1)
        assert snapshot(system, run_id, tasks_before_reconcile) == data, "records or targets changed after settlement"
        reset_all(system.targets)
        assert_reset(system.targets)
        row["result"] = "PASS"
    except Exception as error:
        # Only stored records and target state are included; never serialize a database client.
        try:
            # Refresh even after a reset failure so diagnostics show the failed state.
            data = snapshot(system, row["run_id"] or system.stage.run_id, tasks_before_reconcile)
        except Exception:
            data = data or {"snapshot": "unavailable"}
        row["failure"] = str(error) if isinstance(error, AssertionError) else type(error).__name__
        row["records"] = data
        print("failure evidence " + json.dumps(row, default=str, sort_keys=True), flush=True)
        if isinstance(error, AssertionError):
            raise
        raise AssertionError(f"seed={seed}: {type(error).__name__}") from None
    finally:
        row["seconds"] = time.monotonic() - started
        print(f"result {mode} seed={seed} {row['result']} effects={row['effects']} "
              f"states={json.dumps(row['states'], sort_keys=True)} denies={row['denies']} "
              f"late={row['late']} in_flight={json.dumps(row['in_flight'])} "
              f"elapsed={row['seconds']:.2f}s", flush=True)
