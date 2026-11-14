"""Bounded races and observations of each target's own state and ledger."""

from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from uuid import uuid4

from stopline.contract import Action, ChainView, Claim, EffectState, Reason, Tool
from stopline.targets.router import RouterAdapter


TRIALS = 200
WAIT_SECONDS = 5
FORBIDDEN_PORTS = {8765, 8766, 8775, 8776, 8785, 8786, 8795, 8796}


class RouterProcess:
    """A real HTTP process; restarts retain only its private SQLite file."""

    def __init__(self, store: Path):
        self.store = store
        self.process = None

    def start(self):
        assert self.process is None
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        assert port not in FORBIDDEN_PORTS
        self.process = subprocess.Popen(
            [sys.executable, '-m', 'stopline.targets.router', '--port', str(port),
             '--store', str(self.store)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        self.adapter = RouterAdapter(f'http://127.0.0.1:{port}', timeout=2)
        try:
            deadline = time.monotonic() + WAIT_SECONDS
            while not self.adapter.state()['reachable']:
                assert self.process.poll() is None, self.process.stderr.read()
                assert time.monotonic() < deadline, 'router did not become reachable'
                time.sleep(0.01)
        except BaseException:
            self.stop()
            raise
        return self.adapter

    def stop(self):
        if self.process is None:
            return
        try:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)
        finally:
            self.process.stderr.close()
            self.process = None


def action_for(target, before, label):
    """Every successful action visibly changes the resource, even after a prior trial."""
    identity = f'{label}-{uuid4().hex}'
    if target.name == 'router':
        percent = 25 if before['weights']['region-b'] != 25 else 50
        return Action(identity, 'fence-test', 'worker', 'agent:worker-2', Tool.SHIFT,
                      'router/region-b', {'percent': percent})
    return Action(identity, 'fence-test', 'worker', 'agent:worker-1', Tool.RESTART,
                  'cluster/checkout-worker', {})


def race(pool, first, second, *, reverse=False):
    """Release both workers together; alternate submission order without sleeps."""
    barrier = threading.Barrier(3, timeout=WAIT_SECONDS)

    def run(operation):
        barrier.wait()
        return operation()

    operations = [first, second]
    if reverse:
        operations.reverse()
    futures = [pool.submit(run, operation) for operation in operations]
    try:
        barrier.wait()
        answers = [future.result(timeout=WAIT_SECONDS) for future in futures]
    finally:
        barrier.abort()
    return tuple(reversed(answers)) if reverse else tuple(answers)


def assert_decision(target, action, before, sent, fenced):
    """Only one durable record may explain the answer, resource change and lookup."""
    after = target.state()
    lookup = target.lookup(action.action_id)
    record = sent.evidence
    assert after['reachable'], action.action_id
    assert record == fenced == lookup.evidence, action.action_id
    assert lookup.found
    assert lookup.applied is sent.applied is record['applied']
    assert record['action_id'] == action.action_id
    assert record['system'] == target.name
    assert isinstance(record['at_epoch'], (int, float))
    assert after['log'] == before['log'] + [record]
    assert sum(row['action_id'] == action.action_id for row in after['log']) == 1

    expected = deepcopy(before)
    expected['log'].append(record)
    if sent.applied:
        assert sent.reason == record['reason'] == Reason.OK
        assert not record.get('fenced', False), 'an applied id cannot also be fenced'
        if target.name == 'router':
            percent = action.args['percent']
            expected['weights'] = {'region-a': 100 - percent, 'region-b': percent}
            expected['last_action_id'] = action.action_id
            assert record['before'] == before['weights']
            assert record['weights'] == expected['weights']
        else:
            name = 'checkout-worker'
            deployment = expected['deployments'][name]
            deployment.update(revision=deployment['revision'] + 1,
                              action_id=action.action_id,
                              restarted_at=record['after']['restarted_at'])
            assert deployment['restarted_at']
            assert record['deployment'] == name
            assert record['before'] == before['deployments'][name]
            assert record['after'] == deployment
    else:
        assert sent.reason == record['reason'] == Reason.FENCED
        assert record['fenced'] is True
    # A refusal may append a fence record, but must not change any resource.
    # An application must change exactly the resource described by its evidence.
    assert after == expected, action.action_id
    return 'applied' if sent.applied else 'refused'


def record_counts(target, label, counts, record_property):
    assert sum(counts.values()) == TRIALS
    for outcome, count in counts.items():
        record_property(f'{label}_{outcome}', count)
    print(f'{target.name} {label}: applied={counts["applied"]}, '
          f'refused={counts["refused"]}, trials={TRIALS}')


class RecordingGate:
    """One admitted action, a claim, and every attempted outcome, including conflicts."""

    def __init__(self, action):
        self.lock = threading.Lock()
        fields = asdict(action)
        fields['_id'] = fields.pop('action_id')
        self.effect = dict(fields, state=EffectState.ADMITTED, claimed_by=None, evidence=None)
        self.outcomes = []

    def claim(self, action_id, by):
        with self.lock:
            assert action_id == self.effect['_id']
            if self.effect['state'] != EffectState.ADMITTED:
                return Claim(False, action_id, self.effect['state'], Reason.NOT_ADMITTED)
            self.effect.update(state=EffectState.SENT, claimed_by=by)
            return Claim(True, action_id, EffectState.SENT)

    def chain(self, action_id):
        with self.lock:
            assert action_id == self.effect['_id']
            return ChainView(action_id, deepcopy(self.effect), [], None, [])

    def record_outcome(self, action_id, state, evidence):
        with self.lock:
            assert action_id == self.effect['_id']
            self.outcomes.append((action_id, state, deepcopy(evidence)))
            if self.effect['state'] == EffectState.SENT:
                self.effect.update(state=state, evidence=deepcopy(evidence))
            return deepcopy(self.effect)


def assert_recorded(gate, action, sent, effect, *, calls=1):
    expected = EffectState.APPLIED if sent.applied else EffectState.REJECTED
    assert gate.outcomes == [(action.action_id, expected, sent.evidence)] * calls
    assert effect == gate.chain(action.action_id).effect
    assert effect['state'] == expected
    assert effect['evidence'] == sent.evidence
