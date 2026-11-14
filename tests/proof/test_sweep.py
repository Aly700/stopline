from types import SimpleNamespace

from stopline.contract import HoldPoint, WORKER_ONE, WORKER_TWO
from stopline.proof.backend import ObservedTarget
from stopline.proof.fakes import FakeGate
from stopline.proof.runner import Driver
from stopline.proof.schedules import Schedule
from stopline.proof.store import MemoryDatabase
from stopline.targets import build_sender
from stopline.targets.mock import MockCluster


def driver_for(point):
    db = MemoryDatabase()
    gate = FakeGate(db)
    target = ObservedTarget(MockCluster(), db)
    targets = {'cluster': target}
    sender = build_sender(gate, targets)
    runtime = SimpleNamespace(db=db, gate=gate, targets=targets, sender=sender, backend='real',
                              logs=lambda observed: [dict(row, event='applied' if row['applied'] else 'fenced')
                                                     for row in observed.state()['log']])
    case = Schedule('hold/12/worker-1/0/' + point, 'hold', (WORKER_ONE, WORKER_TWO), WORKER_ONE, 0, point)
    return Driver(runtime, case, 'stopline', 'sweep-unit')


def test_real_sender_sweeps_before_hold_release_and_worker_stops():
    driver = driver_for(HoldPoint.AFTER_CLAIM)
    sweep = driver.sender.sweep
    calls = []

    def checked_sweep(covered):
        assert not driver.db['runs'].find_one({'_id': driver.run_id})['holds'][0]['released']
        assert driver.db['receipts'].find()
        calls.append(covered)
        return sweep(covered)

    driver.sender.sweep = checked_sweep
    data = driver.execute()
    assert len(calls) == 1
    assert data['effects'][0]['state'] == 'rejected'
    assert not data['sends']
    assert data['target_logs'][0]['event'] == 'fenced'
    assert any(o['event'] == 'sender_refused_after_sweep' for o in data['observations'])


def test_sweep_counts_only_target_queries_with_matching_target_evidence():
    from stopline.proof.metrics import sweep_counts
    claimed = driver_for(HoldPoint.AFTER_CLAIM).execute()
    delivered = driver_for(HoldPoint.AFTER_SEND).execute()
    result = {'rows': [{'mode': 'stopline', 'evidence': claimed}, {'mode': 'stopline', 'evidence': delivered}]}
    assert sweep_counts(result) == {'fenced': 1, 'already_applied': 1}
    claimed['target_logs'] = []
    assert sweep_counts(result)['fenced'] == 0


def test_application_during_sweep_remains_after_stop():
    from stopline.proof.metrics import measure, sweep_counts
    driver = driver_for(HoldPoint.AFTER_CLAIM)
    sweep = driver.sender.sweep

    def arrival_during_sweep(covered):
        driver.targets['cluster'].send(driver.action(WORKER_ONE, 0))
        return sweep(covered)

    driver.sender.sweep = arrival_during_sweep
    data = driver.execute()
    applied_at = data['target_logs'][0]['at_epoch']
    assert data['run']['stop_at_local'] < applied_at < data['run']['sweep_completed_at_local']
    assert measure(data)['started_after_stop_unsent'] == 1
    assert sweep_counts({'rows': [{'mode': 'stopline', 'evidence': data}]})['already_applied'] == 1


def test_sweep_does_not_count_terminal_actions_it_never_asked_about():
    from stopline.proof.metrics import sweep_counts
    driver = driver_for(HoldPoint.AFTER_CLAIM)
    driver.step(WORKER_ONE, 0)
    driver.stop()
    assert sweep_counts({'rows': [{'mode': 'stopline', 'evidence': driver.capture()}]}) == {
        'fenced': 0, 'already_applied': 0,
    }
