"""Start the whole system on one machine.

    python -m stopline                 real cluster target (kind cluster "stopline")
    python -m stopline --cluster mock  in-process cluster target
    python -m stopline --voice         also speak the receipt aloud
    python -m stopline --model         a language model plans the repair

One process holds the gate, the sender, the stage and the page. The traffic
router runs as its own process with its own store. Workers are separate
processes started by the stage, and they reach the sender over HTTP.
"""

from __future__ import annotations

import argparse
import atexit
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import uvicorn

from . import db as stopline_db
from .contract import PAGE_PORT, ROUTER_PORT, Hold, StageResult
from .wire import sender_routes, target_routes


def _port_open(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.3)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def start_router(store: Path) -> subprocess.Popen:
    """The router is a second system: its own process, its own SQLite file."""
    if _port_open(ROUTER_PORT):
        raise SystemExit(f"port {ROUTER_PORT} is already in use; stop the old router first")
    process = subprocess.Popen(
        [sys.executable, "-m", "stopline.targets.router", "--port", str(ROUTER_PORT), "--store", str(store)]
    )
    atexit.register(process.terminate)
    deadline = time.time() + 10
    while time.time() < deadline:
        if _port_open(ROUTER_PORT):
            return process
        if process.poll() is not None:
            raise SystemExit("the router exited while starting")
        time.sleep(0.1)
    raise SystemExit("the router did not start within ten seconds")


class ResettingStage:
    """The stage, with both target systems put back to their starting state before each run."""

    def __init__(self, stage: Any, targets: dict[str, Any], reset_all: Any) -> None:
        self._stage = stage
        self._targets = targets
        self._reset_all = reset_all

    def start_run(self, mode: str, holds: list[Hold] | None = None) -> StageResult:
        self._reset_all(self._targets)
        return self._stage.start_run(mode, holds)

    def __getattr__(self, name: str) -> Any:
        try:
            return getattr(self._stage, name)
        except AttributeError:
            if name in ("undo", "validation_rejection"):
                return lambda: StageResult(False, f"{name.replace('_', ' ').capitalize()} is not available in this build.")
            raise


RUNBOOK_INCIDENT = (
    "checkout-api version v2 is failing; the last good version is v1. "
    "Runbook for this incident: worker one rolls checkout-api back to v1, then restarts checkout-worker. "
    "Worker two moves traffic to region B in two steps: 25 percent, then 50 percent."
)


def model_planner(database: Any) -> Any:
    """A language model turns the incident and its runbook into steps. The check still decides what runs.

    Returns None when no model key is configured. A slow or malformed answer falls back to the saved plan,
    and the run records which one was used.
    """
    from . import llm
    from .agents.planner import ModelPlanner, SavedPlanner

    if not llm.key_configured():
        return None
    model = llm.ModelPlanner()

    def call_model(*, run_id: str, mandate: Any, timeout_seconds: float) -> dict[str, Any]:
        plan = model.plan(RUNBOOK_INCIDENT)
        return {worker: [{key: step[key] for key in ("tool", "target", "args")} for step in steps]
                for worker, steps in plan.steps.items()}

    return ModelPlanner(call_model, SavedPlanner(database))


RUN_COLLECTIONS = ("principals", "mandates", "effects", "audit", "receipts", "tasks", "runs")


def build(cluster: str, port: int, voice: bool = False, model: bool = False,
          database_name: str = "stopline", fresh: bool = False) -> Any:
    from .agents import build_stage
    from .gate import build_gate
    from .proof import summary
    from .targets import build_sender, build_targets, reset_all
    from .view import create_app

    database = stopline_db.database(database_name)
    if fresh:
        for name in RUN_COLLECTIONS:   # earlier runs only; the measured numbers are kept
            database[name].delete_many({})
    gate = build_gate(database)
    gate.setup()
    targets = build_targets(cluster)
    sender = build_sender(gate, targets)
    inner = build_stage(database, gate, targets, sender, cluster=cluster)
    planner = model_planner(database) if model else None
    if planner is not None:
        inner.planner = planner
    stage = ResettingStage(inner, targets, reset_all)
    app = create_app(database, stage, targets, numbers=lambda: summary(database))
    app.include_router(sender_routes(sender))
    if cluster == "mock":
        app.include_router(target_routes("cluster", targets["cluster"]))
        os.environ["STOPLINE_TARGETS_URL"] = f"http://127.0.0.1:{port}"  # baseline workers share this mock
    try:
        from .report import report_routes
    except ImportError:
        report_routes = None
    if report_routes is not None:
        app.include_router(report_routes(database))

        @app.middleware("http")
        async def report_styles(request: Any, call_next: Any) -> Any:
            # The report is one self-contained page with inline styles and no scripts.
            response = await call_next(request)
            if request.url.path.startswith("/report/"):
                response.headers["Content-Security-Policy"] = (
                    "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'")
            return response
    if voice:
        from . import voice as spoken

        spoken.watch(database)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(prog="stopline")
    parser.add_argument("--cluster", choices=["kind", "mock"], default="kind")
    parser.add_argument("--port", type=int, default=PAGE_PORT)
    parser.add_argument("--voice", action="store_true", help="speak the receipt when a run is stopped")
    parser.add_argument("--model", action="store_true", help="let a language model plan the repair (needs GEMINI_API_KEY)")
    parser.add_argument("--database", default=os.environ.get("STOPLINE_DATABASE", "stopline"))
    parser.add_argument("--fresh", action="store_true", help="clear earlier runs from the database before starting")
    args = parser.parse_args()

    os.environ["STOPLINE_SENDER_URL"] = f"http://127.0.0.1:{args.port}"  # worker processes inherit this
    start_router(Path(tempfile.gettempdir()) / "stopline-router.sqlite")
    uvicorn.run(build(args.cluster, args.port, args.voice, args.model, args.database, args.fresh), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
