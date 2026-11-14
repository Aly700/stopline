import pytest

from stopline import db as storage

from .records import insert_demo


@pytest.fixture
def db():
    database = storage.test_database("report")
    try:
        yield database
    finally:
        storage.drop_test_database(database)


@pytest.fixture
def demo(db):
    insert_demo(db)
    return db
