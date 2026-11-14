"""Atlas fixtures and bounded, explicit interleaving controls."""
from collections import Counter
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

    def started(self, event):
        label = getattr(self.local, "label", None)
        if label:
            with self.lock:
                self.counts[label] += 1

    def succeeded(self, event):
        pass

    def failed(self, event):
        pass

    @contextmanager
    def operation(self, label):
        old = getattr(self.local, "label", None)
        self.local.label = label
        try:
            yield
        finally:
            self.local.label = old


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
    yield
    report = ", ".join(f"{name}={count}" for name, count in sorted(commands.counts.items()))
    print(f"\nROUND TRIPS {request.node.name}: {report}")


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
