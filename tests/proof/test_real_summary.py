from datetime import datetime, timedelta, timezone

from stopline.proof import summary
from stopline.proof.store import MemoryDatabase


def evidence(mode, late=False, inflight=False):
    return {'run': {'mode': mode, 'stop_at_local': 10}, 'effects': [], 'receipts': [],
            'observations': [], 'target_logs': ([{'action_id': 'a', 'system': 'router',
                'event': 'applied', 'at_epoch': 11}] if late or inflight else []),
            'sends': [{'action_id': 'a', 'at_epoch': 9}] if inflight else [],
            'timings': [], 'audit': [], 'mandates': []}


def real_result():
    stopline = evidence('stopline', inflight=True)
    stopline['receipts'] = [{'_id': 'r', 'mandate_id': 'repair', 'stop_seq': 3,
                            'covered': ['a'], 'status': 'open'}]
    return {'_id': 'real', 'producer': 'real', 'complete': True, 'created_at': datetime.now(timezone.utc),
            'rows': [{'name': 'one', 'mode': 'flag', 'status': 'passed', 'evidence': evidence('flag', late=True)},
                     {'name': 'one', 'mode': 'stopline', 'status': 'passed', 'evidence': stopline}]}


def test_no_real_result_means_no_summary_even_with_old_fake_numbers():
    db = MemoryDatabase()
    assert summary(db) == []
    db['proof_results'].insert_one({'_id': 'fake', 'producer': 'fake', 'created_at': datetime.now(timezone.utc)})
    assert summary(db) == []


def test_summary_uses_real_evidence_and_ignores_newer_fake_result():
    db = MemoryDatabase()
    real = real_result()
    db['proof_results'].insert_one(real)
    db['proof_results'].insert_one({'_id': 'fake', 'producer': 'fake', 'created_at': real['created_at'] + timedelta(days=1)})
    lines = summary(db)
    assert lines == [
        '1 stop schedules, each run with a stop flag and with StopLine.',
        'Stop flag: an action started after the stop in 1 of 1.',
        'StopLine: 0 of 1. Sent before the stop and landed after: 1, all on the receipt.',
    ]
    assert all(len(line) < 90 for line in lines)


def test_summary_does_not_hide_stopline_late_starts_or_uncovered_inflight():
    db = MemoryDatabase()
    real = real_result()
    real['rows'][1]['evidence']['sends'] = []
    db['proof_results'].insert_one(real)
    assert summary(db)[2].startswith('StopLine: 1 of 1.')
    real['rows'][1]['evidence']['sends'] = [{'action_id': 'a', 'at_epoch': 9}]
    real['rows'][1]['evidence']['receipts'][0]['covered'] = []
    db['proof_results'].replace_one({'_id': 'real'}, real)
    assert 'NOT all on the receipt' in summary(db)[2]


def test_incomplete_real_measurement_is_explicit_and_keeps_unpaired_counterexamples():
    from stopline.proof import summary_numbers
    db = MemoryDatabase()
    result = real_result()
    result['complete'] = False
    result['rows'].append({'name': 'unpaired', 'mode': 'stopline', 'status': 'failed',
                           'evidence': evidence('stopline', late=True)})
    db['proof_results'].insert_one(result)
    assert summary(db)[0].startswith('Incomplete: ')
    assert 'unpaired' in summary_numbers(result)['counterexamples']
