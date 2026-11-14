"""Process lifecycle and independent checks of persisted and target evidence."""

from copy import deepcopy
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

import pytest
import uvicorn
from fastapi import FastAPI

from stopline import db as storage
from stopline.agents import build_stage
from stopline.contract import (
    AUDIT, EFFECTS, RECEIPTS, RUNS, TASKS, PAGE_PORT, ROUTER_PORT,
    TERMINAL_STATES, Mode, WORKER_ONE, WORKER_TWO,
)
from stopline.gate import build_gate
from stopline.targets import build_sender, build_targets, reset_all
from stopline.wire import sender_routes, target_routes


def until(check, what, deadline, seconds=20):
    end = min(deadline, time.monotonic() + seconds)
    while time.monotonic() < end:
        value = check()
        if value:
            return value
        time.sleep(0.025)
    raise AssertionError(f"timed out waiting for {what}")


def press(call, what, deadline):
    def completed():
        result = call()
        assert result.ok, f"{what}: {result.message}"
        return result if not result.pending else None

    return until(completed, what, deadline)


def worker_handles(system, run_id):
    return {principal: process for (rid, principal), process in system.stage._processes.items()
            if rid == run_id}


def port_open(port):
    with socket.socket() as probe:
        probe.settimeout(0.1)
        return probe.connect_ex(("127.0.0.1", port)) == 0


@pytest.fixture(scope="session")
def system():
    assert (PAGE_PORT, ROUTER_PORT) == (8805, 8806), "run tests/soak in its own pytest process"
    assert not port_open(PAGE_PORT) and not port_open(ROUTER_PORT), "soak ports are already occupied"
    started = time.monotonic()
    database = router = server = thread = stage = None
    results = []
    saved_env = {key: os.environ.get(key) for key in ("STOPLINE_TARGETS_URL", "STOPLINE_SENDER_URL")}
    # The parent owns the mock; child workers reach it through HTTP.
    os.environ.pop("STOPLINE_TARGETS_URL", None)
    with tempfile.TemporaryDirectory(prefix=".soak-", dir=Path(__file__).resolve().parents[2]) as directory:
        try:
            database = storage.test_database("soak")
            router = subprocess.Popen(
                [sys.executable, "-m", "stopline.targets.router", "--port", str(ROUTER_PORT),
                 "--store", str(Path(directory) / "router.sqlite")],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            until(lambda: port_open(ROUTER_PORT), "router startup", started + 15)
            assert router.poll() is None, "router exited during startup"
            gate = build_gate(database)
            gate.setup()
            targets = build_targets("mock", router_url=f"http://127.0.0.1:{ROUTER_PORT}")
            sender = build_sender(gate, targets)
            stage = build_stage(database, gate, targets, sender, cluster="mock")
            app = FastAPI()
            app.include_router(sender_routes(sender))
            app.include_router(target_routes("cluster", targets["cluster"]))
            server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PAGE_PORT, log_level="critical"))
            thread = threading.Thread(target=server.run, daemon=True)
            thread.start()
            until(lambda: server.started and port_open(PAGE_PORT), "sender startup", started + 25)
            os.environ["STOPLINE_TARGETS_URL"] = f"http://127.0.0.1:{PAGE_PORT}"
            os.environ["STOPLINE_SENDER_URL"] = f"http://127.0.0.1:{PAGE_PORT}"
            reset_all(targets)
            # Extra runs get extra time; the default session has a bounded four-minute budget.
            count = int(os.environ.get("STOPLINE_SOAK_RUNS", "12"))
            yield SimpleNamespace(db=database, targets=targets, stage=stage, results=results,
                                  deadline=started + max(225, (count + 4) * 14))
        except Exception as error:
            if isinstance(error, AssertionError):
                raise
            raise AssertionError(f"soak system failed ({type(error).__name__})") from None
        finally:
            print("\nmode      seed       delay  applied rejected cancelled denies late in-flight result seconds")
            for row in results:
                print(f"{row['mode']:<9} {row['seed']:<10} {row['delay']:5.2f} "
                      f"{row['states'].get('applied', 0):7} {row['states'].get('rejected', 0):8} "
                      f"{row['states'].get('cancelled', 0):9} {row['denies']:6} "
                      f"{row['late']:4} {len(row['in_flight']):9} "
                      f"{row['result']:<6} {row.get('seconds', 0):7.2f}")
            baseline = [r for r in results if r["mode"] == Mode.FLAG]
            print(f"flag actions after stop: {sum(r['late'] for r in baseline)} "
                  f"across {sum(r['late'] > 0 for r in baseline)}/{len(baseline)} runs")
            cleanup_errors = []

            def cleanup(call):
                try:
                    call()
                except Exception as error:
                    cleanup_errors.append(type(error).__name__)

            try:
                if stage:
                    # Startup must finish before enumerating workers: it may still spawn one.
                    for future in list(stage._jobs.values()):
                        cleanup(lambda: future.result(timeout=35))
                    for process in list(stage._processes.values()):
                        if process.poll() is None:
                            cleanup(process.kill)
                        cleanup(lambda: process.wait(timeout=3))
                if server:
                    server.should_exit = True
                if thread:
                    cleanup(lambda: thread.join(timeout=3))
                    if thread.is_alive():
                        cleanup_errors.append("sender thread still alive")
                if router:
                    cleanup(router.terminate)
                    try:
                        router.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        cleanup(router.kill)
                        cleanup(lambda: router.wait(timeout=3))
            finally:
                if database is not None:
                    cleanup(lambda: storage.drop_test_database(database))
                for key, value in saved_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
            print(f"soak session elapsed: {time.monotonic() - started:.2f}s", flush=True)
            assert not cleanup_errors, f"soak cleanup failed: {cleanup_errors}"


