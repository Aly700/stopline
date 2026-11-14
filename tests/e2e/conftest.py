"""The whole system on spare ports: real gate, real router process, real worker processes.

Set STOPLINE_E2E_CLUSTER=kind to use the real cluster instead of the in-process one.
"""

import os

os.environ["STOPLINE_PAGE_PORT"] = "8775"     # before stopline.contract is imported
os.environ["STOPLINE_ROUTER_PORT"] = "8776"

import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import uvicorn
from fastapi import FastAPI

from stopline import db as stopline_db
from stopline.agents import build_stage
from stopline.contract import PAGE_PORT, ROUTER_PORT
from stopline.gate import build_gate
from stopline.targets import build_sender, build_targets, reset_all
from stopline.wire import sender_routes

CLUSTER = os.environ.get("STOPLINE_E2E_CLUSTER", "mock")


def wait_for_port(port, seconds=10.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        with socket.socket() as probe:
            probe.settimeout(0.2)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise RuntimeError(f"nothing is listening on port {port}")


@pytest.fixture(scope="session")
def system():
    database = stopline_db.test_database("e2e")
    store = Path(tempfile.mkdtemp(prefix="stopline-e2e-")) / "router.sqlite"
    router = subprocess.Popen([sys.executable, "-m", "stopline.targets.router",
                               "--port", str(ROUTER_PORT), "--store", str(store)])
    wait_for_port(ROUTER_PORT)
    gate = build_gate(database)
    gate.setup()
    targets = build_targets(CLUSTER)
    sender = build_sender(gate, targets)
    stage = build_stage(database, gate, targets, sender, cluster=CLUSTER)
    app = FastAPI()
    app.include_router(sender_routes(sender))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PAGE_PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    wait_for_port(PAGE_PORT)
    os.environ["STOPLINE_SENDER_URL"] = f"http://127.0.0.1:{PAGE_PORT}"
    try:
        yield SimpleNamespace(db=database, gate=gate, targets=targets, sender=sender, stage=stage,
                              reset=lambda: reset_all(targets))
    finally:
        server.should_exit = True
        router.terminate()
        stopline_db.drop_test_database(database)
