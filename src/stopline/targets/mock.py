"""A cluster with the same idempotency and evidence contract, kept in memory."""

from copy import deepcopy
from datetime import datetime, timezone
import threading

from stopline.contract import Action, Lookup, Reason, TargetTimeout, TargetUnreachable, Tool
from .common import evidence, fingerprint, result


class MockCluster:
    name = 'cluster'

    def __init__(self):
        self.lock = threading.RLock()
        self.reset()

    def reset(self):
        with self.lock:
            self.mode = 'healthy'
            self.records = {}
            self.deployments = {
                name: {'image': f'stopline/checkout:{version}', 'version': version, 'revision': 1,
                       'restarted_at': None, 'action_id': None, 'ready': True}
                for name, version in (('checkout-api', 'v2'), ('checkout-worker', 'v1'))
            }

    def _check(self):
        if self.mode == 'offline':
            raise TargetUnreachable('mock cluster is unreachable')

    def _answer(self, record):
        if self.mode == 'armed':
            self.mode = 'offline'
            raise TargetTimeout('mock cluster withheld the answer')
        return result(deepcopy(record))

    def send(self, action: Action):
        with self.lock:
            self._check()
            signature = fingerprint(action)
            if action.action_id in self.records:
                prior_signature, record = self.records[action.action_id]
                if prior_signature is not None and prior_signature != signature:
                    record = evidence(action.action_id, self.name, False, Reason.SAME_ID_DIFFERENT_ARGS)
                return self._answer(record)
            reason = Reason.OK
            name = action.target.split('/', 1)[-1]
            if action.target != f'cluster/{name}' or name not in self.deployments:
                reason = Reason.TARGET_NOT_ALLOWED
            elif action.tool not in (Tool.ROLLBACK, Tool.RESTART):
                reason = Reason.TOOL_NOT_ALLOWED
            elif action.tool == Tool.ROLLBACK and action.args.get('to_version') != 'v1':
                reason = Reason.LIMIT_EXCEEDED
            if reason != Reason.OK:
                record = evidence(action.action_id, self.name, False, reason, fenced=True)
            else:
                deployment = self.deployments[name]
                before = deepcopy(deployment)
                if action.tool == Tool.ROLLBACK:
                    deployment.update(image='stopline/checkout:v1', version='v1')
                else:
                    deployment['restarted_at'] = datetime.now(timezone.utc).isoformat()
                deployment.update(action_id=action.action_id, revision=deployment['revision'] + 1)
                record = evidence(action.action_id, self.name, True, deployment=name, before=before,
                                  after=deepcopy(deployment))
            record['log_id'] = len(self.records) + 1
            self.records[action.action_id] = (signature, record)
            return self._answer(record)

    def lookup(self, action_id: str) -> Lookup:
        with self.lock:
            self._check()
            row = self.records.get(action_id)
            record = deepcopy(row[1]) if row else evidence(action_id, self.name, False)
            return Lookup(row is not None, record['applied'], record)

    def fence(self, action_id: str) -> dict:
        with self.lock:
            self._check()
            if action_id not in self.records:
                record = evidence(action_id, self.name, False, Reason.FENCED, fenced=True,
                                  log_id=len(self.records) + 1)
                self.records[action_id] = (None, record)
            return deepcopy(self.records[action_id][1])

    def state(self) -> dict:
        with self.lock:
            if self.mode == 'offline':
                return {'system': self.name, 'reachable': False}
            return {'system': self.name, 'reachable': True, 'deployments': deepcopy(self.deployments),
                    'log': [deepcopy(row[1]) for row in self.records.values()]}

    def inverse_of(self, action: Action):
        return None

    def cut(self):
        with self.lock:
            if self.mode != 'offline':
                self.mode = 'armed'

    def restore(self):
        with self.lock:
            self.mode = 'healthy'