def assert_reset(targets):
    router, cluster = targets["router"].state(), targets["cluster"].state()
    assert router["reachable"] and cluster["reachable"], "target unreachable between runs"
    assert router["weights"] == {"region-a": 100, "region-b": 0}
    assert router["last_action_id"] is None and router["log"] == []
    assert cluster["log"] == []
    assert cluster["deployments"] == initial_deployments(), "cluster did not reset"


def initial_deployments():
    return {name: {"image": f"stopline/checkout:{version}", "version": version,
                   "revision": 1, "restarted_at": None, "action_id": None, "ready": True}
            for name, version in (("checkout-api", "v2"), ("checkout-worker", "v1"))}


def snapshot(system, run_id, tasks_before_reconcile=None):
    return {
        "run": system.db[RUNS].find_one({"_id": run_id}),
        "effects": list(system.db[EFFECTS].find({"run_id": run_id}).sort("_id", 1)),
        "receipts": list(system.db[RECEIPTS].find({"run_id": run_id}).sort("_id", 1)),
        "audit": list(system.db[AUDIT].find({"run_id": run_id}).sort("_id", 1)),
        "tasks": list(system.db[TASKS].find({"run_id": run_id}).sort("_id", 1)),
        "tasks_before_reconcile": tasks_before_reconcile,
        "targets": {name: target.state() for name, target in system.targets.items()},
    }


def assert_accounting(data, mode):
    run, effects, audit = data["run"], data["effects"], data["audit"]
    assert run["stop_flag"] and run["status"] == "stopped" and run["stop_at_local"] is not None
    assert all(h["released"] for h in run["holds"]), "stop did not release a hold"
    assert all(e["state"] in TERMINAL_STATES for e in effects), "effect not terminal"
    by_id = {e["_id"]: e for e in effects}
    tasks = [t for t in data["tasks_before_reconcile"] if t["principal"] in (WORKER_ONE, WORKER_TWO)]
    assert {t["principal"] for t in tasks} == {WORKER_ONE, WORKER_TWO}
    steps = {s["action_id"]: s for task in tasks for s in task["plan"]}
    assert set(by_id) <= steps.keys(), "effect outside the saved tasks"
    for effect in effects:
        step = steps[effect["_id"]]
        assert all(effect[k] == step[k] for k in ("tool", "target", "args"))
    for task in tasks:
        position, steps_for_task = task["position"], task["plan"]
        assert task["status"] in ("done", "stopped"), f"worker did not finish: {task}"
        assert 0 <= position <= len(steps_for_task)
        assert all(s["action_id"] in by_id for s in steps_for_task[:position]), "worker skipped an effect"
        if task["status"] == "done":
            assert position == len(steps_for_task)
        else:
            note = task["note"]
            assert note.startswith(("Refused:", "Stopped:", "Heard the stop;")), f"unexpected worker exit: {task}"
            if mode == Mode.STOPLINE and note.startswith("Refused:"):
                action_id = steps_for_task[position]["action_id"]
                if action_id not in by_id:
                    denies = [a for a in audit if a["kind"] == "deny" and a["action_id"] == action_id]
                    assert denies, f"missing deny for attempted admission {action_id}: {note}"
    if mode == Mode.FLAG:
        return
    assert len(data["receipts"]) == 1, "exactly one stop receipt required"
    receipt = data["receipts"][0]
    assert receipt["_id"] == run["receipt_id"] and receipt["status"] == "closed"
    assert receipt["closed_at"] is not None and receipt["closed_at"] >= receipt["stopped_at"]
    assert run["stop_at"] == receipt["stopped_at"]
    stop_seq = receipt["stop_seq"]
    assert all(type(e["admitted_seq"]) is int and e["admitted_seq"] < stop_seq for e in effects), "admission at/after stop"
    ordered = sorted(effects, key=lambda e: e["admitted_seq"])
    assert receipt["covered"] == [e["_id"] for e in ordered if e["admitted_seq"] < stop_seq], "receipt coverage differs"
    allows = [a for a in audit if a["kind"] == "allow"]
    assert len(allows) == len(effects) and {a["action_id"] for a in allows} == set(by_id)
    assert all(a["seq"] == by_id[a["action_id"]]["admitted_seq"] < stop_seq for a in allows)
    stops = [a for a in audit if a["kind"] == "stop"]
    assert len(stops) == 1 and stops[0]["seq"] == stop_seq
    assert stops[0]["detail"]["covered"] == receipt["covered"]
    for denial in (a for a in audit if a["kind"] == "deny"):
        assert denial["action_id"] in steps, "denial outside saved tasks"
        if denial["action_id"] not in by_id:
            assert denial["reason"] == "revoked" and denial["seq"] >= stop_seq
            assert denial["at"] >= receipt["stopped_at"]
            assert denial["detail"]["revoked_at_seq"] == stop_seq


