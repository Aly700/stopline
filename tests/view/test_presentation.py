"""Presentation decisions must come from records, including after a rebuild."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from stopline.contract import StageResult
from stopline.view import create_app
from stopline.view.records import read_state
from .fakes import FakeDB, NOW, ScriptedStage, Target


@pytest.mark.parametrize('mode', ['flag', 'fixed'])
@pytest.mark.parametrize('difference, expected', [(-1, False), (0, False), (1, True)])
def test_baseline_after_stop_uses_the_target_clock(mode, difference, expected):
    db = FakeDB()
    stage = ScriptedStage(db, {})
    stage.targets = {name: Target(name) for name in ('cluster', 'router')}
    stage.start_run(mode)
    stage.press_stop()
    action = db.records['effects'][-1]
    action['evidence']['at_epoch'] = stage.run['stop_at_local'] + difference
    # A later write/finish time alone must not turn an earlier target action red.
    action['admitted_at'] = NOW + timedelta(seconds=60)
    action['finished_at'] = NOW + timedelta(seconds=65)
    row = next(row for row in read_state(db)['rows'] if row['action_id'] == action['_id'])
    assert row['ran_after_stop'] is expected
    assert row['after_stop'] is expected
    assert row['outcome'] == ('RAN AFTER STOP' if expected else 'completed')
    assert row['target_at_epoch'] == action['evidence']['at_epoch']


@pytest.mark.parametrize('difference, expected', [(-1, False), (0, False), (1, True)])
def test_baseline_without_evidence_uses_creation_time(difference, expected):
    db = FakeDB()
    stage = ScriptedStage(db, {name: Target(name) for name in ('cluster', 'router')})
    stage.start_run('flag')
    stage.press_stop()
    action = db.records['effects'][-1]
    action.update(state='sent', evidence=None, admitted_at=stage.run['stop_at'] + timedelta(seconds=difference))
    row = next(row for row in read_state(db)['rows'] if row['action_id'] == action['_id'])
    assert row['ran_after_stop'] is expected
    assert row['target_at_epoch'] is None  # Do not present the database clock as target evidence.


def test_reconciliation_annotation_rebuilds_from_saved_note_and_evidence():
    db = FakeDB()
    stage = ScriptedStage(db, {name: Target(name) for name in ('cluster', 'router')})
    stage.start_run('stopline')
    stage.press_stop()
    stage.restore_and_reconcile()
    action = next(row for row in db.records['effects'] if row['target'].startswith('router/'))
    action['finished_at'] = NOW + timedelta(seconds=20)
    db.records['tasks'][-1]['note'] = 'Restored the router and checked the outstanding sends.'
    state = read_state(db)
    assert next(row for row in state['rows'] if row['action_id'] == action['_id'])['reconciled']
    assert next(row for row in state['receipt']['rows'] if row['action_id'] == action['_id'])['reconciled']
    action['evidence'] = None
    state = read_state(db)
    assert not next(row for row in state['rows'] if row['action_id'] == action['_id'])['reconciled']


def test_pending_result_can_be_read_without_repeating_the_command():
    db = FakeDB()
    stage = ScriptedStage(db, {})
    stage.status = lambda: {'operations': {'cut_and_kill': {
        'ok': True, 'pending': False, 'message': 'Cut the router and killed worker two after its send.'}}}
    with TestClient(create_app(db, stage, {})) as client:
        response = client.get('/api/operation/K')
        assert response.status_code == 200
        assert response.json()['message'] == 'Cut the router and killed worker two after its send.'
        assert stage.calls == []
        assert client.get('/api/operation/X').status_code == 404


def test_validation_result_preserves_database_refusal_text():
    db = FakeDB()
    stage = ScriptedStage(db, {})
    stage.validation_rejection = lambda: StageResult(True, 'Database refused the malformed record.', detail={'error': 'Document failed validation (code 121).'})
    with TestClient(create_app(db, stage, {})) as client:
        response = client.post('/api/key/V').json()
    assert response['detail']['error'] == 'Document failed validation (code 121).'
