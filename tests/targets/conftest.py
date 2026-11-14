"""Small gate double: this suite does not test MongoDB admission or stop."""

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import socket
import subprocess
import sys
import threading
import time

import pytest

from stopline.contract import Action, ChainView, Claim, EffectState, Reason, args_hash


class FakeGate:
    def __init__(self, effects=None):
        self.effects = effects if effects is not None else {}
        self.lock = threading.Lock()

    def admit(self, action):
        fields = asdict(action)
        fields['_id'] = fields.pop('action_id')
        self.effects[action.action_id] = {
            **fields, 'root_id': 'root', 'mode': 'stopline', 'chain': ['root', 'worker'],
            'args_hash': args_hash(action.args), 'admitted_seq': 1, 'state': 'admitted',
            'evidence': None, 'undo': None, 'policy_version': 1, 'policy_hash': 'test',
            'claimed_by': None, 'admitted_at': datetime.now(timezone.utc),
            'sent_at': None, 'finished_at': None,
        }

    def claim(self, action_id, by):
        with self.lock:
            effect = self.effects.get(action_id)
            if not effect or effect['state'] != EffectState.ADMITTED:
                return Claim(False, action_id, effect['state'] if effect else '', Reason.NOT_ADMITTED)
            effect = deepcopy(effect)
            effect.update(state='sent', claimed_by=by, sent_at=datetime.now(timezone.utc))
            self.effects[action_id] = effect
            return Claim(True, action_id, EffectState.SENT)

    def chain(self, action_id):
        return ChainView(action_id, deepcopy(self.effects.get(action_id)), [], None, [])

    def record_outcome(self, action_id, state, evidence):
        with self.lock:
            effect = deepcopy(self.effects[action_id])
            if effect['state'] == EffectState.SENT:
                assert evidence['action_id'] == action_id
                assert evidence['system'] in ('router', 'cluster')
                assert isinstance(evidence['at_epoch'], (int, float))
                effect.update(state=state, evidence=deepcopy(evidence), finished_at=datetime.now(timezone.utc))
                self.effects[action_id] = effect
            return deepcopy(effect)


def shift(action_id='shift-1', percent=25, **args):
    return Action(action_id, 'run', 'worker', 'agent:worker-2', 'shift_traffic',
                  'router/region-b', {'percent': percent, **args})


@pytest.fixture
def gate():
    return FakeGate()


@pytest.fixture
def router_process(tmp_path):
    from stopline.targets.router import RouterAdapter

    # An ephemeral test port avoids touching the demo's listener on 8766.
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    store = tmp_path / 'router.sqlite'
    process = subprocess.Popen(
        [sys.executable, '-m', 'stopline.targets.router', '--port', str(port), '--store', str(store)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    adapter = RouterAdapter(f'http://127.0.0.1:{port}', timeout=0.3)
    try:
        deadline = time.monotonic() + 5
        while not adapter.state()['reachable']:
            if process.poll() is not None:
                pytest.fail(process.stderr.read())
            assert time.monotonic() < deadline, 'router did not start within five seconds'
            time.sleep(0.02)
        yield adapter, store, process
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        process.stderr.close()


@pytest.fixture
def router(router_process):
    return router_process[0]
