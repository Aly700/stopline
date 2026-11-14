"""The page's bounded commands and the operator → repair → workers tree."""

from __future__ import annotations

from concurrent.futures import Future, TimeoutError as FutureTimeout
from copy import deepcopy
from dataclasses import asdict
import os
import secrets
import subprocess
import sys
import threading
import time

import httpx
from pymongo.errors import OperationFailure

from stopline.contract import (
    Action, DEMO_HOLDS, DEMO_PLAN, EffectState, Mode, OPERATOR, PAGE_PORT, REPAIR_AGENT,
    ROUTER, Reason, StageResult, TARGET_REGION_B, TaskStatus, TargetTimeout, TargetUnreachable,
    Tool, UndoMark, WORKER_ONE, WORKER_TWO, action_id_for, system_of,
)
from stopline.db import run_transaction
from .planner import ScriptedPlanner
from .repair import RepairAgent
from .worker import refusal_note, utcnow


WORKERS = (WORKER_ONE, WORKER_TWO)
CUT_PENDING = "Worker two has not sent yet. Press K again in a moment."


class MissingValidator(Exception):
    pass


class Stage:
    def __init__(self, db, gate, targets, sender, cluster="kind"):
        self.db, self.gate, self.targets, self.sender = db, gate, targets, sender
        self.cluster = cluster
        self.planner = ScriptedPlanner()
        self.run_id = None
        self._starting = False
        self._overreach_count = 0
        self._processes = {}
        self._jobs = {}
        self._job_lock = threading.Lock()
        self._actions = threading.Lock()
        self._cached_status = {"run": None, "tasks": [], "receipt_id": None}
        self.wait_seconds = 1.5

    def _command(self, name, operation, run_id=None, *, serial=True, job_key=None):
        """Do slow work once in the background; repeated pending keys reuse it."""
        key = (run_id, job_key or name)
        with self._job_lock:
            future = self._jobs.get(key)
            if future is None or future.done():
                future = Future()
                self._jobs[key] = future

                def execute():
                    try:
                        if serial:
                            with self._actions:
                                result = operation()
                        else:
                            result = operation()
                    except Exception as error:
                        result = StageResult(False, f"{name.replace('_', ' ').capitalize()} failed ({type(error).__name__}).",
                                             run_id=run_id)
                    future.set_result(result)

                threading.Thread(target=execute, daemon=True, name=f"stage-{name}").start()
        try:
            return future.result(timeout=self.wait_seconds)
        except FutureTimeout:
            message = CUT_PENDING if name == "cut_and_kill" else f"{name.replace('_', ' ').capitalize()} is pending."
            return StageResult(True, message, pending=True, run_id=run_id)

    def _run(self, run_id):
        if not run_id:
            raise ValueError("start a run first")
        run = self.db.runs.find_one({"_id": run_id})
        if run is None:
            raise ValueError("run is not saved yet")
        return run

    def _note(self, run_id, principal, sentence, **fields):
        self.db.tasks.update_one({"_id": f"{run_id}:{principal}"}, {"$set": {
            "note": sentence, "updated_at": utcnow(), **fields,
        }})

    def _launch_worker(self, run_id, principal):
        env = os.environ.copy()
        env["STOPLINE_DATABASE"] = self.db.name
        env.setdefault("STOPLINE_SENDER_URL", f"http://127.0.0.1:{PAGE_PORT}")
        return subprocess.Popen(
            [sys.executable, "-m", "stopline.agents.worker", "--run", run_id, "--principal", principal],
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def _spawn(self, run_id, principal):
        handle = self._launch_worker(run_id, principal)
        self._processes[run_id, principal] = handle
        self.db.tasks.update_one({"_id": f"{run_id}:{principal}"}, {"$set": {"pid": handle.pid}})
        return handle

    def start_run(self, mode, holds=None):
        try:
            mode = Mode(mode)
        except ValueError:
            return StageResult(False, "Choose stopline, flag or fixed mode.")
        with self._job_lock:
            previous = self._jobs.get((self.run_id, "start_run"))
            if self._starting or (previous is not None and not previous.done()):
                return StageResult(True, "Start run is pending.", pending=True, run_id=self.run_id)
            if any(handle.poll() is None for handle in self._processes.values()):
                return StageResult(False, "Wait for the previous workers to finish before starting a run.",
                                   run_id=self.run_id)
            run_id = "r" + secrets.token_hex(5)
            self.run_id = run_id
            self._starting = True
        saved_holds = deepcopy(DEMO_HOLDS[mode] if holds is None else holds)

        def start():
            try:
                return self._start(run_id, mode, saved_holds)
            finally:
                with self._job_lock:
                    self._starting = False

        return self._command("start_run", start, run_id)

    def _start(self, run_id, mode, holds):
        at = utcnow()
        self.db.runs.insert_one({
            "_id": run_id, "mode": mode, "cluster": self.cluster, "status": "running",
            "root_mandate_id": None, "agent_mandate_id": None,
            "stop_flag": False, "stop_at": None, "stop_at_local": None,
            "holds": holds, "receipt_id": None, "planner": "scripted", "created_at": at,
        })
        for principal, parent in ((OPERATOR, None), (REPAIR_AGENT, OPERATOR),
                                  (WORKER_ONE, REPAIR_AGENT), (WORKER_TWO, REPAIR_AGENT)):
            self.db.principals.update_one({"_id": principal}, {"$setOnInsert": {
                "started_by": parent, "kind": "person" if principal == OPERATOR else "agent",
            }}, upsert=True)
        tools = sorted({str(s["tool"]) for steps in DEMO_PLAN.values() for s in steps})
        targets = sorted({s["target"] for steps in DEMO_PLAN.values() for s in steps})
        root = self.gate.grant_root(
            run_id=run_id, grantor=OPERATOR, holder=OPERATOR, tools=tools, targets=targets,
            limits={"max_actions": 12, "max_shift_percent": 100}, ttl_seconds=3600,
        )
        RepairAgent(self.db, self.gate, self.planner).prepare(run_id, root)
        for principal in WORKERS:
            self._spawn(run_id, principal)
        return StageResult(True, f"Started the {mode} repair run.", run_id=run_id)

    def press_stop(self):
        run_id = self.run_id
        return self._command("press_stop", lambda: self._stop(run_id), run_id)

    def _stop(self, run_id):
        run = self._run(run_id)
        fields = {}
        if not run["stop_flag"]:
            if run["mode"] == Mode.STOPLINE:
                receipt = self.gate.stop(mandate_id=run["agent_mandate_id"], by=OPERATOR)
                # Second half of a stop: ask each target about work that was claimed but has no
                # evidence yet. What never arrived is fenced there before any hold is released.
                self.sender.sweep(receipt["covered"])
                fields = {"receipt_id": receipt["_id"], "stop_at": receipt["stopped_at"]}
            fields.update(stop_flag=True, status="stopped", stop_at_local=time.time())
            update = {"$set": fields}
            if run["mode"] != Mode.STOPLINE:
                update["$currentDate"] = {"stop_at": True}
            self.db.runs.update_one({"_id": run_id}, update)
        # Release only after the authority change/flag has committed.
        self.db.runs.update_one({"_id": run_id}, {"$set": {
            "holds": [{**h, "released": True} for h in run["holds"]],
        }})
        self._note(run_id, REPAIR_AGENT, "Operator stopped the repair and released its holds.")
        return StageResult(True, "Stopped the repair and released every hold.", run_id=run_id)

    def cut_and_kill(self):
        run_id = self.run_id
        return self._command("cut_and_kill", lambda: self._cut_and_kill(run_id), run_id)

    def _cut_and_kill(self, run_id):
        run = self._run(run_id)
        handle = self._processes.get((run_id, WORKER_TWO))
        if handle is None or handle.poll() is not None:
            return StageResult(False, "Worker two is not running.", run_id=run_id)
        action_id = action_id_for(run_id, WORKER_TWO, 0)
        self.targets[ROUTER].cut()
        self.db.runs.update_one({"_id": run_id}, {"$set": {"holds": [
            {**h, "released": True} if h["principal"] == WORKER_TWO else h for h in run["holds"]
        ]}})
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            effect = self.db.effects.find_one({"_id": action_id})
            state = self.targets[ROUTER].state()
            taken = (state.get("last_action_id") == action_id or not state.get("reachable", True)
                     or any(row.get("action_id") == action_id for row in state.get("log", [])))
            if effect and effect["state"] == EffectState.SENT and taken:
                if handle.poll() is None:
                    handle.kill()
                handle.wait(timeout=0.2)
                self._note(run_id, WORKER_TWO, "Killed after the router took the 25% shift; its outcome is unknown.",
                           status=TaskStatus.KILLED, held_at=None)
                return StageResult(True, "Cut the router and killed worker two after its send.", run_id=run_id)
            time.sleep(0.05)
        return StageResult(True, CUT_PENDING, pending=True, run_id=run_id)

    def resume_worker(self):
        run_id = self.run_id
        return self._command("resume_worker", lambda: self._resume(run_id), run_id)

    def _resume(self, run_id):
        self._run(run_id)
        handle = self._processes.get((run_id, WORKER_TWO))
        if handle is not None and handle.poll() is None:
            return StageResult(False, "Worker two is already running.", run_id=run_id)
        self._note(run_id, WORKER_TWO, "Resuming worker two from its saved position.",
                   status=TaskStatus.RUNNING, held_at=None)
        self._spawn(run_id, WORKER_TWO)
        return StageResult(True, "Resumed worker two from its saved position.", run_id=run_id)

    def restore_and_reconcile(self):
        run_id = self.run_id
        return self._command("restore_and_reconcile", lambda: self._reconcile(run_id), run_id)

    def _reconcile(self, run_id):
        run = self._run(run_id)
        self.targets[ROUTER].restore()
        effects = self.db.effects.find({"run_id": run_id, "state": EffectState.SENT})
        pending = False
        for effect in effects:
            if run["mode"] == Mode.STOPLINE:
                result = self.sender.reconcile(effect["_id"])
                pending |= result["state"] == EffectState.SENT
                if effect.get("reverses"):
                    self._mark_undo(effect["reverses"], result)
            else:
                target = self.targets[system_of(effect["target"])]
                try:
                    found = target.lookup(effect["_id"])
                    evidence = found.evidence if found.found else target.fence(effect["_id"])
                    self.db.effects.update_one({"_id": effect["_id"], "state": EffectState.SENT}, {"$set": {
                        "state": EffectState.APPLIED if found.applied else EffectState.REJECTED,
                        "evidence": evidence, "finished_at": utcnow(),
                    }})
                except TargetUnreachable:
                    pending = True
        self._note(run_id, WORKER_TWO, "Restored the router and checked the outstanding sends.")
        message = ("Restored the router; some outcomes are still pending." if pending
                   else "Restored the router and reconciled outstanding sends.")
        return StageResult(True, message, pending=pending, run_id=run_id)

    def undo(self):
        run_id = self.run_id
        return self._command("undo", lambda: self._undo(run_id), run_id)

    def overreach(self):
        with self._job_lock:
            run_id = self.run_id
            self._overreach_count += 1
            action_id = f"{run_id}-worker-2-over-{self._overreach_count}"
        # Each press asks a fresh question, even while an earlier one is pending.
        return self._command("overreach", lambda: self._overreach(run_id, action_id),
                             run_id, job_key=action_id)

    def _overreach(self, run_id, action_id):
        if run_id is None:
            return StageResult(False, "Start a StopLine run first.")
        run = self._run(run_id)
        if run["mode"] != Mode.STOPLINE:
            return StageResult(False, "Start a StopLine run first: a baseline run has no check to refuse it.",
                               run_id=run_id)
        worker = self.db.tasks.find_one({"_id": f"{run_id}:{WORKER_TWO}"})
        if not worker or not worker["mandate_id"]:
            return StageResult(False, "Worker two has no saved permission yet.", run_id=run_id)
        admitted = self.gate.admit(Action(
            action_id=action_id, run_id=run_id, mandate_id=worker["mandate_id"],
            principal=WORKER_TWO, tool=Tool.SHIFT, target=TARGET_REGION_B, args={"percent": 90},
        ))
        if admitted.allowed:
            outcome = "The gate allowed it. It was not sent."
        elif admitted.reason == Reason.LIMIT_EXCEEDED:
            outcome = "Refused: its permission allows at most 50%."
        else:
            outcome = refusal_note(admitted)
        message = f"Worker two asked to shift 90%. {outcome}"
        self._note(run_id, WORKER_TWO, message)
        return StageResult(not admitted.allowed, message, run_id=run_id, detail={
            "action_id": action_id, "allowed": admitted.allowed, "reason": admitted.reason,
        })

    def _mark_undo(self, action_id, result):
        mark = {EffectState.APPLIED: UndoMark.UNDONE, EffectState.SENT: UndoMark.UNKNOWN}.get(result["state"])
        self.db.effects.update_one({"_id": action_id}, {"$set": {"undo": mark}})
        return mark

    def _undo(self, run_id):
        run = self._run(run_id)
        effects = list(self.db.effects.find({"run_id": run_id, "state": EffectState.APPLIED}))
        effects.sort(key=lambda e: e.get("admitted_seq") or (e.get("evidence") or {}).get("at_epoch", 0), reverse=True)
        pending, refusals = False, []
        for effect in effects:
            if effect.get("reverses") or effect.get("undo") in {UndoMark.UNDONE, UndoMark.NO_UNDO}:
                continue
            undo_id = f"{effect['_id']}-undo"
            existing = self.db.effects.find_one({"_id": undo_id})
            if not existing:
                admitted = self.gate.undo(effect["_id"], mandate_id=run["root_mandate_id"],
                                          principal=OPERATOR, targets=self.targets, undo_id=undo_id)
                if not admitted.allowed:
                    if admitted.reason == Reason.NO_UNDO:
                        self.db.effects.update_one({"_id": effect["_id"]}, {"$set": {"undo": UndoMark.NO_UNDO}})
                    else:
                        refusals.append({"action_id": effect["_id"], "reason": admitted.reason})
                    continue
                undo_id = admitted.action_id
            result = existing
            if not existing or existing["state"] == EffectState.ADMITTED:
                claim = self.sender.claim(undo_id)
                if not claim.claimed:
                    refusals.append({"action_id": effect["_id"], "reason": claim.reason})
                    continue
                try:
                    result = self.sender.send(undo_id)
                except (TargetTimeout, TargetUnreachable, httpx.RequestError):
                    result = {"state": EffectState.SENT}
            mark = self._mark_undo(effect["_id"], result)
            pending |= mark == UndoMark.UNKNOWN
            if mark is None:
                reason = (result.get("evidence") or {}).get("reason", Reason.STATE_CHANGED)
                refusals.append({"action_id": effect["_id"], "reason": reason})
        note = ("Some undo results are still unknown." if pending else
                "Undo was refused because the state or permission changed." if refusals else
                "Finished undo under Operator's own permission. Marked actions that have no undo.")
        self._note(run_id, REPAIR_AGENT, note)
        return StageResult(not refusals, note, pending=pending, run_id=run_id, detail={"refusals": refusals})

    def validation_rejection(self):
        run_id = self.run_id
        return self._command("validation_rejection", lambda: self._validate(run_id), run_id)

    def _validate(self, run_id):
        def malformed(session):
            self.db.audit.insert_one({"_id": "invalid-" + secrets.token_hex(5), "run_id": run_id}, session=session)
            # If setup was missed, abort rather than leave malformed audit data.
            raise MissingValidator()

        try:
            run_transaction(malformed)
        except OperationFailure as error:
            if error.code != 121:
                raise
            return StageResult(True, "MongoDB rejected the malformed audit entry (validation error 121).",
                               run_id=run_id, detail={"code": 121, "rejected": True})
        except MissingValidator:
            return StageResult(False, "The audit validator is missing; the attempted insert was rolled back.", run_id=run_id)

    def status(self):
        run_id = self.run_id

        def read():
            run = self.db.runs.find_one({"_id": run_id}) if run_id else None
            tasks = list(self.db.tasks.find({"run_id": run_id})) if run_id else []
            with self._job_lock:
                operations = {name: asdict(f.result()) for (rid, name), f in self._jobs.items()
                              if rid == run_id and name != "status" and f.done()}
            snapshot = {"run": run, "run_id": run_id, "mode": run["mode"] if run else None,
                        "tasks": tasks, "receipt_id": run.get("receipt_id") if run else None,
                        "operations": operations, "pending": False}
            self._cached_status = snapshot
            return StageResult(True, "Read the current run.", run_id=run_id, detail=snapshot)

        result = self._command("status", read, run_id, serial=False)
        if result.pending or not result.ok:
            return {**self._cached_status, "pending": result.pending, "message": result.message}
        return result.detail
