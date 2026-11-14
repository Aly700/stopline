"""P5 and P6: global identities and shared authority budgets."""

import pytest

from stopline.contract import Reason, TARGET_API, Tool
from concurrency_helpers import action, at_method, audit, child, delegation, interleave, root, snapshot


@pytest.mark.parametrize("changed", [False, True])
def test_same_action_id_commits_once(concurrency_gate, monkeypatch, changed):
    gate = concurrency_gate
    mandate = root(gate)
    first = action(mandate)
    second = action(mandate, args={"percent": 30} if changed else first.args)
    pause = at_method(monkeypatch, gate.db.effects, "find_one",
                      lambda args, kwargs: args[0] == {"_id": first.action_id})
    loser, winner = interleave(pause, lambda: gate.admit(first), lambda: gate.admit(second))
    assert pause.values[0] is None and pause.calls >= 2
    assert winner.allowed
    assert gate.db.effects.count_documents({"_id": first.action_id}) == 1
    assert len(audit(gate, first, "allow")) == 1
    assert gate.db.mandates.find_one({"_id": mandate["_id"]})["seq"] >= winner.seq
    if changed:
        assert not loser.allowed and loser.reason == Reason.SAME_ID_DIFFERENT_ARGS
        assert len(audit(gate, first, "deny")) == 1
    else:
        assert loser.allowed and loser.replayed and loser.seq == winner.seq
        assert not audit(gate, first, "deny")


def test_same_action_id_under_two_roots_refuses_the_loser(concurrency_gate, monkeypatch):
    gate = concurrency_gate
    first_root, second_root = root(gate), root(gate)
    first = action(first_root)
    second = action(second_root, action_id=first.action_id)
    pause = at_method(monkeypatch, gate.db.effects, "find_one",
                      lambda args, kwargs: args[0] == {"_id": first.action_id})
    loser, winner = interleave(pause, lambda: gate.admit(first), lambda: gate.admit(second))
    assert pause.values[0] is None
    assert winner.allowed
    assert not loser.allowed and loser.reason == Reason.SAME_ID_DIFFERENT_ARGS
    effects = list(gate.db.effects.find({"_id": first.action_id}))
    assert len(effects) == 1 and effects[0]["root_id"] == second_root["_id"]
    assert gate.db.mandates.find_one({"_id": first_root["_id"]})["seq"] == 0
    assert len(audit(gate, first, "allow")) == len(audit(gate, first, "deny")) == 1


def test_two_hand_downs_cannot_overallocate_parent(concurrency_gate, monkeypatch):
    gate = concurrency_gate
    parent = root(gate, budget=3)
    pause = snapshot(monkeypatch, gate, parent)
    loser, winner = interleave(
        pause, lambda: gate.hand_down(**delegation(parent, budget=2, holder="one", ttl_seconds=7200)),
        lambda: gate.hand_down(**delegation(parent, budget=2, holder="two", ttl_seconds=7200)))
    assert winner.allowed
    assert not loser.allowed and loser.reason == Reason.LIMIT_EXCEEDED
    assert loser.attempts >= 2
    children = list(gate.db.mandates.find({"parent_id": parent["_id"]}))
    assert len(children) == 1
    assert sum(item["limits"]["max_actions"] for item in children) <= parent["limits"]["max_actions"]
    assert children[0]["expires_at"] == parent["expires_at"]


def test_sibling_admissions_share_last_parent_slot(concurrency_gate, monkeypatch):
    gate = concurrency_gate
    parent = root(gate, budget=2)
    one, two = child(gate, parent, budget=1, holder="one"), child(gate, parent, budget=1, holder="two")
    assert gate.admit(action(parent, "parent-spent-one")).allowed
    first, second = action(one, "first-child"), action(two, "second-child")
    pause = snapshot(monkeypatch, gate, one)
    loser, winner = interleave(pause, lambda: gate.admit(first), lambda: gate.admit(second))
    assert winner.allowed
    assert not loser.allowed and loser.reason == Reason.LIMIT_EXCEEDED
    assert loser.attempts >= 2
    assert gate.db.effects.count_documents({"root_id": parent["_id"]}) == 2
    assert gate.db.effects.count_documents({"mandate_id": one["_id"]}) == 0
    assert gate.db.effects.count_documents({"mandate_id": two["_id"]}) == 1
    assert len(audit(gate, first, "deny")) == 1


def test_concurrent_hand_down_cannot_widen_scope(concurrency_gate, monkeypatch):
    gate = concurrency_gate
    parent = root(gate)
    pause = snapshot(monkeypatch, gate, parent)
    wider_scopes = [
        {"tools": [Tool.SHIFT, Tool.RESTART]},
        {"targets": [TARGET_API]},
        {"limits": {"max_actions": 21, "max_shift_percent": 50}},
        {"limits": {"max_actions": 1, "max_shift_percent": 51}},
    ]
    denied, allowed = interleave(
        pause, lambda: [gate.hand_down(**delegation(parent, budget=1, **wider)) for wider in wider_scopes],
        lambda: gate.hand_down(**delegation(parent, budget=1, holder="peer")))
    assert allowed.allowed
    assert all(not result.allowed and result.reason == Reason.WIDER_THAN_PARENT for result in denied)
    assert denied[0].attempts >= 2
    children = list(gate.db.mandates.find({"parent_id": parent["_id"]}))
    assert len(children) == 1
    child_doc = children[0]
    assert set(child_doc["tools"]) <= set(parent["tools"])
    assert set(child_doc["targets"]) <= set(parent["targets"])
    assert all(child_doc["limits"][key] <= value for key, value in parent["limits"].items())
