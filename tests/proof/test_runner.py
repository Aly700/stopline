from stopline.proof import summary
from stopline.proof.metrics import COUNT_KEYS, INVARIANTS
from stopline.proof.runner import run_suite
from stopline.proof.store import MemoryDatabase


def test_two_complete_runs_reproduce_all_safety_counts(tmp_path):
    first = run_suite(work_dir=tmp_path / 'first')
    second = run_suite(work_dir=tmp_path / 'second')
    assert first['schedule_count'] == 43
    assert len(first['rows']) == 129
    assert not first['failed']
    assert len(first['skipped']) == 24
    assert first['sources']['gate'] == 'protocol fake; no MongoDB transactions'
    assert all(first['totals']['stopline'][key] == 0 for key in INVARIANTS)
    assert first['flag_counterexamples']
    for a, b in zip(first['rows'], second['rows'], strict=True):
        assert (a['name'], a['mode'], a['status'], a.get('counts')) == (b['name'], b['mode'], b['status'], b.get('counts'))
    assert first['elapsed_seconds'] < 120
    assert first['totals']['stopline']['sent_before_stop_landed_after'] > 0
    assert first['totals']['stopline']['started_after_stop_unsent'] > 0


def test_fault_checks_have_recorded_evidence(tmp_path):
    result = run_suite(work_dir=tmp_path)
    rows = {r['name']: r for r in result['rows'] if r['mode'] == 'stopline'}
    retry = rows['retry']['evidence']
    assert any(a.get('detail', {}).get('attempts') == 2 and a['kind'] == 'deny' for a in retry['audit'])
    lost = rows['lost_reply']['evidence']
    assert any(o['event'] == 'while_unreachable' and o['receipts'][0]['status'] == 'open'
               and o['effects'][0]['state'] == 'sent' for o in lost['observations'])
    assert rows['late_duplicate']['counts']['duplicate_effects'] == 0
    assert rows['router_restart']['checks']['durable_after_restart']
    assert rows['receipt_closure']['checks']['closed_after_reconcile']


def test_summary_reads_latest_result_and_always_returns_three_lines(tmp_path):
    db = MemoryDatabase()
    assert len(summary(db)) == 3
    result = run_suite(work_dir=tmp_path)
    db['proof_results'].insert_one(result)
    lines = summary(db)
    assert len(lines) == 3
    assert 'FAKE' in ' '.join(lines)
    assert 'in-flight' in ' '.join(lines)
    assert 'flag' in ' '.join(lines)


def test_missing_required_schedule_is_incomplete(tmp_path):
    from stopline.proof.runner import finalize
    result = run_suite(work_dir=tmp_path)
    result['rows'] = [r for r in result['rows'] if not (r['mode'] == 'stopline' and r['name'] == 'retry')]
    assert not finalize(result)['complete']


def test_skipped_required_schedule_does_not_count_as_passed_measurement(tmp_path):
    from stopline.proof.runner import finalize
    result = run_suite(work_dir=tmp_path)
    row = next(r for r in result['rows'] if r['mode'] == 'stopline' and r['name'] == 'paused_send')
    row.update(status='skipped', reason='fault hook unavailable')
    finalize(result)
    assert not result['complete']
    assert result['totals']['stopline']['sent_before_stop_landed_after'] == 0
    db = MemoryDatabase()
    db['proof_results'].insert_one(result)
    assert 'incomplete' in ' '.join(summary(db)).lower()
