"""Database-enforced audit rules and bounded stop work."""
from uuid import uuid4

import pytest
from pymongo.collection import Collection
from pymongo.errors import DuplicateKeyError, WriteError

from stopline import db as storage
from stopline.contract import NotAuthorised, OPERATOR, Reason
from stopline.gate import build_gate
from conftest import action, call, root


@pytest.mark.parametrize("authorised", [True, False])
def test_stop_cancels_before_reading_authority_and_rolls_back_refusal(gate, run_id, monkeypatch, authorised):
    mandate = root(gate, run_id)
    proposed = action(mandate)
    assert call("admit", gate.admit, proposed).allowed
    original = Collection.find_one
    observed = []

    def reading(collection, query=None, *args, **kwargs):
        if collection.full_name == gate.db.mandates.full_name and kwargs.get("session") and not observed:
            effect = original(gate.db.effects, {"_id": proposed.action_id}, session=kwargs["session"])
            observed.append(effect["state"])
            assert effect["state"] == "cancelled", "cancel must precede the first authority read"
        return original(collection, query, *args, **kwargs)

    monkeypatch.setattr(Collection, "find_one", reading)
    if authorised:
        receipt = call("stop", gate.stop, mandate_id=mandate["_id"], by=OPERATOR)
        assert receipt["covered"] == [proposed.action_id]
    else:
        with pytest.raises(NotAuthorised):
            call("stop_refused", gate.stop, mandate_id=mandate["_id"], by="stranger")
    effect = gate.db.effects.find_one({"_id": proposed.action_id})
    assert observed == ["cancelled"]
    assert effect["state"] == ("cancelled" if authorised else "admitted")


def test_root_action_budget_is_bounded(gate, run_id):
    assert root(gate, run_id, limits={"max_actions": 1000, "max_shift_percent": 50})
    with pytest.raises(ValueError):
        root(gate, run_id, limits={"max_actions": 1001, "max_shift_percent": 50})


def test_replay_audit_fills_every_committed_counter(gate, run_id):
    mandate = root(gate, run_id)
    proposed = action(mandate)
    first = call("admit", gate.admit, proposed)
    replay = call("replay", gate.admit, proposed)
    receipt = call("stop", gate.stop, mandate_id=mandate["_id"], by=OPERATOR)
    rows = list(gate.db.audit.find({"root_id": mandate["_id"], "kind": {"$in": ["allow", "hand_down", "stop"]}}).sort("seq", 1))
    assert [row["seq"] for row in rows] == list(range(1, receipt["stop_seq"] + 1))
    assert rows[1]["detail"] == {"replayed": True, "admitted_seq": first.seq}
    assert replay.allowed and replay.replayed and replay.seq == first.seq
    assert gate.db.effects.count_documents({"_id": proposed.action_id}) == 1


def test_validator_checks_kind_specific_fields(gate, run_id):
    mandate = root(gate, run_id)
    proposed = action(mandate)
    assert call("admit", gate.admit, proposed).allowed
    entry = gate.db.audit.find_one({"action_id": proposed.action_id, "kind": "allow"})
    for change in ({"action_id": None}, {"reason": "revoked"}, {"root_id": None},
                   {"mandate_id": ""}, {"principal": ""}, {"run_id": ""}, {"seq": -1},
                   {"policy_hash": "x"}, {"policy_version": 0}, {"kind": "deny", "reason": "ok"},
                   {"kind": "stop", "action_id": proposed.action_id}):
        with pytest.raises(WriteError) as rejected:
            call("invalid_audit", gate.db.audit.insert_one, {**entry, "_id": uuid4().hex, **change})
        assert rejected.value.code == 121


def test_decision_index_rejects_copies_but_allows_nondecisions(gate, run_id):
    mandate = root(gate, run_id)
    proposed = action(mandate)
    assert call("admit", gate.admit, proposed).allowed
    entry = gate.db.audit.find_one({"action_id": proposed.action_id, "kind": "allow"})
    with pytest.raises(DuplicateKeyError):
        call("duplicate_decision", gate.db.audit.insert_one, {**entry, "_id": uuid4().hex})
    call("deny_same_position", gate.db.audit.insert_one,
         {**entry, "_id": uuid4().hex, "kind": "deny", "reason": Reason.NOT_ADMITTED})
    assert call("claim", gate.claim, proposed.action_id, "sender").claimed


