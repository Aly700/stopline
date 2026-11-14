"""Planning has no authority and performs no target calls.

The default is ScriptedPlanner. To wire a model later:
    stage.planner = ModelPlanner(call_model, SavedPlanner(db))
call_model receives run_id, mandate and timeout_seconds as keyword arguments.
"""

from concurrent.futures import Future, TimeoutError as FutureTimeout
from copy import deepcopy
from dataclasses import dataclass
import threading
from typing import Any, Callable, Protocol

from stopline.contract import DEMO_PLAN, Mandate, Step, WORKER_ONE, WORKER_TWO, action_id_for


MODEL_TIMEOUT_SECONDS = 10.0
WORKERS = (WORKER_ONE, WORKER_TWO)


@dataclass(frozen=True)
class PlannedRepair:
    steps: dict[str, list[Step]]
    source: str
    note: str


class Planner(Protocol):
    def plan(self, run_id: str, mandate: Mandate) -> PlannedRepair: ...


def prepare_steps(run_id, raw):
    """Only the agreed four-step repair is executable in today's demo."""
    if set(raw) != set(WORKERS):
        raise ValueError("the repair needs exactly two workers")
    result = {}
    for principal, expected in DEMO_PLAN.items():
        steps = raw[principal]
        if len(steps) != len(expected):
            raise ValueError("each worker needs two steps")
        for step, allowed in zip(steps, expected):
            if any(step.get(key) != allowed[key] for key in ("tool", "target", "args")):
                raise ValueError("the proposed repair differs from the agreed scope")
        result[principal] = [
            {"action_id": action_id_for(run_id, principal, position),
             "tool": str(step["tool"]), "target": step["target"], "args": deepcopy(step["args"])}
            for position, step in enumerate(steps)
        ]
    return result


class ScriptedPlanner:
    def plan(self, run_id, mandate):
        return PlannedRepair(prepare_steps(run_id, DEMO_PLAN), "scripted", "Used the scripted repair.")


class SavedPlanner:
    def __init__(self, db, source_run_id=None):
        self.db, self.source_run_id = db, source_run_id

    def plan(self, run_id, mandate):
        tasks = self.db.tasks.find({"run_id": self.source_run_id or run_id,
                                    "principal": {"$in": list(WORKERS)}})
        steps = {task["principal"]: task["plan"] for task in tasks}
        return PlannedRepair(prepare_steps(run_id, steps), "saved", "Loaded the saved repair.")


class ModelPlanner:
    def __init__(self, call_model: Callable[..., dict[str, Any]], fallback: SavedPlanner,
                 timeout_seconds=MODEL_TIMEOUT_SECONDS):
        self.call_model, self.fallback = call_model, fallback
        self.timeout_seconds = min(float(timeout_seconds), MODEL_TIMEOUT_SECONDS)
        if self.timeout_seconds <= 0:
            raise ValueError("the model timeout must be positive")

    def plan(self, run_id, mandate):
        answer = Future()

        def call():
            try:
                result = self.call_model(run_id=run_id, mandate=deepcopy(mandate),
                                         timeout_seconds=self.timeout_seconds)
                answer.set_result(result)
            except Exception as error:
                answer.set_exception(error)

        # Do not use an executor context manager: exiting it waits for a stuck call.
        # The callback receives the same deadline for its own network timeout.
        threading.Thread(target=call, daemon=True, name="repair-model").start()
        try:
            raw = answer.result(timeout=self.timeout_seconds)
            return PlannedRepair(prepare_steps(run_id, raw), "model", "Used the model's repair.")
        except FutureTimeout:
            why = f"Model planner timed out after {self.timeout_seconds:g} seconds"
        except Exception as error:
            why = f"Model planner failed ({type(error).__name__})"
        saved = self.fallback.plan(run_id, mandate)
        return PlannedRepair(saved.steps, "saved", f"{why}. Loaded the saved repair.")
