"""The one place that opens a database connection.

The connection string comes from the MONGODB_URI environment variable, or from
the file ~/.stopline_env (a line of the form MONGODB_URI='...'). It is never
printed, logged or placed in an error message.
"""

from __future__ import annotations

import os
import re
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Callable, TypeVar

import certifi
from pymongo import MongoClient
from pymongo.client_session import ClientSession
from pymongo.database import Database
from pymongo.read_concern import ReadConcern
from pymongo.read_preferences import ReadPreference
from pymongo.write_concern import WriteConcern

DB_NAME = "stopline"
TEST_PREFIX = "stopline_test_"
ENV_FILE = Path.home() / ".stopline_env"

T = TypeVar("T")


def _uri() -> str:
    uri = os.environ.get("MONGODB_URI", "").strip()
    if not uri and ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            found = re.match(r"\s*(?:export\s+)?MONGODB_URI\s*=\s*(.+?)\s*$", line)
            if found:
                uri = found.group(1).strip().strip("'\"")
    if not uri:
        raise RuntimeError("MONGODB_URI is not set. Put it in the environment or in ~/.stopline_env.")
    return uri


@lru_cache(maxsize=1)
def client() -> MongoClient:
    """One shared client per process. Majority writes, timezone-aware dates."""
    try:
        return MongoClient(
            _uri(),
            tlsCAFile=certifi.where(),
            appname="stopline",
            w="majority",
            retryWrites=True,
            tz_aware=True,
            serverSelectionTimeoutMS=15000,
        )
    except RuntimeError:
        raise
    except Exception as error:  # keep the connection string out of the message
        raise RuntimeError(f"could not create the database client ({type(error).__name__})") from None


def database(name: str = DB_NAME) -> Database:
    return client()[name]


def test_database(lane: str) -> Database:
    """A fresh database for one test session. Drop it with drop_test_database."""
    return client()[f"{TEST_PREFIX}{lane}_{secrets.token_hex(4)}"]


def drop_test_database(db: Database) -> None:
    if not db.name.startswith(TEST_PREFIX):
        raise ValueError(f"refusing to drop {db.name}: only test databases are dropped")
    db.client.drop_database(db.name)


def run_transaction(callback: Callable[[ClientSession], T]) -> tuple[T, int]:
    """Run callback in a transaction and return (its result, how many attempts it took).

    Snapshot reads, majority writes, primary reads. The driver retries the
    callback on a transient error such as a write conflict, so the callback
    must recompute everything it depends on each time it is called, and must
    not call a model or a target system.
    """
    attempts = 0

    def counted(session: ClientSession) -> T:
        nonlocal attempts
        attempts += 1
        return callback(session)

    with client().start_session() as session:
        result = session.with_transaction(
            counted,
            read_concern=ReadConcern("snapshot"),
            write_concern=WriteConcern("majority"),
            read_preference=ReadPreference.PRIMARY,
        )
    return result, attempts
