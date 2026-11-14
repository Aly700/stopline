"""Component construction and instrumentation, the only backend switching seam."""

import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from stopline.contract import Action, GateAPI, Sender, TargetAdapter, TargetUnreachable
from stopline.proof.fakes import FakeGate, FakeSender, FakeTarget
from stopline.proof.store import MemoryDatabase


class UnsupportedSchedule(Exception):
    pass


class ObservedTarget:
    """Sender transport boundary. Target evidence still comes only from the target.

    A dispatch hook models a queued request, after dispatch but before delivery.
    A return hook holds the worker after delivery and before outcome persistence.
    Re-delivery models the transport replaying an already captured request.
    """

    def __init__(self, target: TargetAdapter, db):
        self.target, self.db, self.name = target, db, target.name
        self.on_dispatch = {}
        self.on_return = {}
        self.requests = {}
        self.fault_active = False

    def send(self, action: Action):
        self.requests[action.action_id] = action
        dispatch_id = uuid4().hex
        self.db['proof_sends'].insert_one({
            '_id': dispatch_id, 'dispatched': True,
            'run_id': action.run_id, 'action_id': action.action_id,
            'system': self.name, 'at_epoch': time.time(),
        })
        hook = self.on_dispatch.pop(action.action_id, None)
        if hook:
            hook()
        try:
            return self.target.send(action)
        except TargetUnreachable:
            # The protocol guarantees this call did not accept the connection.
            self.db['proof_sends'].update_one({'_id': dispatch_id}, {'$set': {'dispatched': False}})
            raise
        finally:
            hook = self.on_return.pop(action.action_id, None)
            if hook:
                hook()

    def redeliver(self, action_id):
        return self.send(self.requests[action_id])

    def lookup(self, action_id):
        return self.target.lookup(action_id)

    def fence(self, action_id):
        return self.target.fence(action_id)

    def state(self):
        return self.target.state()

    def inverse_of(self, action):
        return self.target.inverse_of(action)

    def cut(self):
        self.fault_active = True
        return self.target.cut()

    def restore(self):
        result = self.target.restore()
        self.fault_active = False
        return result


@dataclass
class Runtime:
    db: object
    gate: GateAPI
    targets: dict[str, ObservedTarget]
    sender: Sender
    backend: str

    def expire(self):
        if self.backend != 'fake':
            raise UnsupportedSchedule('real gate has no injected server clock; expiry hook needs integration')
        self.gate.clock_offset += 3601

    def force_retry(self, callback):
        if self.backend != 'fake':
            raise UnsupportedSchedule('real transaction retry injection is not integrated')
        self.gate.retry_once = callback

    def restart_router(self):
        if self.backend != 'fake':
            raise UnsupportedSchedule('real router process restart controller is not integrated')
        self.targets['router'].target.restart()

    def close(self):
        for target in self.targets.values():
            try:
                if target.fault_active:
                    target.restore()
            finally:
                if self.backend == 'fake':
                    target.target.close()


SOURCES = {
    'gate': 'protocol fake; no MongoDB transactions',
    'sender': 'protocol fake',
    'workers': 'two deterministic worker routines; no worker processes',
    'cluster': 'protocol fake with independent in-memory SQLite target log; no Kubernetes',
    'router': 'protocol fake with target-owned on-disk SQLite journal; restart reopens the connection, not an HTTP process',
    'retry': 'synthetic callback retry with a stop between authority reads; not a PyMongo retry',
    'expiry': 'injected gate clock crossing a stored deadline; not Atlas server-time expiry',
    'timing': 'measured local wall durations of fake check/claim calls; not Atlas gate latency',
    'record_storage': 'in-memory records; CLI additionally persists and rereads them through Atlas scratch collections',
}


def build_runtime(*, backend='fake', work_dir: Path, db=None, router_url='http://127.0.0.1:8766') -> Runtime:
    """After merge, select --backend real. The three factories are all here.

    Real mode requires an already-running isolated local router and complete
    target-owned state()['log'] journals with event='applied' or 'fenced'. Missing
    capabilities are explicit skips, never silently replaced by fake results.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    if backend == 'fake':
        db = MemoryDatabase() if db is None else db
        gate = FakeGate(db)
        raw_targets = {'cluster': FakeTarget('cluster'), 'router': FakeTarget('router', work_dir / 'router.sqlite')}
        targets = {key: ObservedTarget(target, db) for key, target in raw_targets.items()}
        sender = FakeSender(gate, targets)
    elif backend == 'real':
        if db is None or not db.name.startswith('stopline_test_proof_'):
            raise ValueError('real backend requires a proof scratch database')
        router = urlparse(router_url)
        if (router.scheme != 'http' or router.hostname not in ('127.0.0.1', 'localhost', '::1')
                or router.port != 8766 or router.username or router.password):
            raise ValueError('real target timestamps require a loopback router on port 8766')
        from stopline.gate import build_gate
        from stopline.targets import build_sender, build_targets

        gate = build_gate(db)
        raw_targets = build_targets(cluster='mock', router_url=router_url)
        targets = {key: ObservedTarget(target, db) for key, target in raw_targets.items()}
        sender = build_sender(gate, targets)
    else:
        raise ValueError('backend must be fake or real')
    return Runtime(db, gate, targets, sender, backend)
