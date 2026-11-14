import time

import pytest
from fastapi.testclient import TestClient

from .fakes import FakeDB, ScriptedStage, Target


@pytest.fixture
def rig():
    from stopline.view import create_app
    db = FakeDB()
    targets = {name: Target(name) for name in ('cluster', 'router')}
    stage = ScriptedStage(db, targets)
    app = create_app(db, stage, targets, numbers=lambda: ['40 schedules measured', 'Stop flag: 12 actions after stop', 'StopLine: 0 admitted after stop'])
    with TestClient(app) as client:
        yield client, db, stage, app


def test_page_assets_and_empty_state(rig):
    client, db, stage, app = rig
    assert client.get('/').status_code == 200
    assert client.get('/app.js').status_code == 200
    assert client.get('/style.css').status_code == 200
    state = client.get('/api/state').json()
    assert state['run'] is None and state['rows'] == []
    assert db.opened.wait(1)
    assert db.watches == 1


def test_demo_sequence_and_receipt(rig):
    client, db, stage, app = rig
    assert client.post('/api/key/1').json()['ok']
    state = client.get('/api/state').json()
    assert state['run']['mode'] == 'flag'
    assert [r['outcome'] for r in state['rows']] == ['completed', 'completed']
    client.post('/api/key/S')
    state = client.get('/api/state').json()
    assert state['stop']['at']
    restart = state['rows'][-1]
    assert restart['tool'] == 'restart' and restart['after_stop']
    assert restart['held']
    client.post('/api/key/2')
    state = client.get('/api/state').json()
    assert state['run']['mode'] == 'stopline'
    assert len(state['permissions']) == 4
    assert len(state['rows']) == 3
    client.post('/api/key/K')
    assert client.get('/api/targets').json()['router']['reachable'] is False
    assert client.get('/api/state').json()['workers'][-1]['status'] == 'killed'
    client.post('/api/key/S')
    client.post('/api/key/W')
    state = client.get('/api/state').json()
    assert state['stop']['seq'] == 7
    assert len(state['receipt']['rows']) == 4
    assert sorted(r['outcome'] for r in state['receipt']['rows']) == ['blocked', 'blocked', 'completed', 'unknown']
    assert state['receipt']['status'] == 'open'
    assert all(r['chain'] for r in state['receipt']['rows'])
    before = state['receipt']
    assert client.get('/api/state').json()['receipt'] == before  # waiting never closes it
    client.post('/api/key/R')
    state = client.get('/api/state').json()
    assert state['receipt']['status'] == 'closed'
    assert not any(r['outcome'] == 'unknown' for r in state['receipt']['rows'])
    client.post('/api/key/U')
    assert client.get('/api/targets').json()['router']['weights']['region-b'] == 0
    assert any(r['undo'] == 'no_undo' for r in client.get('/api/state').json()['receipt']['rows'])
    assert len(client.post('/api/key/M').json()['lines']) == 3
    assert client.post('/api/key/V').json()['message'] == 'Database refused the malformed record.'
    assert client.post('/api/key/X').status_code == 404


def test_rebuild_uses_stored_run_and_limits_trace_to_eight(rig):
    client, db, stage, app = rig
    stage.start_run('flag')
    for step in range(15):
        item = stage.effect(1, 0, 'applied', 10 + step)
        item['_id'] = f'extra-{step}'
    first = client.get('/api/state').json()
    assert len(first['rows']) == 8
    assert first['omitted_rows'] == 9
    from stopline.view import create_app
    replacement = ScriptedStage(db, stage.targets)  # has no in-memory run
    with TestClient(create_app(db, replacement, stage.targets)) as restarted:
        assert restarted.get('/api/state').json() == first


def test_chain_and_untrusted_text_stay_data(rig):
    client, db, stage, app = rig
    stage.start_run('stopline')
    action = db.records['effects'][0]
    action['evidence']['message'] = '<img src=x onerror=alert(1)>'
    chain = client.get('/api/chain/' + action['_id']).json()
    assert [row['holder'] for row in chain['permissions']] == ['person:operator', 'agent:repair', 'agent:worker-1']
    assert chain['policy']['_id'] == 1
    assert chain['effect']['evidence']['message'] == '<img src=x onerror=alert(1)>'
    assert client.get('/api/chain/missing').status_code == 404


def test_stream_reopens_after_failure_and_closes(rig):
    client, db, stage, app = rig
    assert db.opened.wait(1)
    revision = app.state.feed.revision
    db.changes.put(RuntimeError('stream interrupted'))
    deadline = time.monotonic() + 3
    while db.watches < 2 and time.monotonic() < deadline:
        time.sleep(.02)
    assert db.watches >= 2
    assert app.state.feed.revision > revision


def test_one_database_round_trip_for_state(rig):
    client, db, stage, app = rig
    stage.start_run('stopline')
    before = db.reads
    client.get('/api/state')
    assert db.reads == before + 1
