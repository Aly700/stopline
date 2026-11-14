from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import multiprocessing
import sqlite3
import subprocess
import time

import pytest

from stopline.contract import Action, NotClaimed, Reason, TargetTimeout, TargetUnreachable
from .conftest import FakeGate, shift


def test_factories_are_available_without_the_cluster_module():
    from stopline.targets import build_targets, build_sender, reset_all
    targets = build_targets('mock')
    assert set(targets) == {'router', 'cluster'}
    assert callable(build_sender) and callable(reset_all)


def test_duplicate_requests_apply_once_and_changed_arguments_are_refused(router):
    action = shift()
    first = router.send(action)
    duplicate = router.send(action)
    conflict = router.send(shift(percent=50))
    assert first.applied and duplicate == first
    assert not conflict.applied and conflict.reason == Reason.SAME_ID_DIFFERENT_ARGS
    assert router.state()['weights'] == {'region-a': 75, 'region-b': 25}
    assert router.state()['log'] == [first.evidence]
    assert router.lookup(action.action_id).evidence == first.evidence


def test_concurrent_duplicates_apply_once(router):
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: router.send(shift()), range(8)))
    assert all(result == results[0] for result in results)
    assert len(router.state()['log']) == 1


def test_lost_answer_stays_sent_then_reconciles_from_the_router_log(gate, router):
    from stopline.targets import build_sender
    gate.admit(shift())
    sender = build_sender(gate, {'router': router})
    assert sender.claim('shift-1').claimed
    router.cut()
    assert sender.send('shift-1')['state'] == 'sent'
    assert not router.state()['reachable']
    assert sender.reconcile('shift-1')['state'] == 'sent'
    router.restore()
    # A fresh sender recovers without having the old process's claim token.
    recovered = build_sender(gate, {'router': router}).reconcile('shift-1')
    assert recovered['state'] == 'applied'
    assert recovered['evidence'] == router.state()['log'][0]


def test_never_delivered_action_is_fenced_before_recording_rejected(gate, router):
    from stopline.targets import build_sender
    gate.admit(shift())
    sender = build_sender(gate, {'router': router})
    sender.claim('shift-1')
    result = sender.reconcile('shift-1')
    assert result['state'] == 'rejected'
    assert result['evidence']['fenced']
    late = router.send(shift())
    assert not late.applied and late.reason == Reason.FENCED
    assert router.state()['weights']['region-b'] == 0


def test_sender_requires_its_own_successful_claim(gate, router):
    from stopline.targets import build_sender
    gate.admit(shift())
    first = build_sender(gate, {'router': router})
    other = build_sender(gate, {'router': router})
    with pytest.raises(NotClaimed):
        first.send('shift-1')
    assert first.claim('shift-1').claimed
    assert not other.claim('shift-1').claimed
    with pytest.raises(NotClaimed):
        other.send('shift-1')
    assert not router.lookup('shift-1').found
    assert first.send('shift-1')['state'] == 'applied'


def test_fence_reports_an_application_that_won_the_race(gate, router):
    from stopline.targets import build_sender
    gate.admit(shift())
    sender = build_sender(gate, {'router': router})
    sender.claim('shift-1')
    original_lookup = router.lookup

    def racing_lookup(action_id):
        missing = original_lookup(action_id)
        router.send(shift())
        return missing

    router.lookup = racing_lookup
    result = sender.reconcile('shift-1')
    assert result['state'] == 'applied'
    assert result['evidence'] == router.state()['log'][0]


def test_inverse_restores_previous_weight_and_refuses_a_moved_state(router):
    from stopline.contract import Action
    router.send(shift('earlier', 10))
    router.send(shift('forward', 25))
    inverse = router.inverse_of(shift('forward', 25))
    assert inverse.args['percent'] == 10
    assert inverse.expect == {'weights': {'region-a': 75, 'region-b': 25}, 'last_action_id': 'forward'}
    undo = Action('undo', 'run', 'root', 'person:operator', inverse.tool, inverse.target,
                  inverse.args, reverses='forward')
    assert router.send(undo).applied
    assert router.state()['weights']['region-b'] == 10
    router.send(shift('later', 50))
    refused = router.send(replace(undo, action_id='stale-undo'))
    assert not refused.applied and refused.reason == Reason.STATE_CHANGED
    assert router.lookup('stale-undo').evidence['fenced']


def test_mock_cluster_rolls_back_restarts_deduplicates_and_has_no_inverse():
    from stopline.targets.mock import MockCluster
    cluster = MockCluster()
    rollback = Action('rollback', 'run', 'worker', 'agent:worker-1', 'rollback',
                      'cluster/checkout-api', {'to_version': 'v1'})
    restart = replace(rollback, action_id='restart', tool='restart', target='cluster/checkout-worker', args={})
    assert cluster.state()['deployments']['checkout-api']['version'] == 'v2'
    for action in (rollback, restart):
        result = cluster.send(action)
        assert result.applied
        assert cluster.send(action) == result
        assert cluster.lookup(action.action_id).evidence == result.evidence
        assert cluster.inverse_of(action) is None
    deployments = cluster.state()['deployments']
    assert deployments['checkout-api']['version'] == 'v1'
    assert deployments['checkout-worker']['action_id'] == 'restart'
    assert deployments['checkout-worker']['restarted_at']
    assert len(cluster.state()['log']) == 2


