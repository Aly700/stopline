from copy import deepcopy

import pytest

from stopline.proof.runner import finalize, run_suite


@pytest.fixture(scope='module')
def result(tmp_path_factory):
    return run_suite(work_dir=tmp_path_factory.mktemp('evidence-contract'))


def changed_row(result, name):
    result = deepcopy(result)
    row = next(r for r in result['rows'] if r['mode'] == 'stopline' and r['name'] == name)
    return result, row


def test_hold_is_not_a_pass_unless_the_designated_hold_was_reached(result):
    changed, row = changed_row(result, 'hold/12/worker-1/0/after_send')
    row['evidence']['observations'] = [o for o in row['evidence']['observations'] if o['event'] != 'hold_reached']
    finalize(changed)
    assert row['status'] == 'failed'


def test_late_duplicate_is_not_a_pass_without_the_replayed_dispatch(result):
    changed, row = changed_row(result, 'late_duplicate')
    row['evidence']['sends'] = row['evidence']['sends'][:1]
    finalize(changed)
    assert row['status'] == 'failed'


def test_restart_requires_an_observation_after_reopening(result):
    changed, row = changed_row(result, 'router_restart')
    row['evidence']['observations'] = [o for o in row['evidence']['observations'] if o['event'] != 'after_restart']
    finalize(changed)
    assert row['status'] == 'failed'


def test_missing_timestamp_is_an_incomplete_measurement(result):
    changed, row = changed_row(result, 'paused_send')
    del row['evidence']['target_logs'][0]['at_epoch']
    finalize(changed)
    assert row['status'] == 'failed'
    assert not changed['complete']


def test_baseline_cannot_pass_with_a_missing_target_timestamp(result):
    changed = deepcopy(result)
    row = next(r for r in changed['rows'] if r['mode'] == 'flag' and r['name'] == 'paused_send')
    del row['evidence']['target_logs'][0]['at_epoch']
    finalize(changed)
    assert row['status'] == 'failed'


def test_required_row_without_evidence_is_a_failure(result):
    changed, row = changed_row(result, 'receipt_closure')
    del row['evidence']
    finalize(changed)
    assert row['status'] == 'failed'
    assert not changed['complete']


def test_failed_fixed_control_does_not_abort_the_report(result):
    changed = deepcopy(result)
    row = next(r for r in changed['rows'] if r['mode'] == 'fixed' and r['name'] == 'paused_send')
    del row['evidence']
    row.update(status='failed', error='TargetUnreachable')
    finalize(changed)
    assert 'fixed:paused_send' in changed['failed']
    assert not changed['complete']
