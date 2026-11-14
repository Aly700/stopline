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
    assert sorted(r['outcome'] for r in state['receipt']['rows']) == ['blocked', 'completed', 'refused', 'unknown']
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
    assert client.post('/api/key/Z').status_code == 404


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


def test_stopline_does_not_draw_an_unconfirmed_stop(rig):
    client, db, stage, app = rig
    stage.start_run('stopline')
    stage.run['stop_at'] = stage.run['created_at']
    state = client.get('/api/state').json()
    assert state['stop'] is None  # StopLine's boundary comes only from its stored receipt.


def test_refusal_keeps_the_original_policy_and_operator_chain(rig):
    client, db, stage, app = rig
    stage.start_run('stopline')
    stage.press_stop()
    stage.resume_worker()
    action_id = db.records['audit'][-1]['action_id']
    chain = client.get('/api/chain/' + action_id).json()
    assert chain['effect'] is None
    assert chain['policy']['_id'] == 1
    assert [row['holder'] for row in chain['permissions']] == ['person:operator', 'agent:repair', 'agent:worker-2']
    assert chain['audit'][-1]['detail']['revoked_by'] == 'person:operator'


def test_a_closed_stream_is_reopened(rig):
    client, db, stage, app = rig
    assert db.opened.wait(1)
    app.state.feed.stream.close()
    deadline = time.monotonic() + 2
    while db.watches < 2 and time.monotonic() < deadline:
        time.sleep(.02)
    assert db.watches >= 2


def test_errors_do_not_leak_connection_details(rig):
    client, db, stage, app = rig
    def failed():
        raise RuntimeError('private-connection-details')
    stage.press_stop = failed
    result = client.post('/api/key/S')
    assert result.status_code == 503
    assert 'private-connection' not in result.text


def test_missing_covered_record_is_unknown_and_not_invented(rig):
    client, db, stage, app = rig
    stage.start_run('stopline')
    stage.press_stop()
    db.records['receipts'][-1]['covered'].append('missing-record')
    row = client.get('/api/state').json()['receipt']['rows'][-1]
    assert row['action_id'] == 'missing-record'
    assert row['outcome'] == 'unknown'
    assert row['evidence'] is None


def test_chain_database_failure_is_pending_and_sanitized(rig, monkeypatch):
    client, db, stage, app = rig
    def failed(*args):
        raise RuntimeError('private connection details')
    monkeypatch.setattr('stopline.view.read_chain', failed)
    response = client.get('/api/chain/an-action')
    assert response.status_code == 503
    assert response.json()['pending'] is True
    assert 'private connection details' not in response.text


def test_no_numbers_are_invented_without_a_provider():
    from stopline.view import create_app
    db = FakeDB()
    with TestClient(create_app(db, ScriptedStage(db, {}), {})) as client:
        response = client.post('/api/key/M').json()
    assert response['lines'] == []
    assert response['message'] == 'Numbers have not been measured.'


def test_sse_carries_the_confirmed_rows(rig):
    import asyncio
    import json
    client, db, stage, app = rig
    stage.start_run('stopline')
    endpoint = next(route.endpoint for route in app.routes if route.path == '/api/events')
    class ConnectedRequest:
        async def is_disconnected(self):
            return False
    async def receive():
        response = await endpoint(ConnectedRequest())
        try:
            assert response.media_type == 'text/event-stream'
            await anext(response.body_iterator)  # reconnect instruction
            event = await anext(response.body_iterator)
            return json.loads(next(line[6:] for line in event.splitlines() if line.startswith('data: ')))
        finally:
            await response.body_iterator.aclose()
    payload = asyncio.run(receive())
    assert len(payload['state']['rows']) == 3
    assert payload['state']['run']['mode'] == 'stopline'


def test_recent_runs_and_historical_state_are_read_only(rig):
    client, db, stage, app = rig
    client.post('/api/key/1')
    client.post('/api/key/S')
    earlier = client.get('/api/state').json()
    client.post('/api/key/2')
    calls = list(stage.calls)
    runs = client.get('/api/runs').json()
    assert [run['id'] for run in runs] == ['run-2', 'run-1']
    assert runs[1]['counts'] == {'completed': 2, 'RAN AFTER STOP': 1}
    assert runs[1]['stop_at'] == earlier['stop']['at']
    assert runs[1]['receipt_status'] is None
    saved = client.get('/api/state', params={'run_id': 'run-1'}).json()
    assert saved['rows'] == earlier['rows']
    assert saved['run'] == earlier['run']
    assert saved['ready_key'] is None and saved['last_key'] is None
    assert saved['read_only'] is True
    assert stage.calls == calls
    assert client.get('/api/state').json()['run']['_id'] == 'run-2'
    assert client.get('/api/state?run_id=missing').status_code == 404


def test_run_counts_include_rows_omitted_from_timeline(rig):
    client, db, stage, app = rig
    stage.start_run('flag')
    for step in range(15):
        action = stage.effect(1, 0, 'applied', 10 + step)
        action['_id'] = f'extra-{step}'
    runs = client.get('/api/runs').json()
    assert runs[0]['counts'] == {'completed': 17}
    assert len(client.get('/api/state').json()['rows']) == 8


def test_runs_failure_is_pending_and_sanitized(rig, monkeypatch):
    client, db, stage, app = rig
    def failed(*args):
        raise RuntimeError('private connection details')
    monkeypatch.setattr('stopline.view.read_runs', failed)
    response = client.get('/api/runs')
    assert response.status_code == 503
    assert response.json()['pending'] is True
    assert 'private connection details' not in response.text


def test_saved_running_run_has_no_command_hint(rig):
    client, db, stage, app = rig
    stage.start_run('flag')
    assert client.get('/api/state').json()['ready_key'] == 'S'
    stage.start_run('stopline')
    saved = client.get('/api/state?run_id=run-1').json()
    assert saved['run']['status'] == 'running'
    assert saved['ready_key'] is None
    assert saved['read_only'] is True


def test_recent_runs_are_bounded_and_read_in_one_round_trip(rig):
    client, db, stage, app = rig
    for _ in range(24):
        stage.start_run('flag')
    before = db.reads
    response = client.get('/api/runs').json()
    assert len(response) == 20
    assert response[0]['id'] == 'run-24'
    assert response[-1]['id'] == 'run-5'
    assert db.reads == before + 1
