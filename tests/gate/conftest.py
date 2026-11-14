"""Atlas fixtures and bounded, explicit interleaving controls."""
from collections import Counter, defaultdict
from contextlib import contextmanager
import threading
import uuid

import pytest
from pymongo import monitoring

from stopline import db as storage
from stopline.contract import Action, OPERATOR, Tool, TARGET_REGION_B


class Commands(monitoring.CommandListener):
    def __init__(self):
        self.local = threading.local()
        self.counts = Counter()
        self.lock = threading.Lock()
        self.samples = defaultdict(list)
        self.failures = []
        self.pending = {}

    def started(self, event):
        label = getattr(self.local, "label", None)
        if label:
            with self.lock:
                self.counts[label] += 1
                self.local.invocation["round_trips"] += 1
                if "lsid" in event.command:
                    self.local.invocation["sessions"].add(bytes(event.command["lsid"]["id"]))
                self.pending[event.request_id] = {
                    "label": label, "command": event.command_name,
                    "collection": event.command.get(event.command_name),
                    "query": event.command.get("query"),
                    "update": event.command.get("update"),
                }

    def succeeded(self, event):
        with self.lock:
            self.pending.pop(event.request_id, None)

    def failed(self, event):
        with self.lock:
            command = self.pending.pop(event.request_id, None)
            if command:
                self.failures.append({**command, "code": event.failure.get("code")})

    @contextmanager
    def operation(self, label):
        old = getattr(self.local, "label", None)
        previous_invocation = getattr(self.local, "invocation", None)
        invocation = {"round_trips": 0, "sessions": set()}
        self.local.label = label
        self.local.invocation = invocation
        try:
            yield
        finally:
            self.local.label = old
            self.local.invocation = previous_invocation
            with self.lock:
                self.samples[label].append(invocation)


commands = Commands()
monitoring.register(commands)  # registered before stopline.db creates its client


@pytest.fixture(scope="session")
def database():
    db = storage.test_database("gate")
    try:
        yield db
    finally:
        storage.drop_test_database(db)


@pytest.fixture
def gate(database):
    import stopline.gate as package
    assert hasattr(package, "build_gate"), "the gate factory has not been implemented"
    instance = package.build_gate(database)
    with commands.operation("setup"):
        instance.setup()
    return instance


@pytest.fixture(autouse=True)
def round_trips(request):
    commands.counts.clear()
    commands.samples.clear()
    commands.failures.clear()
    yield
    report = ", ".join(f"{name}={[s['round_trips'] for s in samples]}"
                       for name, samples in sorted(commands.samples.items()))
    # The required command uses pytest's default capture; write through its reporter.
    reporter = request.config.pluginmanager.getplugin("terminalreporter")
    line = f"ROUND TRIPS {request.node.name}: {report}"
    if reporter:
        reporter.write_line(line)
    else:
        print(line)


@pytest.fixture
def run_id():
    return uuid.uuid4().hex


def root(gate, run_id, **overrides):
    kwargs = dict(run_id=run_id, grantor=OPERATOR, holder=OPERATOR,
                  tools=[Tool.SHIFT], targets=[TARGET_REGION_B],
                  limits={"max_actions": 20, "max_shift_percent": 50}, ttl_seconds=3600)
    kwargs.update(overrides)
    with commands.operation("grant_root"):
        return gate.grant_root(**kwargs)


def child(gate, parent, holder="worker", **overrides):
    kwargs = dict(parent_id=parent["_id"], holder=holder, tools=parent["tools"],
                  targets=parent["targets"], limits=dict(parent["limits"]), ttl_seconds=1800)
    kwargs.update(overrides)
    with commands.operation("hand_down"):
        result = gate.hand_down(**kwargs)
    assert result.allowed, result
    return gate.db.mandates.find_one({"_id": result.mandate_id})


def action(mandate, suffix="a", **overrides):
    kwargs = dict(action_id=f'{mandate["run_id"]}-{suffix}', run_id=mandate["run_id"],
                  mandate_id=mandate["_id"], principal=mandate["holder"], tool=Tool.SHIFT,
                  target=TARGET_REGION_B, args={"percent": 25})
    kwargs.update(overrides)
    return Action(**kwargs)


def call(label, fn, *args, **kwargs):
    with commands.operation(label):
        return fn(*args, **kwargs)


class Pause:
    """One-shot pause; always released in finally and never sleeps to schedule a race."""
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def hold(self):
        self.entered.set()
        assert self.release.wait(15), "interleaving pause was not released"

    def wait(self):
        assert self.entered.wait(15), "interleaving pause was not reached"
