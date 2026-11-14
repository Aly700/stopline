import pytest

from stopline.view.records import read_state
from .fakes import FakeDB, ScriptedStage, Target


def rig(mode='stopline'):
    db = FakeDB()
    stage = ScriptedStage(db, {name: Target(name) for name in ('cluster', 'router')})
    stage.start_run(mode)
    return db, stage


@pytest.mark.parametrize('state,fenced,expected', [
    ('rejected', True, 'STOPPED IN TIME'), ('rejected', False, 'no effect'),
    ('rejected', None, 'no effect'), ('applied', True, 'completed'),
])
def test_fence_outcome_in_actions_and_receipt(state, fenced, expected):
    db, stage = rig()
    stage.press_stop()
    action = db.records['effects'][1]
    action.update(state=state, evidence={'action_id': action['_id'], 'system': 'router', 'at_epoch': 1})
    if fenced is not None:
        action['evidence']['fenced'] = fenced
    view = read_state(db)
    for rows in (view['rows'], view['receipt']['rows']):
        assert next(row for row in rows if row['action_id'] == action['_id'])['outcome'] == expected


def test_readiness_follows_saved_demo_progress():
    db, stage = rig()
    assert read_state(db)['ready_key'] == 'K'
    stage.cut_and_kill()
    assert read_state(db)['ready_key'] == 'S'
    stage.press_stop()
    assert read_state(db)['ready_key'] == 'W'
    stage.resume_worker()
    assert read_state(db)['ready_key'] == 'R'
    stage.restore_and_reconcile()
    assert read_state(db)['ready_key'] == 'U'
    stage.undo()
    assert read_state(db)['ready_key'] is None


@pytest.mark.parametrize('mode', ['flag', 'fixed'])
def test_baseline_readiness_requires_the_worker_to_be_held(mode):
    db, stage = rig(mode)
    worker = db.records['tasks'][0]
    worker['status'] = 'running'
    assert read_state(db)['ready_key'] is None
    worker['status'] = 'held'
    assert read_state(db)['ready_key'] == 'S'
    stage.press_stop()
    assert read_state(db)['ready_key'] is None


def test_released_or_mismatched_hold_does_not_claim_ready_for_k():
    db, stage = rig()
    stage.run['holds'][1]['released'] = True
    assert read_state(db)['ready_key'] is None
    stage.run['holds'][1]['released'] = False
    db.records['tasks'][1]['held_at']['point'] = 'before_check'
    assert read_state(db)['ready_key'] is None


def test_leads_folded_refusal_and_receipt_word_are_preserved():
    db, stage = rig()
    stage.cut_and_kill()
    stage.press_stop()
    stage.resume_worker()
    view = read_state(db)
    restart = [row for row in view['rows'] if row['tool'] == 'restart']
    assert len(restart) == 1
    assert restart[0]['refusal']['reason'] == 'revoked'
    assert view['receipt']['rows'][-1]['outcome'] == 'refused'