def test_reset_returns_both_systems_to_the_starting_state_twice(router):
    from stopline.targets import build_targets, reset_all
    targets = build_targets('mock')
    targets['router'] = router
    for _ in range(2):
        router.send(shift())
        router.fence('missing')
        router.cut()
        action = Action('rollback', 'run', 'worker', 'agent:worker-1', 'rollback',
                        'cluster/checkout-api', {'to_version': 'v1'})
        targets['cluster'].send(action)
        started = time.monotonic()
        reset_all(targets)
        assert time.monotonic() - started < 10
        assert router.state()['weights'] == {'region-a': 100, 'region-b': 0}
        assert router.state()['log'] == []
        assert not router.lookup('missing').found
        assert targets['cluster'].state()['deployments']['checkout-api']['version'] == 'v2'
        assert targets['cluster'].state()['log'] == []


def _send_in_child(effects, router_url):
    from stopline.targets import build_sender
    from stopline.targets.router import RouterAdapter

    sender = build_sender(FakeGate(effects), {'router': RouterAdapter(router_url, timeout=30)})
    assert sender.claim('shift-1').claimed
    sender.send('shift-1')


def test_killing_the_sender_after_commit_leaves_sent_and_recovers(router_process):
    from stopline.targets import build_sender

    router, store, _ = router_process
    context = multiprocessing.get_context('spawn')
    with context.Manager() as manager:
        effects = manager.dict()
        gate = FakeGate(effects)
        gate.admit(shift())
        router.cut()
        process = context.Process(target=_send_in_child, args=(effects, router.base_url))
        process.start()
        try:
            deadline = time.monotonic() + 4
            while True:
                with sqlite3.connect(store, timeout=0.2) as db:
                    committed = db.execute('SELECT evidence FROM actions WHERE action_id=?', ('shift-1',)).fetchone()
                if committed:
                    break
                assert process.is_alive(), 'sender exited before delivery'
                assert time.monotonic() < deadline, 'router did not commit within four seconds'
                time.sleep(0.01)
            assert effects['shift-1']['state'] == 'sent'
            assert process.is_alive(), 'sender must still be waiting for the lost answer'
            process.kill()
            process.join(timeout=2)
            assert process.exitcode is not None and process.exitcode != 0
            assert effects['shift-1']['state'] == 'sent'
            router.restore()
            recovered = build_sender(gate, {'router': router}).reconcile('shift-1')
            assert recovered['state'] == 'applied'
            assert recovered['evidence'] == router.state()['log'][0]
        finally:
            if process.is_alive():
                process.kill()
                process.join(timeout=2)


def test_router_log_and_fences_survive_process_restart(router_process):
    router, _, process = router_process
    applied = router.send(shift())
    fenced = router.fence('late')
    process.terminate()
    process.wait(timeout=2)
    restarted = subprocess.Popen(process.args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 4
        while not router.state()['reachable']:
            assert restarted.poll() is None, restarted.stderr.read()
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert router.lookup('shift-1').evidence == applied.evidence
        assert router.lookup('late').evidence == fenced
        assert not router.send(shift('late')).applied
        assert router.send(shift()) == applied
        assert router.state()['weights']['region-b'] == 25
    finally:
        restarted.terminate()
        restarted.wait(timeout=2)
        restarted.stderr.close()


def test_inverse_refuses_when_later_actions_return_to_the_same_weights(router):
    router.send(shift('forward', 25))
    inverse = router.inverse_of(shift('forward', 25))
    router.send(shift('later-50', 50))
    router.send(shift('later-25', 25))
    undo = Action('undo', 'run', 'root', 'person:operator', inverse.tool, inverse.target,
                  inverse.args, reverses='forward')
    answer = router.send(undo)
    assert not answer.applied and answer.reason == Reason.STATE_CHANGED
    assert router.state()['last_action_id'] == 'later-25'
    assert router.state()['weights']['region-b'] == 25


@pytest.mark.parametrize('changed_args', [False, True])
def test_mock_cut_withholds_the_next_duplicate_answer(changed_args):
    from stopline.targets.mock import MockCluster

    cluster = MockCluster()
    action = Action('restart', 'run', 'worker', 'agent:worker-1', 'restart',
                    'cluster/checkout-worker', {})
    cluster.send(action)
    cluster.cut()
    if changed_args:
        action = replace(action, args={'changed': True})
    with pytest.raises(TargetTimeout):
        cluster.send(action)
    assert not cluster.state()['reachable']
    with pytest.raises(TargetUnreachable):
        cluster.lookup('restart')
    cluster.restore()
    assert cluster.lookup('restart').applied
    assert len(cluster.state()['log']) == 1
