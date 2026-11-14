"""Named schedules on real sessions. No probabilistic race loops or server failpoints."""
from concurrent.futures import ThreadPoolExecutor

import pytest
from pymongo.collection import Collection
from pymongo.errors import OperationFailure

from stopline.contract import MAX_DEPTH, MAX_TREE_SIZE, OPERATOR, Reason, Tool
from stopline.gate.core import TICK
from conftest import Pause, action, call, child, commands, root


def hold_first_mandate_read(monkeypatch, gate, mandate_id):
    """Pause after a real snapshot read, immediately before the shared root write."""
    pause = Pause()
    original = Collection.find_one
    armed = True

    def reading(collection, query=None, *args, **kwargs):
        nonlocal armed
        result = original(collection, query, *args, **kwargs)
        if (armed and collection.full_name == gate.db.mandates.full_name
                and query == {"_id": mandate_id} and kwargs.get("session")):
            armed = False
            pause.hold()
        return result

    monkeypatch.setattr(Collection, "find_one", reading)
    return pause


def assert_root_conflict(label, root_id):
    assert any(f["label"] == label and f["command"] == "findAndModify"
               and f["collection"] == "mandates" and f["query"] == {"_id": root_id}
               and f["update"] == TICK and f["code"] == 112 for f in commands.failures), commands.failures


def assert_distinct_sessions(first, second):
    first_ids = commands.samples[first][-1]["sessions"]
    second_ids = commands.samples[second][-1]["sessions"]
    assert first_ids and second_ids and first_ids.isdisjoint(second_ids)


@pytest.mark.parametrize("winner", ["parent_stop", "child_admit"])
def test_2_parent_stop_and_child_admit_conflict_on_root(gate, run_id, monkeypatch, winner):
    operator = root(gate, run_id)
    repair = child(gate, operator, "repair")
    worker = child(gate, repair)
    proposed = action(worker)
    paused_id = worker["_id"] if winner == "parent_stop" else repair["_id"]
    pause = hold_first_mandate_read(monkeypatch, gate, paused_id)
    with ThreadPoolExecutor(1) as pool:
        if winner == "parent_stop":
            pending = pool.submit(call, "child_admit", gate.admit, proposed)
        else:
            pending = pool.submit(call, "parent_stop", gate.stop, mandate_id=repair["_id"], by=OPERATOR)
        try:
            pause.wait()
            if winner == "parent_stop":
                receipt = call("parent_stop", gate.stop, mandate_id=repair["_id"], by=OPERATOR)
            else:
                admission = call("child_admit", gate.admit, proposed)
        finally:
            pause.release.set()
        result = pending.result(timeout=20)
    if winner == "parent_stop":
        admission = result
        assert not admission.allowed and admission.reason == Reason.REVOKED and admission.attempts == 2
        assert_root_conflict("child_admit", operator["_id"])
        assert not any(f["label"] == "parent_stop" for f in commands.failures)
    else:
        receipt = result
        assert admission.allowed and admission.attempts == 1
        assert_root_conflict("parent_stop", operator["_id"])
        assert not any(f["label"] == "child_admit" for f in commands.failures)
    assert not admission.allowed or proposed.action_id in receipt["covered"]
    assert_distinct_sessions("parent_stop", "child_admit")
    assert gate.db.mandates.find_one({"_id": operator["_id"]})["status"] == "active"


@pytest.mark.parametrize("winner", ["stop", "hand_down"])
def test_3_hand_down_and_parent_stop(gate, run_id, monkeypatch, winner):
    operator = root(gate, run_id)
    repair = child(gate, operator, "repair")
    pause = hold_first_mandate_read(monkeypatch, gate, repair["_id"])
    kwargs = dict(parent_id=repair["_id"], holder="worker", tools=repair["tools"],
                  targets=repair["targets"], limits=repair["limits"], ttl_seconds=600)
    with ThreadPoolExecutor(1) as pool:
        if winner == "stop":
            pending = pool.submit(call, "delegation", gate.hand_down, **kwargs)
        else:
            pending = pool.submit(call, "parent_stop", gate.stop, mandate_id=repair["_id"], by=OPERATOR)
        try:
            pause.wait()
            if winner == "stop":
                receipt = call("parent_stop", gate.stop, mandate_id=repair["_id"], by=OPERATOR)
            else:
                handed = call("delegation", gate.hand_down, **kwargs)
        finally:
            pause.release.set()
        result = pending.result(timeout=20)
    if winner == "stop":
        handed = result
        assert not handed.allowed and handed.reason == Reason.REVOKED and handed.attempts == 2
        assert_root_conflict("delegation", operator["_id"])
        assert gate.db.mandates.count_documents({"parent_id": repair["_id"]}) == 0
        assert gate.db.audit.find_one({"run_id": run_id, "kind": "deny"})
    else:
        receipt = result
        assert handed.allowed
        assert handed.mandate_id in receipt["revoked"]
        assert gate.db.mandates.find_one({"_id": handed.mandate_id})["status"] == "revoked"
        assert_root_conflict("parent_stop", operator["_id"])
    assert_distinct_sessions("parent_stop", "delegation")


