"""One bounded integration test; all writes are to a disposable test database."""
import time

from fastapi.testclient import TestClient

from stopline import db as stopline_db
from stopline.view import create_app
from .fakes import FakeDB, ScriptedStage, Target


def wait_until(predicate, seconds=8):
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        time.sleep(.05)
    assert predicate(), 'database change stream did not deliver within the bound'


def test_real_atlas_stream_and_stored_rebuild():
    db = stopline_db.test_database('view')
    fake = FakeDB()
    targets = {name: Target(name) for name in ('cluster', 'router')}
    stage = ScriptedStage(fake, targets)
    stage.start_run('stopline')
    stage.cut_and_kill()
    stage.press_stop()
    stage.resume_worker()
    try:
        for name, rows in fake.records.items():
            if rows:
                db[name].insert_many(rows)
        app = create_app(db, stage, targets)
        with TestClient(app) as client:
            wait_until(lambda: app.state.feed.connected)
            state = client.get('/api/state').json()
            assert state['receipt']['status'] == 'open'
            assert len(state['receipt']['rows']) == 4
            revision = app.state.feed.revision
            action = next(row for row in fake.records['effects'] if row['state'] == 'sent')
            db['effects'].update_one({'_id': action['_id']}, {'$set': {'state': 'applied', 'evidence': {
                'system': 'router', 'action_id': action['_id'], 'at_epoch': time.time(), 'applied': True}}})
            wait_until(lambda: app.state.feed.revision > revision)
            rebuilt = client.get('/api/state').json()
            assert not any(row['outcome'] == 'unknown' for row in rebuilt['receipt']['rows'])
            # View must not close a receipt itself, even when every row is terminal.
            assert rebuilt['receipt']['status'] == 'open'
        assert not app.state.feed.thread.is_alive()
        with TestClient(create_app(db, ScriptedStage(FakeDB(), targets), targets)) as restarted:
            assert restarted.get('/api/state').json() == rebuilt
    finally:
        stopline_db.drop_test_database(db)
