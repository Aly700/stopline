"""The database behaviours the design rests on, checked against the real cluster."""

import threading
import time

import pytest
from pymongo.errors import OperationFailure, WriteError
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern

from stopline import db as stopline_db

SNAPSHOT = dict(read_concern=ReadConcern("snapshot"), write_concern=WriteConcern("majority"))
TICK = [{"$set": {"seq": {"$add": ["$seq", 1]}, "clock": "$$NOW"}}]


@pytest.fixture(scope="module")
def db():
    database = stopline_db.test_database("sandbox")
    yield database
    stopline_db.drop_test_database(database)


def test_two_transactions_writing_one_document_conflict(db):
    db.roots.insert_one({"_id": "r1", "seq": 0})
    client = db.client
    with client.start_session() as a, client.start_session() as b:
        a.start_transaction(**SNAPSHOT)
        b.start_transaction(**SNAPSHOT)
        db.roots.update_one({"_id": "r1"}, TICK, session=a)
        with pytest.raises(OperationFailure) as conflict:
            db.roots.update_one({"_id": "r1"}, TICK, session=b)
        assert conflict.value.has_error_label("TransientTransactionError")
        b.abort_transaction()
        a.commit_transaction()
    assert db.roots.find_one({"_id": "r1"})["seq"] == 1


def test_reading_a_document_does_not_conflict(db):
    db.parents.insert_one({"_id": "p1", "status": "active"})
    client = db.client
    with client.start_session() as reader:
        reader.start_transaction(**SNAPSHOT)
        assert db.parents.find_one({"_id": "p1"}, session=reader)["status"] == "active"
        db.parents.update_one({"_id": "p1"}, {"$set": {"status": "revoked"}})
        db.children.insert_one({"_id": "c1", "parent": "p1"}, session=reader)
        reader.commit_transaction()
    # The child was created from a stale read of its parent. Reads protect nothing.
    assert db.children.find_one({"_id": "c1"}) is not None
    assert db.parents.find_one({"_id": "p1"})["status"] == "revoked"


def test_the_losing_transaction_retries_and_sees_the_new_state(db):
    db.roots.insert_one({"_id": "r2", "seq": 0, "status": "active"})
    first_has_written = threading.Event()
    let_first_commit = threading.Event()
    seen = []

    def stopper(session):
        db.roots.update_one({"_id": "r2"}, [{"$set": {"seq": {"$add": ["$seq", 1]}, "status": "revoked"}}], session=session)
        first_has_written.set()
        let_first_commit.wait(10)

    def admitter(session):
        root = db.roots.find_one_and_update({"_id": "r2"}, TICK, session=session, return_document=True)
        seen.append(root["status"])
        return root["status"]

    thread = threading.Thread(target=lambda: stopline_db.run_transaction(stopper))
    thread.start()
    assert first_has_written.wait(10)
    threading.Timer(0.5, let_first_commit.set).start()
    result, attempts = stopline_db.run_transaction(admitter)
    thread.join()
    assert result == "revoked"
    assert attempts >= 2, "the admitter should have been retried by the driver"
    assert db.roots.find_one({"_id": "r2"})["seq"] == 2


def test_server_time_is_available_inside_a_transaction(db):
    db.roots.insert_one({"_id": "r3", "seq": 0})

    def tick(session):
        return db.roots.find_one_and_update({"_id": "r3"}, TICK, session=session, return_document=True)

    root, _ = stopline_db.run_transaction(tick)
    assert root["seq"] == 1 and root["clock"].tzinfo is not None


def test_a_conditional_update_claims_once(db):
    db.effects.insert_one({"_id": "e1", "state": "admitted"})
    first = db.effects.update_one({"_id": "e1", "state": "admitted"}, {"$set": {"state": "sent"}})
    second = db.effects.update_one({"_id": "e1", "state": "admitted"}, {"$set": {"state": "sent"}})
    assert (first.modified_count, second.modified_count) == (1, 0)


def test_a_validator_rejects_a_malformed_entry(db):
    db.create_collection(
        "audit",
        validator={"$jsonSchema": {"bsonType": "object", "required": ["kind", "principal", "at"],
                                   "properties": {"kind": {"enum": ["allow", "deny"]}}}},
    )
    db.audit.insert_one({"kind": "allow", "principal": "agent:worker-1", "at": 1})
    with pytest.raises(WriteError) as rejected:
        db.audit.insert_one({"kind": "maybe", "principal": "agent:worker-1"})
    assert rejected.value.code == 121


def test_a_change_stream_delivers_an_insert(db):
    db.create_collection("feed")
    with db.feed.watch(max_await_time_ms=500) as stream:
        db.feed.insert_one({"_id": "row-1"})
        deadline = time.time() + 10
        change = None
        while change is None and time.time() < deadline:
            change = stream.try_next()
    assert change is not None and change["documentKey"]["_id"] == "row-1"


def test_graph_lookup_finds_a_subtree(db):
    db.mandates.insert_many([
        {"_id": "m0", "parent_id": None}, {"_id": "m1", "parent_id": "m0"},
        {"_id": "m2", "parent_id": "m1"}, {"_id": "m3", "parent_id": "m1"}, {"_id": "x", "parent_id": None},
    ])
    found = list(db.mandates.aggregate([
        {"$match": {"_id": "m1"}},
        {"$graphLookup": {"from": "mandates", "startWith": "$_id", "connectFromField": "_id",
                          "connectToField": "parent_id", "as": "below"}},
    ]))
    assert sorted(m["_id"] for m in found[0]["below"]) == ["m2", "m3"]
