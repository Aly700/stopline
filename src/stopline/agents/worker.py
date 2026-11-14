"""Run one saved worker: python -m stopline.agents.worker --run ID --principal ID."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
import threading
import time

import httpx

from stopline.contract import (
    Action, EffectState, HoldPoint, Mode, NotClaimed, OPERATOR, REPAIR_AGENT,
    TaskStatus, TargetTimeout, TargetUnreachable, TERMINAL_STATES,
    WORKER_ONE, WORKER_TWO, args_hash, system_of,
)


DISPLAY_NAMES = {
    OPERATOR: "Operator", REPAIR_AGENT: "Repair agent",
    WORKER_ONE: "Worker one", WORKER_TWO: "Worker two",
}


def utcnow():
    return datetime.now(timezone.utc)


def refusal_note(result):
    """Keep the gate's stop identity and time visible on the saved task."""
    who = result.detail.get("revoked_by")
    when = result.detail.get("revoked_at")
    if hasattr(when, "isoformat"):
        when = when.isoformat()
    if who is not None and when is not None:
        name = DISPLAY_NAMES.get(who, "Permission holder")
        return f"Refused: {name} stopped the permission at {when}."
    reason = result.reason.replace("_", " ").replace("mandate", "permission")
    return f"Refused: {reason}."


class StopListener:
    """An early-exit hint; admission and claim remain the authority checks."""

    def __init__(self, db, run_id):
        self.db, self.run_id = db, run_id
        self.stopped = threading.Event()
        self.finished = threading.Event()
        self.thread = threading.Thread(target=self._listen, daemon=True)

    def _listen(self):
        try:
            with self.db.runs.watch(
                [{"$match": {"documentKey._id": self.run_id}}],
                full_document="updateLookup", max_await_time_ms=500,
            ) as stream:
                while not self.finished.is_set():
                    change = stream.try_next()
                    if change:
                        run = change.get("fullDocument") or {}
                        if run.get("stop_flag") or run.get("status") == "stopped":
                            self.stopped.set()
                            return
        except Exception:
            # An unavailable stream must never make the gate optional.
            return

    def start(self):
        self.thread.start()

    def close(self):
        self.finished.set()
        self.thread.join(timeout=0.6)