def test_setup_skips_unique_index_when_legacy_decisions_are_duplicated():
    db = storage.test_database("gate")
    try:
        db.audit.insert_many([{"kind": "allow", "root_id": "legacy", "seq": 1},
                              {"kind": "stop", "root_id": "legacy", "seq": 1}])
        gate = build_gate(db)
        call("setup_legacy", gate.setup)
        assert "one_decision_per_counter" not in db.audit.index_information()
        assert db.audit.count_documents({"root_id": "legacy"}) == 2
    finally:
        storage.drop_test_database(db)


def test_stop_wins_claim_after_its_first_cancel(gate, run_id, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from pymongo.errors import OperationFailure
    from conftest import Pause, commands

    mandate = root(gate, run_id)
    proposed = action(mandate)
    assert call("admit", gate.admit, proposed).allowed
    pause = Pause()
    original_cancel = Collection.update_many
    original_claim = Collection.find_one_and_update

    def held_cancel(collection, *args, **kwargs):
        result = original_cancel(collection, *args, **kwargs)
        if collection.full_name == gate.db.effects.full_name:
            pause.hold()
        return result

    def claim_conflict(collection, *args, **kwargs):
        try:
            return original_claim(collection, *args, **kwargs)
        except OperationFailure as error:
            if collection.full_name == gate.db.effects.full_name and error.has_error_label("TransientTransactionError"):
                pause.release.set()
            raise

    monkeypatch.setattr(Collection, "update_many", held_cancel)
    monkeypatch.setattr(Collection, "find_one_and_update", claim_conflict)
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(call, "stop", gate.stop, mandate_id=mandate["_id"], by=OPERATOR)
        try:
            pause.wait()
            claimed = call("claim", gate.claim, proposed.action_id, "sender")
        finally:
            pause.release.set()
        receipt = pending.result(timeout=20)
    assert not claimed.claimed and claimed.reason == Reason.CANCELLED_BY_STOP
    assert receipt["covered"] == [proposed.action_id] and receipt["status"] == "closed"
    assert not any(f["label"] == "stop" for f in commands.failures)
    assert any(f["label"] == "claim" and f["code"] == 112 for f in commands.failures)


def test_undo_rows_and_stage_validation_remain_compatible(gate, run_id):
    from stopline.agents.stage import Stage
    from stopline.contract import Inverse, Tool
    from conftest import child
    from test_core import evidence

    parent = root(gate, run_id)
    mandate = child(gate, parent)
    proposed = action(mandate)
    assert call("admit", gate.admit, proposed).allowed
    assert call("claim", gate.claim, proposed.action_id, "sender").claimed
    call("outcome", gate.record_outcome, proposed.action_id, "applied", evidence(proposed.action_id))

    class Target:
        def inverse_of(self, original):
            return Inverse(Tool.SHIFT, original.target, {"percent": 0}, {"weights": {"region-b": 25}})

        def state(self):
            return {"reachable": True, "weights": {"region-b": 25}}

    kwargs = dict(mandate_id=parent["_id"], principal=OPERATOR, targets={"router": Target()}, undo_id=f"{run_id}-undo")
    assert call("undo", gate.undo, proposed.action_id, **kwargs).allowed
    assert call("undo_replay", gate.undo, proposed.action_id, **kwargs).replayed
    assert gate.db.audit.count_documents({"action_id": kwargs["undo_id"], "kind": "undo"}) == 2
    assert not call("undo_unknown", gate.undo, "missing", **{**kwargs, "undo_id": f"{run_id}-missing"}).allowed
    stage = Stage(gate.db, gate, {}, None, cluster="mock")
    result = call("validation_key", stage._validate, run_id)
    assert result.ok and result.detail == {"code": 121, "rejected": True}