def assert_targets(data, mode):
    effects = {e["_id"]: e for e in data["effects"]}
    late, in_flight = 0, []
    stop_at = data["run"]["stop_at_local"]
    receipt = data["receipts"][0] if mode == Mode.STOPLINE else None
    for name, target in data["targets"].items():
        assert target["reachable"], f"{name} unreachable"
        log = target["log"]
        assert [r["log_id"] for r in log] == sorted({r["log_id"] for r in log}), "target order or duplicate log entry"
        assert len({r["action_id"] for r in log}) == len(log), "duplicate target action"
        records = {r["action_id"]: r for r in log}
        expected = {key: e for key, e in effects.items() if e["target"].split("/")[0] == name}
        assert records.keys() <= expected.keys(), "target action without a recorded effect, or a previous run leaked"
        assert {key for key, r in records.items() if r["applied"]} == {
            key for key, e in expected.items() if e["state"] == "applied"
        }, "target execution disagrees with terminal effects"
        for action_id, effect in expected.items():
            record = records.get(action_id)
            if effect["state"] == "cancelled":
                assert record is None, f"cancelled action reached target: {action_id}"
            elif effect["state"] == "rejected":
                assert record and record["applied"] is False and record.get("fenced"), "rejection lacks target fence"
                assert record == effect["evidence"], "rejection evidence differs from target log"
            else:
                assert record and record == effect["evidence"], "applied evidence differs from target log"
                after_stop = record["at_epoch"] > stop_at
                late += int(after_stop)
                if mode == Mode.STOPLINE:
                    sent_before = effect["sent_at"] is not None and effect["sent_at"] < receipt["stopped_at"]
                    if after_stop and sent_before:
                        assert action_id in receipt["covered"], "in-flight action missing from receipt"
                        in_flight.append({"action_id": action_id, "after_stop_seconds": record["at_epoch"] - stop_at})
                    assert record["at_epoch"] <= stop_at + 1 or sent_before, "target applied too late without a pre-stop send"
        applied = [r for r in log if r["applied"]]
        if name == "router":
            weights, last_id = {"region-a": 100, "region-b": 0}, None
            for record in applied:
                effect = effects[record["action_id"]]
                assert effect["tool"] == "shift_traffic" and effect["target"] == "router/region-b"
                assert record["before"] == weights, "router history is discontinuous"
                percent = effect["args"]["percent"]
                weights = {"region-a": 100 - percent, "region-b": percent}
                assert record["weights"] == weights
                last_id = record["action_id"]
            assert target["weights"] == weights and target["last_action_id"] == last_id
        else:
            deployments = initial_deployments()
            for record in applied:
                effect = effects[record["action_id"]]
                deployment = effect["target"].split("/", 1)[1]
                assert record["deployment"] == deployment
                assert record["before"] == deployments[deployment], "cluster history is discontinuous"
                expected_after = deepcopy(deployments[deployment])
                if effect["tool"] == "rollback":
                    version = effect["args"]["to_version"]
                    expected_after.update(version=version, image=f"stopline/checkout:{version}")
                else:
                    assert effect["tool"] == "restart"
                    assert record["after"]["restarted_at"]
                    expected_after["restarted_at"] = record["after"]["restarted_at"]
                expected_after.update(action_id=effect["_id"], revision=expected_after["revision"] + 1)
                assert record["after"] == expected_after
                deployments[deployment] = expected_after
            assert target["deployments"] == deployments, "cluster differs from applied effects in target order"
    return late, in_flight
