"""A send and a fence for one id must agree on one immutable decision."""

from concurrent.futures import ThreadPoolExecutor

from stopline.contract import Reason
from .fence_helpers import TRIALS, action_for, assert_decision, race, record_counts


def test_send_then_fence_preserves_the_application(target):
    before = target.state()
    action = action_for(target, before, 'send-first')
    assert not target.lookup(action.action_id).found
    sent = target.send(action)
    after_send = target.state()
    fenced = target.fence(action.action_id)

    assert sent.applied
    assert_decision(target, action, before, sent, fenced)
    assert target.state() == after_send


def test_fence_then_send_refuses_without_changing_state_or_log(target):
    before = target.state()
    action = action_for(target, before, 'fence-first')
    assert not target.lookup(action.action_id).found
    fenced = target.fence(action.action_id)
    after_fence = target.state()
    sent = target.send(action)

    assert not sent.applied and sent.reason == Reason.FENCED
    assert_decision(target, action, before, sent, fenced)
    assert target.state() == after_fence
    assert target.fence(action.action_id) == fenced
    assert target.send(action) == sent
    assert target.state() == after_fence


def test_send_and_fence_choose_one_winner(target, record_property):
    counts = {'applied': 0, 'refused': 0}
    with ThreadPoolExecutor(max_workers=2) as pool:
        for trial in range(TRIALS):
            before = target.state()
            action = action_for(target, before, f'race-{trial}')
            sent, fenced = race(pool, lambda: target.send(action),
                                lambda: target.fence(action.action_id), reverse=bool(trial % 2))
            outcome = assert_decision(target, action, before, sent, fenced)
            counts[outcome] += 1
    record_counts(target, 'send_fence', counts, record_property)


def test_late_duplicate_after_fence_returns_the_first_result(target):
    before = target.state()
    action = action_for(target, before, 'duplicate')
    first = target.send(action)
    assert first.applied
    after_first = target.state()
    fenced = target.fence(action.action_id)
    duplicate = target.send(action)

    assert duplicate == first
    assert_decision(target, action, before, duplicate, fenced)
    assert target.state() == after_first


def test_late_duplicate_does_not_overwrite_a_later_application(target):
    before = target.state()
    action = action_for(target, before, 'old-duplicate')
    first = target.send(action)
    assert first.applied
    after_first = target.state()
    later_action = action_for(target, after_first, 'newer-application')
    later = target.send(later_action)
    assert later.applied
    after_later = target.state()
    assert after_later['log'] == [first.evidence, later.evidence]

    assert target.fence(action.action_id) == first.evidence
    assert target.send(action) == first
    lookup = target.lookup(action.action_id)
    assert lookup.found and lookup.applied
    assert lookup.evidence == first.evidence
    assert target.state() == after_later


def test_router_fence_survives_process_restart(router_process):
    router = router_process.adapter
    before = router.state()
    action = action_for(router, before, 'restart-fence')
    fenced = router.fence(action.action_id)
    after_fence = router.state()
    original_pid = router_process.process.pid

    router_process.stop()
    restarted = router_process.start()
    assert router_process.process.pid != original_pid
    assert restarted.state() == after_fence
    sent = restarted.send(action)

    assert not sent.applied and sent.reason == Reason.FENCED
    assert_decision(restarted, action, before, sent, fenced)
    assert restarted.state() == after_fence
