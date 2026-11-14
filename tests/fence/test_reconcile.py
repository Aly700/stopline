"""Reconciliation must record the target decision, including a late arrival."""

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from stopline.targets import build_sender
from .fence_helpers import (TRIALS, WAIT_SECONDS, RecordingGate, action_for, assert_decision,
                            assert_recorded, race, record_counts)


def test_reconcile_racing_a_late_send_records_the_target_decision(target, monkeypatch, record_property):
    counts = {'applied': 0, 'refused': 0}
    original_fence = target.fence
    with ThreadPoolExecutor(max_workers=2) as pool:
        for trial in range(TRIALS):
            before = target.state()
            action = action_for(target, before, f'reconcile-{trial}')
            gate = RecordingGate(action)
            sender = build_sender(gate, {target.name: target})
            assert sender.claim(action.action_id).claimed
            deciding = threading.Barrier(2, timeout=WAIT_SECONDS)

            def late_send():
                deciding.wait()
                return target.send(action)

            def concurrent_fence(action_id):
                deciding.wait()
                return original_fence(action_id)

            # Let the real lookup miss, then release delivery and the real fence
            # together. Neither HTTP round-trip count nor a sleep picks a winner.
            with monkeypatch.context() as patch:
                patch.setattr(target, 'fence', concurrent_fence)
                try:
                    sent, recovered = race(pool, late_send,
                                           lambda: sender.reconcile(action.action_id),
                                           reverse=bool(trial % 2))
                finally:
                    deciding.abort()
            outcome = assert_decision(target, action, before, sent, recovered['evidence'])
            assert_recorded(gate, action, sent, recovered)
            counts[outcome] += 1
    record_counts(target, 'send_reconcile', counts, record_property)


@pytest.mark.parametrize('arrives_before_fence', [True, False], ids=['send-wins', 'fence-wins'])
def test_sweep_after_a_missing_lookup_records_who_wins(target, monkeypatch, arrives_before_fence):
    before = target.state()
    action = action_for(target, before, 'missing-lookup')
    gate = RecordingGate(action)
    sender = build_sender(gate, {target.name: target})
    assert sender.claim(action.action_id).claimed
    original_lookup = target.lookup
    missing_seen = threading.Event()
    resume = threading.Event()

    def held_lookup(action_id):
        missing = original_lookup(action_id)
        assert not missing.found
        missing_seen.set()
        assert resume.wait(WAIT_SECONDS), 'lookup was not released'
        return missing

    with ThreadPoolExecutor(max_workers=1) as pool:
        with monkeypatch.context() as patch:
            patch.setattr(target, 'lookup', held_lookup)
            swept = pool.submit(sender.sweep, [action.action_id])
            try:
                assert missing_seen.wait(WAIT_SECONDS), 'sweep did not look up the claimed id'
                if arrives_before_fence:
                    sent = target.send(action)
            finally:
                resume.set()
            recovered, = swept.result(timeout=WAIT_SECONDS)
        if not arrives_before_fence:
            after_sweep = target.state()
            sent = target.send(action)
            assert target.state() == after_sweep

    assert sent.applied is arrives_before_fence
    assert_decision(target, action, before, sent, recovered['evidence'])
    assert_recorded(gate, action, sent, recovered)
    # A repeated sweep returns the terminal decision without recording it again.
    assert sender.sweep([action.action_id]) == [recovered]
    assert_recorded(gate, action, sent, recovered)
