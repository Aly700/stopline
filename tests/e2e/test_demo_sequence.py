"""The demo's two runs, driven through the same calls the page's keys make."""

import time

from stopline.contract import (
    AUDIT, EFFECTS, RUNS, TASKS, WORKER_ONE, WORKER_TWO, AuditKind, EffectState, Mode, Reason,
    TaskStatus, action_id_for,
)


def until(check, what, seconds=30.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for: {what}")


def press(call, what, seconds=30.0):
    """A key press: repeat while the stage says pending, as the page does."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        result = call()
        if not result.pending:
            assert result.ok, f"{what}: {result.message}"
            return result
        time.sleep(0.2)
    raise AssertionError(f"{what} stayed pending")


def start(system, mode):
    """Key 1 or 2: press once; if the stage says pending, wait for the saved tasks."""
    result = system.stage.start_run(mode)
    assert result.ok, result.message
    until(lambda: system.db[TASKS].count_documents({"run_id": result.run_id}) >= 3, "the run's tasks are saved")
    return result.run_id


def state_of(system, action_id):
    effect = system.db[EFFECTS].find_one({"_id": action_id})
    return effect["state"] if effect else None


def task_of(system, run_id, principal):
    return system.db[TASKS].find_one({"_id": f"{run_id}:{principal}"}) or {}


def workers_gone(system, run_id):
    return all(handle.poll() is not None for (rid, _), handle in system.stage._processes.items() if rid == run_id)


def test_flag_run_lets_an_action_start_after_the_stop(system):
    system.reset()
    run_id = start(system, Mode.FLAG)
    rollback, restart = (action_id_for(run_id, WORKER_ONE, step) for step in (0, 1))
    shift_25, shift_50 = (action_id_for(run_id, WORKER_TWO, step) for step in (0, 1))

    until(lambda: state_of(system, rollback) == EffectState.APPLIED, "rollback applied")
    until(lambda: state_of(system, shift_25) == EffectState.APPLIED, "shift 25 applied")
    until(lambda: task_of(system, run_id, WORKER_ONE).get("status") == TaskStatus.HELD, "worker one held")

    press(system.stage.press_stop, "stop")
    until(lambda: state_of(system, restart) == EffectState.APPLIED, "restart ran after the stop")

    run = system.db[RUNS].find_one({"_id": run_id})
    restarted = system.db[EFFECTS].find_one({"_id": restart})
    assert restarted["evidence"]["at_epoch"] > run["stop_at_local"], "the target's own clock puts it after the stop"
    cluster = system.targets["cluster"].state()["deployments"]
    assert cluster["checkout-api"]["version"] == "v1", "the cluster itself shows the rollback"
    assert cluster["checkout-worker"]["restarted_at"], "the cluster itself shows the restart"
    until(lambda: task_of(system, run_id, WORKER_TWO).get("status") == TaskStatus.STOPPED, "worker two saw the flag")
    assert state_of(system, shift_50) is None
    until(lambda: workers_gone(system, run_id), "workers exited")


def test_stopline_run_refuses_after_the_stop_and_closes_the_receipt_on_evidence(system):
    system.reset()
    run_id = start(system, Mode.STOPLINE)
    rollback, restart = (action_id_for(run_id, WORKER_ONE, step) for step in (0, 1))
    shift_25, shift_50 = (action_id_for(run_id, WORKER_TWO, step) for step in (0, 1))

    until(lambda: state_of(system, rollback) == EffectState.APPLIED, "rollback applied")
    until(lambda: state_of(system, restart) == EffectState.ADMITTED, "restart admitted and held")
    until(lambda: state_of(system, shift_25) == EffectState.SENT, "shift 25 claimed and held")

    press(system.stage.cut_and_kill, "cut and kill")
    assert state_of(system, shift_25) == EffectState.SENT
    assert system.targets["router"].state()["reachable"] is False

    press(system.stage.press_stop, "stop")
    run = system.db[RUNS].find_one({"_id": run_id})
    assert run["receipt_id"]
    until(lambda: state_of(system, restart) == EffectState.CANCELLED, "restart cancelled by the stop")
    stopped = until(lambda: (task_of(system, run_id, WORKER_ONE).get("status") == TaskStatus.STOPPED
                             and task_of(system, run_id, WORKER_ONE)), "worker one refused")
    assert "person:operator" in stopped["note"], "the refusal names who stopped it"

    press(system.stage.resume_worker, "resume worker two")
    refusal = until(lambda: system.db[AUDIT].find_one({"action_id": shift_50, "kind": AuditKind.DENY}),
                    "shift 50 refused")
    assert refusal["reason"] == Reason.REVOKED
    assert state_of(system, shift_50) is None

    view = system.gate.receipt(run["receipt_id"])
    outcomes = {row.action_id: row.outcome for row in view.rows}
    assert view.open
    assert outcomes == {rollback: "completed", restart: "blocked", shift_25: "unknown", shift_50: "blocked"}

    time.sleep(1.0)
    assert system.gate.receipt(run["receipt_id"]).open, "waiting never closes a receipt"

    press(system.stage.restore_and_reconcile, "restore and reconcile")
    until(lambda: not system.gate.receipt(run["receipt_id"]).open, "receipt closed on the router's evidence")
    assert state_of(system, shift_25) == EffectState.APPLIED
    assert system.targets["router"].state()["weights"]["region-b"] == 25
    until(lambda: workers_gone(system, run_id), "workers exited")
