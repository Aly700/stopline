"""Bounded schedules over real collection calls; no sleeps or storage doubles."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import timedelta, timezone
import threading
import time
from uuid import uuid4

from pymongo.collection import Collection
from pymongo.errors import OperationFailure

from stopline.contract import Action, OPERATOR, TARGET_REGION_B, Tool


_actor = threading.local()
WAIT_SECONDS = 8


@dataclass
class Pause:
    entered: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    calls: int = 0
    values: list = field(default_factory=list)

    def hold(self):
        self.entered.set()
        assert self.release.wait(WAIT_SECONDS), "held operation was not released"

    def wait(self):
        assert self.entered.wait(WAIT_SECONDS), "operation did not reach its checkpoint"


def is_held_actor():
    return getattr(_actor, "held", False)


def interleave(pause, held, other):
    """Worker reaches checkpoint; caller completes other; worker resumes.

    The caller and worker are the two actors. Release happens even if other
    raises. Future.result propagates worker assertions and database errors.
    """
    def run():
        _actor.held = True
        try:
            return held()
        finally:
            _actor.held = False

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="concurrency") as pool:
        pending = pool.submit(run)
        try:
            pause.wait()
            completed = other()
        finally:
            pause.release.set()
        return pending.result(timeout=WAIT_SECONDS), completed


def interleave_until_write_or_conflict(monkeypatch, pause, collection, held, other):
    """Let other commit, or release held when other proves a real write conflict.

    The second branch lets the same property test finish after a correction
    adds the missing root write. Waiting for a conflicting writer to commit
    while holding that write would deadlock the corrected implementation.
    """
    ready = threading.Event()
    original = Collection.find_one_and_update

    def watch_conflict(current, *args, **kwargs):
        try:
            return original(current, *args, **kwargs)
        except OperationFailure as error:
            if (not is_held_actor() and current.full_name == collection.full_name
                    and error.has_error_label("TransientTransactionError")):
                ready.set()
                assert pause.release.wait(WAIT_SECONDS), "conflicting writer was not released"
            raise

    monkeypatch.setattr(Collection, "find_one_and_update", watch_conflict)

    def run_held():
        _actor.held = True
        try:
            return held()
        finally:
            _actor.held = False

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="concurrency") as pool:
        first = pool.submit(run_held)
        try:
            pause.wait()
            second = pool.submit(other)
            second.add_done_callback(lambda future: ready.set())
            assert ready.wait(WAIT_SECONDS), "other operation neither committed nor conflicted"
        finally:
            pause.release.set()
        return first.result(timeout=WAIT_SECONDS), second.result(timeout=WAIT_SECONDS)


def at_method(monkeypatch, collection, method, predicate=lambda args, kwargs: True,
              *, before=False):
    """Pause only the first matching call from the held actor, observe retries.

    Every call, argument and return value reaches the original PyMongo method.
    A find_one checkpoint after the call pins a real transaction snapshot.
    """
    original = getattr(Collection, method)
    pause = Pause()

    def wrapped(current, *args, **kwargs):
        matches = (is_held_actor() and current.full_name == collection.full_name
                   and predicate(args, kwargs))
        if matches:
            pause.calls += 1
        first = matches and pause.calls == 1
        if first and before:
            pause.hold()
        value = original(current, *args, **kwargs)
        if matches:
            pause.values.append(value)
        if first and not before:
            pause.hold()
        return value

    monkeypatch.setattr(Collection, method, wrapped)
    return pause


def snapshot(monkeypatch, gate, mandate):
    return at_method(monkeypatch, gate.db.mandates, "find_one",
                     lambda args, kwargs: args[0] == {"_id": mandate["_id"]}
                     and kwargs.get("session") is not None)


def root(gate, *, budget=20, **overrides):
    options = dict(run_id=uuid4().hex, grantor=OPERATOR, holder=OPERATOR,
                   tools=[Tool.SHIFT], targets=[TARGET_REGION_B],
                   limits={"max_actions": budget, "max_shift_percent": 50}, ttl_seconds=3600)
    options.update(overrides)
    mandate = gate.grant_root(**options)
    # hello's localTime is naive UTC; ordinary collection reads decode aware UTC.
    for key in ("clock", "created_at", "expires_at"):
        if mandate[key].tzinfo is None:
            mandate[key] = mandate[key].replace(tzinfo=timezone.utc)
    return mandate


def delegation(parent, *, budget=None, **overrides):
    options = dict(parent_id=parent["_id"], holder="worker", tools=list(parent["tools"]),
                   targets=list(parent["targets"]), limits=dict(parent["limits"]),
                   ttl_seconds=1800)
    if budget is not None:
        options["limits"]["max_actions"] = budget
    options.update(overrides)
    return options


def child(gate, parent, **overrides):
    """Seed a valid pre-existing descendant without spending setup transactions.

    Delegation races themselves call hand_down. This fixture derives a narrower
    record from its parent and persists it in the real test database.
    """
    options = delegation(parent, **overrides)
    identity = options.get("mandate_id", uuid4().hex)
    mandate = {key: value for key, value in parent.items() if key not in ("seq", "clock")}
    mandate.update(_id=identity, parent_id=parent["_id"], depth=parent["depth"] + 1,
                   grantor=parent["holder"], holder=options["holder"], tools=options["tools"],
                   targets=options["targets"], limits=options["limits"],
                   expires_at=min(parent["expires_at"], parent["created_at"] + timedelta(seconds=options["ttl_seconds"])))
    assert set(mandate["tools"]) <= set(parent["tools"])
    assert set(mandate["targets"]) <= set(parent["targets"])
    assert all(mandate["limits"][key] <= value for key, value in parent["limits"].items())
    gate.db.mandates.insert_one(mandate)
    return mandate


def action(mandate, suffix="a", **overrides):
    options = dict(action_id=f'{mandate["run_id"]}-{suffix}', run_id=mandate["run_id"],
                   mandate_id=mandate["_id"], principal=mandate["holder"], tool=Tool.SHIFT,
                   target=TARGET_REGION_B, args={"percent": 25})
    options.update(overrides)
    return Action(**options)


def evidence(identity):
    return {"action_id": identity, "system": "router", "at_epoch": time.time()}


def sent(gate, mandate, suffix="a"):
    proposed = action(mandate, suffix)
    assert gate.admit(proposed).allowed
    assert gate.claim(proposed.action_id, "sender").claimed
    return proposed


def audit(gate, proposed, kind):
    return list(gate.db.audit.find({"action_id": proposed.action_id, "kind": kind}))
