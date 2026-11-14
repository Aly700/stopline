from copy import deepcopy
from datetime import datetime, timezone

from stopline.proof.metrics import measure


def snapshot():
    return {
        'run': {'_id': 'r', 'mode': 'stopline', 'stop_at_local': 10.0},
        'effects': [
            {'_id': 'a', 'chain': ['root', 'repair', 'worker'], 'admitted_seq': 2,
             'state': 'applied', 'evidence': {'action_id': 'a', 'system': 'router', 'at_epoch': 11.0},
             'target': 'router/region-b', 'sent_at': datetime.fromtimestamp(9, timezone.utc)},
        ],
        'receipts': [{'_id': 'receipt', 'mandate_id': 'repair', 'stop_seq': 3,
                      'covered': ['a'], 'status': 'closed'}],
        'target_logs': [{'action_id': 'a', 'system': 'router', 'at_epoch': 11.0, 'event': 'applied'}],
        'sends': [{'action_id': 'a', 'at_epoch': 9.0}],
        'timings': [], 'audit': [], 'mandates': [], 'observations': [],
    }


def test_claim_time_does_not_count_as_transport_send():
    data = snapshot()
    data['sends'][0]['at_epoch'] = 10.5
    counts = measure(data)
    assert counts['started_after_stop_unsent'] == 1
    assert counts['sent_before_stop_landed_after'] == 0


def test_in_flight_is_reported_separately():
    counts = measure(snapshot())
    assert counts['started_after_stop_unsent'] == 0
    assert counts['sent_before_stop_landed_after'] == 1
    assert counts['admitted_after_stop'] == 0
    assert counts['missing_from_receipt'] == 0
    assert counts['closed_without_evidence'] == 0


def test_corrupt_records_are_detected_independently():
    data = snapshot()
    data['receipts'][0]['covered'] = []
    data['effects'].append(dict(data['effects'][0], _id='late', admitted_seq=4))
    data['target_logs'].append(deepcopy(data['target_logs'][0]))
    counts = measure(data)
    assert counts['admitted_after_stop'] == 1
    assert counts['missing_from_receipt'] == 1
    assert counts['duplicate_effects'] == 1
    assert counts['closed_without_evidence'] == 1


def test_terminal_state_without_matching_target_evidence_is_not_closed():
    data = snapshot()
    data['target_logs'] = []
    assert measure(data)['closed_without_evidence'] == 1
    data['effects'][0]['state'] = 'rejected'
    data['target_logs'] = [{'action_id': 'a', 'system': 'router', 'at_epoch': 11, 'event': 'applied'}]
    assert measure(data)['closed_without_evidence'] == 1


def test_cancelled_effect_must_never_have_been_sent_or_applied():
    data = snapshot()
    data['effects'][0].update(state='cancelled', evidence=None)
    assert measure(data)['closed_without_evidence'] == 1
    data['sends'] = []
    data['target_logs'] = []
    assert measure(data)['closed_without_evidence'] == 0


def test_baselines_do_not_claim_admissions_or_receipts():
    data = snapshot()
    data['run']['mode'] = 'flag'
    data['receipts'] = []
    counts = measure(data)
    for key in ('admitted_after_stop', 'missing_from_receipt', 'closed_without_evidence'):
        assert counts[key] is None