def test_5_transient_abort_rechecks_the_entire_chain(gate, run_id, monkeypatch):
    operator = root(gate, run_id)
    repair = child(gate, operator, "repair")
    worker = child(gate, repair)
    proposed = action(worker)
    pause = Pause()
    original_insert = Collection.insert_one
    original_find = Collection.find_one
    injected = False
    reads = 0

    def transient_after_check(collection, document, *args, **kwargs):
        nonlocal injected
        if collection.full_name == gate.db.effects.full_name and not injected:
            injected = True
            raise OperationFailure("forced transient abort after authority check", code=112,
                                   details={"errorLabels": ["TransientTransactionError"]})
        return original_insert(collection, document, *args, **kwargs)

    def hold_retry_before_its_first_read(collection, query=None, *args, **kwargs):
        nonlocal reads
        if collection.full_name == gate.db.effects.full_name and kwargs.get("session"):
            reads += 1
            if reads == 2:
                pause.hold()  # the first attempt is aborted; its root lock is released
        return original_find(collection, query, *args, **kwargs)

    monkeypatch.setattr(Collection, "insert_one", transient_after_check)
    monkeypatch.setattr(Collection, "find_one", hold_retry_before_its_first_read)
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(call, "admit_retry", gate.admit, proposed)
        try:
            pause.wait()
            receipt = call("stop_between_attempts", gate.stop, mandate_id=repair["_id"], by=OPERATOR)
        finally:
            pause.release.set()
        result = pending.result(timeout=20)
    assert injected and result.attempts == 2
    assert not result.allowed and result.reason == Reason.REVOKED
    assert proposed.action_id not in receipt["covered"]
    assert gate.db.effects.find_one({"_id": proposed.action_id}) is None
    assert gate.db.audit.count_documents({"action_id": proposed.action_id, "kind": "allow"}) == 0
    assert gate.db.audit.count_documents({"action_id": proposed.action_id, "kind": "deny"}) == 1
    assert gate.db.mandates.find_one({"_id": operator["_id"]})["seq"] == receipt["stop_seq"]
    assert_distinct_sessions("admit_retry", "stop_between_attempts")


def test_6_expiry_uses_server_time_on_the_retried_admission(gate, run_id, monkeypatch):
    operator = root(gate, run_id)
    worker = child(gate, operator)
    pause = hold_first_mandate_read(monkeypatch, gate, worker["_id"])
    # The client's helper must never supply the admission decision's clock.
    monkeypatch.setattr(gate, "_now", lambda: pytest.fail("admission used a client-side clock read"))
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(call, "admit_expiring", gate.admit, action(worker))
        try:
            pause.wait()
            with gate.db.client.start_session() as other:
                with commands.operation("expire_on_server"):
                    expired = gate.db.mandates.find_one_and_update(
                        {"_id": operator["_id"]}, [{"$set": {"expires_at": "$$NOW"}}],
                        return_document=True, session=other)
        finally:
            pause.release.set()
        result = pending.result(timeout=20)
    assert not result.allowed and result.reason == Reason.EXPIRED and result.attempts == 2
    deny = gate.db.audit.find_one({"action_id": action(worker).action_id})
    assert deny["at"] >= expired["expires_at"]
    assert deny["detail"]["expired_mandate"] == operator["_id"]
    assert_root_conflict("admit_expiring", operator["_id"])
    assert_distinct_sessions("admit_expiring", "expire_on_server")


def test_7_sibling_delegations_share_parent_budget(gate, run_id, monkeypatch):
    operator = root(gate, run_id, limits={"max_actions": 3, "max_shift_percent": 50})
    pause = hold_first_mandate_read(monkeypatch, gate, operator["_id"])
    kwargs = dict(parent_id=operator["_id"], tools=operator["tools"], targets=operator["targets"],
                  limits={"max_actions": 2, "max_shift_percent": 25}, ttl_seconds=600)
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(call, "child_two", gate.hand_down, holder="two", **kwargs)
        try:
            pause.wait()
            first = call("child_one", gate.hand_down, holder="one", **kwargs)
        finally:
            pause.release.set()
        second = pending.result(timeout=20)
    assert first.allowed and first.attempts == 1
    assert not second.allowed and second.reason == Reason.LIMIT_EXCEEDED and second.attempts == 2
    assert_root_conflict("child_two", operator["_id"])
    assert_distinct_sessions("child_one", "child_two")
    assert sum(m["limits"]["max_actions"] for m in gate.db.mandates.find(
        {"parent_id": operator["_id"]})) == 2


@pytest.mark.parametrize("change", [
    {"tools": [Tool.RESTART]}, {"targets": ["cluster/checkout-api"]},
    {"limits": {"max_actions": 4, "max_shift_percent": 50}},
    {"limits": {"max_actions": 1, "max_shift_percent": 51}},
])
def test_7_child_scope_is_no_wider(gate, run_id, change):
    parent = root(gate, run_id, limits={"max_actions": 3, "max_shift_percent": 50})
    kwargs = dict(parent_id=parent["_id"], holder="worker", tools=parent["tools"],
                  targets=parent["targets"], limits=parent["limits"], ttl_seconds=600)
    result = call("hand_down_wider", gate.hand_down, **{**kwargs, **change})
    assert not result.allowed and result.reason == Reason.WIDER_THAN_PARENT
    assert gate.db.mandates.count_documents({"parent_id": parent["_id"]}) == 0


