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

from stopline.contract import (
    DEMO_HOLDS, DEMO_PLAN, EffectState, Mode, OPERATOR, PAGE_PORT, REPAIR_AGENT,
    ROUTER, StageResult, TaskStatus, WORKER_ONE, WORKER_TWO, action_id_for,
)
from .worker import utcnow


WORKERS = (WORKER_ONE, WORKER_TWO)


class Stage:
    def __init__(self, db, gate, targets, sender, cluster="kind"):
        self.db, self.gate, self.targets, self.sender = db, gate, targets, sender
        self.cluster = cluster
        self.run_id = None
        self._processes = {}
        self._jobs = {}
        self._job_lock = threading.Lock()
        self._actions = threading.Lock()
        self._cached_status = {"run": None, "tasks": [], "receipt_id": None}
        self.wait_seconds = 1.5

    def _command(self, name, operation, run_id=None, *, serial=True):
        """Do slow work once in the background; repeated pending keys reuse it."""
        key = (run_id, name)
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
            return StageResult(True, f"{name.replace('_', ' ').capitalize()} is pending.",
                               pending=True, run_id=run_id)

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
            if previous is not None and not previous.done():
                return StageResult(True, "Start run is pending.", pending=True, run_id=self.run_id)
            if any(handle.poll() is None for handle in self._processes.values()):
                return StageResult(False, "Wait for the previous workers to finish before starting a run.",
                                   run_id=self.run_id)
            run_id = "r" + secrets.token_hex(5)
            self.run_id = run_id
        saved_holds = deepcopy(DEMO_HOLDS[mode] if holds is None else holds)
        return self._command("start_run", lambda: self._start(run_id, mode, saved_holds), run_id)

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
        agent = self.gate.hand_down(
            parent_id=root["_id"], holder=REPAIR_AGENT, tools=tools, targets=targets,
            limits={"max_actions": 4, "max_shift_percent": 50}, ttl_seconds=1800,
        )
        if not agent.allowed:
            raise RuntimeError("repair mandate was refused")
        self.db.runs.update_one({"_id": run_id}, {"$set": {
            "root_mandate_id": root["_id"], "agent_mandate_id": agent.mandate_id,
        }})
        tasks = []
        for principal in WORKERS:
            plan = [{**deepcopy(step), "action_id": action_id_for(run_id, principal, index)}
                    for index, step in enumerate(DEMO_PLAN[principal])]
            scope = {
                "tools": sorted({str(s["tool"]) for s in plan}),
                "targets": sorted({s["target"] for s in plan}),
                "limits": {"max_actions": len(plan), "max_shift_percent": 50 if principal == WORKER_TWO else 0},
            }
            child = self.gate.hand_down(parent_id=agent.mandate_id, holder=principal,
                                        ttl_seconds=900, **scope)
            if not child.allowed:
                raise RuntimeError("worker mandate was refused")
            tasks.append({
                "_id": f"{run_id}:{principal}", "run_id": run_id, "principal": principal,
                "mandate_id": child.mandate_id, "plan": plan, "position": 0,
                "planner": "scripted", "status": TaskStatus.RUNNING, "held_at": None,
                "pid": None, "note": "Repair steps are saved; ready to start.", "updated_at": at,
                "fixed_rules": scope,
            })
        tasks.append({
            "_id": f"{run_id}:{REPAIR_AGENT}", "run_id": run_id, "principal": REPAIR_AGENT,
            "mandate_id": agent.mandate_id, "plan": [s for t in tasks for s in t["plan"]],
            "position": 4, "planner": "scripted", "status": TaskStatus.DONE,
            "held_at": None, "pid": None,
            "note": "Saved the repair and delegated narrower mandates to both workers.", "updated_at": at,
        })
        self.db.tasks.insert_many(tasks)
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
        self._note(run_id, REPAIR_AGENT, f"{OPERATOR} stopped the repair and released its holds.")
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
        return StageResult(True, "Router send is pending; press cut and kill again once it arrives.",
                           pending=True, run_id=run_id)

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
        for effect in effects:
            if run["mode"] == Mode.STOPLINE:
                self.sender.reconcile(effect["_id"])
        self._note(run_id, WORKER_TWO, "Restored the router and checked the outstanding sends.")
        return StageResult(True, "Restored the router and reconciled outstanding sends.", run_id=run_id)

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