class Worker:
    def __init__(self, db, gate, targets, sender, run_id, principal):
        self.db, self.gate, self.targets, self.sender = db, gate, targets, sender
        self.run_id, self.principal = run_id, principal
        self.task_id = f"{run_id}:{principal}"
        self.listener = StopListener(db, run_id)

    def _save(self, note, **fields):
        self.db.tasks.update_one({"_id": self.task_id}, {"$set": {
            "note": note, "updated_at": utcnow(), **fields,
        }})

    def _hold(self, point, position):
        matches = [h for h in self.run["holds"]
                   if h["principal"] == self.principal and h["step"] == position
                   and h["point"] == point and not h["released"]]
        if not matches:
            return
        self._save(f"Held by test schedule {point} at step {position + 1}.",
                   status=TaskStatus.HELD, held_at={"step": position, "point": point})
        while True:
            current = self.db.runs.find_one({"_id": self.run_id})
            waiting = any(h["principal"] == self.principal and h["step"] == position
                          and h["point"] == point and not h["released"] for h in current["holds"])
            if not waiting:
                break
            time.sleep(0.1)
        self._save(f"Released from {point} at step {position + 1}.",
                   status=TaskStatus.RUNNING, held_at=None)

    def _stop(self, note):
        self._save(note, status=TaskStatus.STOPPED, held_at=None)

    def _baseline(self, action, position):
        mode = self.run["mode"]
        if mode == Mode.FLAG:
            checked = self.db.runs.find_one({"_id": self.run_id})
            if checked["stop_flag"]:
                self._stop(f"Stopped: Operator set the flag at {checked['stop_at']}.")
                return False
        # Fixed rules were saved when this run started, and are never refreshed.
        rules = self.task["fixed_rules"]
        if (action.tool not in rules["tools"] or action.target not in rules["targets"]
                or position >= rules["limits"]["max_actions"]
                or action.args.get("percent", 0) > rules["limits"]["max_shift_percent"]):
            self._stop("Refused by the rules saved at the start of this run.")
            return False
        self._hold(HoldPoint.AFTER_CHECK, position)
        at = utcnow()
        self.db.effects.update_one({"_id": action.action_id}, {"$setOnInsert": {
            "_id": action.action_id, "run_id": self.run_id, "mode": mode,
            "root_id": None, "mandate_id": None, "chain": [],
            "principal": self.principal, "tool": action.tool, "target": action.target,
            "args": action.args, "args_hash": args_hash(action.args),
            "admitted_seq": None, "state": EffectState.SENT, "evidence": None,
            "reverses": None, "undo": None, "policy_version": None, "policy_hash": None,
            "claimed_by": self.principal, "admitted_at": at, "sent_at": at, "finished_at": None,
        }}, upsert=True)
        self._save(f"Sending {action.tool} to {action.target}.")
        result = self.targets[system_of(action.target)].send(action)
        self._hold(HoldPoint.AFTER_SEND, position)
        self.db.effects.update_one({"_id": action.action_id, "state": EffectState.SENT}, {"$set": {
            "state": EffectState.APPLIED if result.applied else EffectState.REJECTED,
            "evidence": result.evidence, "finished_at": utcnow(),
        }})
        return EffectState.APPLIED if result.applied else EffectState.REJECTED

    def _gated(self, action, position):
        admitted = self.gate.admit(action)
        if not admitted.allowed:
            self._stop(refusal_note(admitted))
            return False
        self._hold(HoldPoint.AFTER_CHECK, position)
        try:
            claimed = self.sender.claim(action.action_id)
        except httpx.RequestError:
            # A lost claim response does not establish that a send happened.
            # Resume will inspect the persisted effect before retrying this step.
            self._stop("Could not confirm the claim; kept this step saved for resume.")
            return False
        if not claimed.claimed:
            self._stop(refusal_note(claimed))
            return False
        self._hold(HoldPoint.AFTER_CLAIM, position)
        self._save(f"Sending {action.tool} to {action.target}.")
        try:
            result = self.sender.send(action.action_id)
        except NotClaimed:
            # The stop's sweep settled this action (fenced at the target) while the worker was held.
            self._stop("Refused: this action was settled by the stop before it could be sent.")
            return False
        # Sender.send owns outcome recording in the final shared interface.
        self._hold(HoldPoint.AFTER_SEND, position)
        return result["state"]

    def run_saved(self):
        self.task = self.db.tasks.find_one({"_id": self.task_id})
        self.run = self.db.runs.find_one({"_id": self.run_id})
        if self.task is None or self.run is None:
            raise ValueError("saved run or worker task is missing")
        self.listener.start()
        try:
            plan = self.task["plan"]
            unsettled = False
            for position in range(self.task["position"], len(plan)):
                step = plan[position]
                # Save both plan and position before doing anything for this step.
                self._save(f"Ready for step {position + 1}: {step['tool']}.",
                           plan=plan, position=position, pid=os.getpid(),
                           status=TaskStatus.RUNNING, held_at=None)
                existing = self.db.effects.find_one({"_id": step["action_id"]})
                if existing and existing["state"] in {EffectState.SENT, *TERMINAL_STATES}:
                    unsettled |= existing["state"] == EffectState.SENT
                    self._save("This step was already sent or settled; reconciliation owns its outcome.",
                               position=position + 1)
                    continue
                self._hold(HoldPoint.BEFORE_CHECK, position)
                action = Action(run_id=self.run_id, principal=self.principal,
                                mandate_id=self.task["mandate_id"] or "", **step)
                try:
                    outcome = (self._gated(action, position) if self.run["mode"] == Mode.STOPLINE
                               else self._baseline(action, position))
                except (TargetTimeout, TargetUnreachable, httpx.RequestError):
                    outcome = EffectState.SENT
                if not outcome:
                    return
                unsettled |= outcome == EffectState.SENT
                note = (f"Sent step {position + 1}; its outcome is pending reconciliation."
                        if outcome == EffectState.SENT else f"Finished step {position + 1}: {outcome}.")
                self._save(note, position=position + 1)
                # Never interrupt an already checked step: the demo deliberately
                # pauses that race. A resumed worker must reach its next gate check.
                if (position + 1 < len(plan) and self.run["mode"] != Mode.FIXED
                        and self.listener.stopped.is_set()):
                    self._stop("Heard the stop; halted before starting another step.")
                    return
            note = ("All saved steps have been handled; some outcomes are pending reconciliation."
                    if unsettled else "All saved steps have been handled.")
            self._save(note, status=TaskStatus.DONE, held_at=None)
        finally:
            self.listener.close()


def run_worker(db, gate, targets, sender, run_id, principal):
    """Dependency-injected entry point, also used by process tests."""
    worker = Worker(db, gate, targets, sender, run_id, principal)
    try:
        worker.run_saved()
    except Exception as error:
        # Driver exceptions can contain connection information; retain only type.
        worker._stop(f"Worker halted after {type(error).__name__}; its position is saved.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    parser.add_argument("--principal", required=True)
    args = parser.parse_args(argv)
    from stopline.db import database

    db = database(os.environ.get("STOPLINE_DATABASE", "stopline"))
    run = db.runs.find_one({"_id": args.run})
    if run is None:
        parser.error("saved run does not exist")
    if run["mode"] == Mode.STOPLINE:
        from stopline.gate import build_gate
        from stopline.wire import RemoteSender

        sender_url = os.environ.get("STOPLINE_SENDER_URL")
        if not sender_url:
            parser.error("STOPLINE_SENDER_URL is required for a separate StopLine worker")
        gate, targets, sender = build_gate(db), {}, RemoteSender(sender_url)
    else:
        from stopline.targets import build_targets

        gate, sender = None, None
        targets = build_targets(cluster=run["cluster"])
    run_worker(db, gate, targets, sender, args.run, args.principal)


if __name__ == "__main__":
    main()