def test_7_children_and_parent_actions_share_ancestor_limit(gate, run_id, monkeypatch):
    parent = root(gate, run_id, limits={"max_actions": 2, "max_shift_percent": 50})
    one = child(gate, parent, "one", limits={"max_actions": 1, "max_shift_percent": 50})
    two = child(gate, parent, "two", limits={"max_actions": 1, "max_shift_percent": 50})
    assert call("parent_admit", gate.admit, action(parent, "parent")).allowed
    pause = hold_first_mandate_read(monkeypatch, gate, two["_id"])
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(call, "child_two_admit", gate.admit, action(two, "two"))
        try:
            pause.wait()
            first = call("child_one_admit", gate.admit, action(one, "one"))
        finally:
            pause.release.set()
        second = pending.result(timeout=20)
    assert first.allowed
    assert not second.allowed and second.reason == Reason.LIMIT_EXCEEDED and second.attempts == 2
    assert gate.db.effects.count_documents({"root_id": parent["_id"]}) == 2
    assert_root_conflict("child_two_admit", parent["_id"])


def test_depth_cap_and_child_expiry_clamping(gate, run_id):
    current = root(gate, run_id)
    for depth in range(1, MAX_DEPTH + 1):
        parent = current
        current = child(gate, current, f"depth-{depth}", ttl_seconds=7200)
        assert current["depth"] == depth
        assert current["expires_at"] == parent["expires_at"]
    result = call("hand_down_too_deep", gate.hand_down, parent_id=current["_id"], holder="too-deep",
                  tools=current["tools"], targets=current["targets"], limits=current["limits"], ttl_seconds=600)
    assert not result.allowed and result.reason == Reason.TREE_TOO_LARGE


def test_tree_size_cap_is_enforced_by_hand_down_and_stop(gate, run_id):
    parent = root(gate, run_id)
    # Seed structurally valid zero-budget siblings in one trip; exercise the cap at its boundary.
    siblings = [{**parent, "_id": f"{run_id}-m{i}", "parent_id": parent["_id"], "depth": 1,
                 "limits": {"max_actions": 0, "max_shift_percent": 0}}
                for i in range(MAX_TREE_SIZE - 1)]
    for sibling in siblings:
        sibling.pop("seq")
        sibling.pop("clock")
    gate.db.mandates.insert_many(siblings)
    result = call("hand_down_too_large", gate.hand_down, parent_id=parent["_id"], holder="extra",
                  tools=parent["tools"], targets=parent["targets"],
                  limits={"max_actions": 0, "max_shift_percent": 0}, ttl_seconds=600)
    assert not result.allowed and result.reason == Reason.TREE_TOO_LARGE
    receipt = call("stop_capped_tree", gate.stop, mandate_id=parent["_id"], by=OPERATOR)
    assert len(receipt["revoked"]) == MAX_TREE_SIZE


def test_concurrent_replay_creates_one_effect_and_two_decisions(gate, run_id, monkeypatch):
    mandate = root(gate, run_id)
    proposed = action(mandate)
    pause = hold_first_mandate_read(monkeypatch, gate, mandate["_id"])
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(call, "replayed_admit", gate.admit, proposed)
        try:
            pause.wait()
            first = call("first_admit", gate.admit, proposed)
        finally:
            pause.release.set()
        replay = pending.result(timeout=20)
    assert first.allowed and replay.allowed and replay.replayed and replay.attempts == 2
    assert first.seq == replay.seq == 1
    assert gate.db.effects.count_documents({"_id": proposed.action_id}) == 1
    assert gate.db.audit.count_documents({"action_id": proposed.action_id, "kind": "allow"}) == 2
    assert gate.db.audit.count_documents({"action_id": proposed.action_id, "detail.replayed": True}) == 1
    assert_root_conflict("replayed_admit", mandate["_id"])


def test_action_id_collision_across_roots_is_refused_and_audited(gate, run_id, monkeypatch):
    first_root = root(gate, run_id)
    second_root = root(gate, run_id)
    proposed = action(second_root)
    pause = hold_first_mandate_read(monkeypatch, gate, second_root["_id"])
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(call, "colliding_admit", gate.admit, proposed)
        try:
            pause.wait()
            first = call("winning_admit", gate.admit, action(first_root))
        finally:
            pause.release.set()
        collision = pending.result(timeout=20)
    assert first.allowed
    assert not collision.allowed and collision.reason == Reason.SAME_ID_DIFFERENT_ARGS
    assert collision.attempts == 2
    assert gate.db.effects.find_one({"_id": proposed.action_id})["root_id"] == first_root["_id"]
    deny = gate.db.audit.find_one({"action_id": proposed.action_id, "kind": "deny"})
    assert deny["root_id"] == second_root["_id"] and deny["seq"] == 1
    assert gate.db.mandates.find_one({"_id": second_root["_id"]})["seq"] == 0
