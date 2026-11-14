"""Scripted records for exercising the judge's sequence without external actions."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from queue import Empty, Queue
import threading
import time

from stopline.contract import DEMO_HOLDS, DEMO_PLAN, StageResult, action_id_for

NOW = datetime(2026, 11, 14, 18, 0, tzinfo=timezone.utc)


class Cursor(list):
    def sort(self, key, direction=1):
        return Cursor(sorted(self, key=lambda row: row.get(key) or 0, reverse=direction < 0))

    def limit(self, count):
        return Cursor(self[:count])


class Collection:
    def __init__(self, db, name):
        self.db, self.name = db, name

    def find(self, query=None):
        query = query or {}
        def matches(row):
            return all(row.get(key) in val['$in'] if isinstance(val, dict) and '$in' in val
                       else row.get(key) == val for key, val in query.items())
        return Cursor(deepcopy([row for row in self.db.records.get(self.name, []) if matches(row)]))

    def find_one(self, query=None, sort=None):
        rows = self.find(query)
        if sort:
            for key, direction in reversed(sort):
                rows = rows.sort(key, direction)
        return rows[0] if rows else None

    def aggregate(self, pipeline):
        # The production read is a run plus bounded, run-scoped lookups.
        rows = self.find().sort('created_at', -1).limit(1)
        if rows:
            for lookup in (part['$lookup'] for part in pipeline if '$lookup' in part):
                rows[0][lookup['as']] = self.db[lookup['from']].find({'run_id': rows[0]['_id']})
        self.db.reads += 1
        return iter(rows)


class Stream:
    def __init__(self, db):
        self.db, self.closed = db, False
        self.resume_token = None

    def __enter__(self):
        self.db.opened.set()
        return self

    def __exit__(self, *args):
        self.close()

    def try_next(self):
        if self.closed:
            return None
        try:
            change = self.db.changes.get(timeout=.03)
            if isinstance(change, Exception):
                raise change
            self.resume_token = {'n': self.db.watches}
            return change
        except Empty:
            return None

    def close(self):
        self.closed = True
        self.db.closed += 1


class FakeDB:
    def __init__(self):
        self.records = {name: [] for name in ('runs', 'effects', 'audit', 'receipts', 'mandates', 'tasks', 'policies')}
        self.changes = Queue()
        self.opened = threading.Event()
        self.watches = self.closed = self.reads = 0

    def __getitem__(self, name):
        return Collection(self, name)

    def watch(self, *args, **kwargs):
        self.watches += 1
        return Stream(self)

    def changed(self):
        self.changes.put({'operationType': 'update', 'ns': {'coll': 'effects'}})


class Target:
    def __init__(self, name):
        self.name = name
        self.data = ({'system': name, 'reachable': True, 'deployments': {
            'checkout-api': {'image': 'checkout:v1', 'version': 'v1', 'revision': 2, 'restarted_at': None, 'action_id': 'rollback', 'ready': True},
            'checkout-worker': {'image': 'checkout:v2', 'version': 'v2', 'revision': 1, 'restarted_at': None, 'action_id': None, 'ready': True}}}
            if name == 'cluster' else {'system': name, 'reachable': True, 'weights': {'region-a': 75, 'region-b': 25}, 'last_action_id': 'shift', 'log': []})

    def state(self):
        return deepcopy(self.data)


class ScriptedStage:
    def __init__(self, db, targets):
        self.db, self.targets, self.calls = db, targets, []
        self.run = None
        self.serial = 0

    def result(self, name, message):
        self.calls.append(name)
        self.db.changed()
        return StageResult(True, message, run_id=self.run['_id'] if self.run else None)

    def effect(self, worker, step, state, seq):
        principal = f'agent:worker-{worker}'
        spec = DEMO_PLAN[principal][step]
        aid = action_id_for(self.run['_id'], principal, step)
        record = dict(_id=aid, run_id=self.run['_id'], mode=self.run['mode'], root_id='root',
                      mandate_id=f'w{worker}', chain=['root', 'repair', f'w{worker}'], principal=principal,
                      **deepcopy(spec), args_hash='hash', admitted_seq=seq, state=state,
                      evidence={'system': spec['target'].split('/')[0], 'action_id': aid, 'at_epoch': NOW.timestamp(), 'applied': True} if state == 'applied' else None,
                      reverses=None, undo=None, policy_version=1, policy_hash='policy-hash', claimed_by='sender',
                      admitted_at=NOW + timedelta(seconds=seq or step), sent_at=NOW, finished_at=NOW if state == 'applied' else None)
        if self.run['mode'] != 'stopline':
            record.update(root_id=None, mandate_id=None, chain=[], admitted_seq=None, policy_version=None)
        self.db.records['effects'].append(record)
        return record

    def start_run(self, mode, holds=None):
        self.serial += 1
        self.run = dict(_id=f'run-{self.serial}', mode=mode, cluster='mock', status='running',
                        root_mandate_id='root', agent_mandate_id='repair', stop_flag=False, stop_at=None,
                        stop_at_local=None, holds=deepcopy(DEMO_HOLDS[mode]), receipt_id=None,
                        planner='scripted', created_at=NOW + timedelta(minutes=self.serial))
        self.db.records['runs'].append(self.run)
        for mid, parent, holder, grantor in [('root', None, 'person:operator', 'person:operator'), ('repair', 'root', 'agent:repair', 'person:operator'), ('w1', 'repair', 'agent:worker-1', 'agent:repair'), ('w2', 'repair', 'agent:worker-2', 'agent:repair')]:
            self.db.records['mandates'].append(dict(_id=mid, run_id=self.run['_id'], parent_id=parent, holder=holder, grantor=grantor, status='active'))
        self.db.records['policies'] = [dict(_id=1, text='Check permission before every action.', hash='policy-hash')]
        self.effect(1, 0, 'applied', 4)
        self.effect(2, 0, 'sent' if mode == 'stopline' else 'applied', 5)
        if mode == 'stopline':
            self.effect(1, 1, 'admitted', 6)
        for worker in (1, 2):
            principal = f'agent:worker-{worker}'
            step = 0 if mode == 'stopline' and worker == 2 else 1
            self.db.records['tasks'].append(dict(_id=f'{self.run["_id"]}:{principal}', run_id=self.run['_id'], principal=principal, status='held', position=step,
                held_at={'step': step, 'point': 'after_claim' if step == 0 else 'after_check'},
                plan=[dict(action_id=action_id_for(self.run['_id'], principal, index), **deepcopy(spec)) for index, spec in enumerate(DEMO_PLAN[principal])]))
        self.targets['router'].data.update(reachable=True, weights={'region-a': 75, 'region-b': 25})
        return self.result('start:' + mode, 'Permission granted. Two workers ready.' if mode == 'stopline' else 'Stop flag run started.')

    def deny(self, worker, step):
        principal = f'agent:worker-{worker}'
        self.db.records['audit'].append(dict(_id=f'deny-{worker}', run_id=self.run['_id'], kind='deny', principal=principal,
            mandate_id=f'w{worker}', root_id='root', action_id=action_id_for(self.run['_id'], principal, step),
            policy_version=1, policy_hash='policy-hash', reason='revoked', seq=8, at=NOW + timedelta(seconds=12 + worker),
            detail={**deepcopy(DEMO_PLAN[principal][step]), 'revoked_by': 'person:operator', 'revoked_at': NOW + timedelta(seconds=10)}))

    def press_stop(self):
        self.run.update(stop_flag=True, status='stopped', stop_at=NOW + timedelta(seconds=10), stop_at_local=NOW.timestamp() + 10)
        for hold in self.run['holds']:
            hold['released'] = True
        for task in self.db.records['tasks']:
            if task['run_id'] == self.run['_id'] and task['status'] != 'killed':
                task.update(status='stopped', held_at=None)
        if self.run['mode'] == 'stopline':
            for effect in self.db.records['effects']:
                if effect['run_id'] == self.run['_id'] and effect['state'] == 'admitted':
                    effect['state'] = 'cancelled'
            receipt = dict(_id='receipt-' + self.run['_id'], run_id=self.run['_id'], root_id='root', mandate_id='repair', stopped_by='person:operator',
                stop_seq=7, stopped_at=self.run['stop_at'], revoked=['repair', 'w1', 'w2'],
                covered=[e['_id'] for e in self.db.records['effects'] if e['run_id'] == self.run['_id']], status='open', closed_at=None)
            self.db.records['receipts'].append(receipt)
            self.run['receipt_id'] = receipt['_id']
            self.deny(1, 1)
        else:
            effect = self.effect(1, 1, 'applied', None)
            effect.update(admitted_at=NOW + timedelta(seconds=12), finished_at=NOW + timedelta(seconds=12))
            effect['evidence']['at_epoch'] = NOW.timestamp() + 12
            self.targets['cluster'].data['deployments']['checkout-worker'].update(revision=2, restarted_at='2026-11-14T18:00:12Z', action_id=effect['_id'])
        return self.result('stop', 'Stop confirmed by Atlas.')

    def cut_and_kill(self):
        self.targets['router'].data['reachable'] = False
        self.db.records['tasks'][-1].update(status='killed', held_at=None)
        return self.result('cut', 'Worker two crashed. Router unreachable.')

    def resume_worker(self):
        self.db.records['tasks'][-1]['status'] = 'stopped'
        self.deny(2, 1)
        return self.result('resume', 'Worker two resumed. Next action refused.')

    def restore_and_reconcile(self):
        self.targets['router'].data['reachable'] = True
        for effect in self.db.records['effects']:
            if effect['run_id'] == self.run['_id'] and effect['state'] == 'sent':
                effect.update(state='applied', evidence={'system': 'router', 'action_id': effect['_id'], 'at_epoch': NOW.timestamp() + 20, 'applied': True})
        self.db.records['receipts'][-1].update(status='closed', closed_at=NOW + timedelta(seconds=20))
        return self.result('restore', 'Asked router about the action. Router: applied.')

    def undo(self):
        self.targets['router'].data['weights'] = {'region-a': 100, 'region-b': 0}
        for effect in self.db.records['effects']:
            if effect['run_id'] == self.run['_id']:
                effect['undo'] = 'no_undo' if effect['tool'] == 'restart' else 'undone'
        return self.result('undo', 'Traffic shift undone under the operator’s permission.')

    def validation_rejection(self):
        return self.result('validation', 'Database refused the malformed record.')

    def status(self):
        return {'run': self.run}
