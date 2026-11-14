"""Start the whole system on one machine.

    python -m stopline                 real cluster target (kind cluster "stopline")
    python -m stopline --cluster mock  in-process cluster target

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
from .wire import sender_routes


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


def build(cluster: str, port: int) -> Any:
    from .agents import build_stage
    from .gate import build_gate
    from .proof import summary
    from .targets import build_sender, build_targets, reset_all
    from .view import create_app

    database = stopline_db.database()
    gate = build_gate(database)
    gate.setup()
    targets = build_targets(cluster)
    sender = build_sender(gate, targets)
    stage = ResettingStage(build_stage(database, gate, targets, sender, cluster=cluster), targets, reset_all)
    app = create_app(database, stage, targets, numbers=lambda: summary(database))
    app.include_router(sender_routes(sender))
    return app


def main() -> None:
    parser = argparse.ArgumentParser(prog="stopline")
    parser.add_argument("--cluster", choices=["kind", "mock"], default="kind")
    parser.add_argument("--port", type=int, default=PAGE_PORT)
    args = parser.parse_args()

    os.environ["STOPLINE_SENDER_URL"] = f"http://127.0.0.1:{args.port}"  # worker processes inherit this
    start_router(Path(tempfile.gettempdir()) / "stopline-router.sqlite")
    uvicorn.run(build(args.cluster, args.port), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
