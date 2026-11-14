"""One isolated database per invocation, always dropped after the suite."""

import pytest

from stopline import db as storage
from stopline.gate import build_gate


@pytest.fixture(scope="session")
def concurrency_gate():
    database = storage.test_database("conc")
    try:
        gate = build_gate(database)
        gate.setup()
        yield gate
    finally:
        storage.drop_test_database(database)
