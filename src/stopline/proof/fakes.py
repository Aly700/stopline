"""Explicit protocol fakes. These do not test MongoDB atomicity or real workers.

The gate executes serially. Its retry hook discards an initial authority read,
then repeats the check; this is a model of callback retry, not a driver retry.
The router keeps its own SQLite journal across an adapter restart.
"""

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from stopline.contract import (
    Action, Admission, ChainView, Claim, HandDown, Lookup, NotClaimed,
    ReceiptView, SendResult, TargetTimeout, TargetUnreachable, args_hash, system_of,
)


def now():
    return datetime.now(timezone.utc)


def effect_for(action, mode, *, root_id=None, chain=(), seq=None):
    return {
        '_id': action.action_id, 'run_id': action.run_id, 'mode': str(mode),
        'root_id': root_id, 'mandate_id': action.mandate_id if mode == 'stopline' else None,
        'chain': list(chain), 'principal': action.principal, 'tool': str(action.tool),
        'target': action.target, 'args': action.args, 'args_hash': args_hash(action.args),
        'admitted_seq': seq, 'state': 'admitted', 'evidence': None,
        'reverses': action.reverses, 'undo': None, 'policy_version': 1 if chain else None,
        'policy_hash': 'fake-policy' if chain else None, 'claimed_by': None,
        'admitted_at': now(), 'sent_at': None, 'finished_at': None,
    }


def action_for(effect):
    return Action(effect['_id'], effect['run_id'], effect.get('mandate_id') or '',
                  effect['principal'], effect['tool'], effect['target'], effect['args'], effect.get('reverses'))


class FakeGate:
    def __init__(self, db):
        self.db = db
        self.clock_offset = 0
        self.retry_once = None

    def setup(self):
        pass

    def _clock(self):
        return now() + timedelta(seconds=self.clock_offset)

    def _save(self, collection, row):
        self.db[collection].replace_one({'_id': row['_id']}, row, upsert=True)

    def _tick(self, root_id):
        root = self.db['mandates'].find_one({'_id': root_id})
        root['seq'] += 1
        root['clock'] = self._clock()
        self._save('mandates', root)
        return root['seq']

    def _chain(self, mandate_id):
        chain = []
        while mandate_id:
            mandate = self.db['mandates'].find_one({'_id': mandate_id})
            if mandate is None:
                return []
            chain.insert(0, mandate)
            mandate_id = mandate['parent_id']
        return chain

    def _reason(self, chain):
        if not chain:
            return 'unknown_mandate'
        if any(m['status'] != 'active' for m in chain):
            return 'revoked'
        if any(m['expires_at'] <= self._clock() for m in chain):
            return 'expired'
        return 'ok'

    def _audit(self, kind, mandate, *, action_id=None, reason='ok', seq=None, detail=None):
        self.db['audit'].insert_one({
            '_id': uuid4().hex, 'run_id': mandate['run_id'], 'kind': kind,
            'principal': mandate['holder'], 'mandate_id': mandate['_id'], 'root_id': mandate['root_id'],
            'action_id': action_id, 'policy_version': 1, 'policy_hash': 'fake-policy',
            'reason': reason, 'seq': seq, 'at': self._clock(), 'detail': detail or {},
        })

    def grant_root(self, *, run_id, grantor, holder, tools, targets, limits, ttl_seconds, mandate_id=None):
        key = mandate_id or uuid4().hex
        row = dict(_id=key, run_id=run_id, root_id=key, parent_id=None, depth=0,
                   grantor=grantor, holder=holder, tools=tools, targets=targets, limits=limits,
                   expires_at=self._clock() + timedelta(seconds=ttl_seconds), status='active',
                   revoked_by=None, revoked_at_seq=None, revoked_at=None, created_at=self._clock(), seq=0)
        self.db['mandates'].insert_one(row)
        return row

    def hand_down(self, *, parent_id, holder, tools, targets, limits, ttl_seconds, mandate_id=None):
        chain = self._chain(parent_id)
        reason = self._reason(chain)
        parent = chain[-1]
        seq = self._tick(parent['root_id'])
        if reason != 'ok':
            self._audit('deny', parent, reason=reason, seq=seq)
            return HandDown(False, None, reason, seq)
        key = mandate_id or uuid4().hex
        row = dict(_id=key, run_id=parent['run_id'], root_id=parent['root_id'], parent_id=parent_id,
                   depth=parent['depth'] + 1, grantor=parent['holder'], holder=holder, tools=tools,
                   targets=targets, limits=limits, expires_at=min(parent['expires_at'], self._clock() + timedelta(seconds=ttl_seconds)),
                   status='active', revoked_by=None, revoked_at_seq=None, revoked_at=None, created_at=self._clock())
        self.db['mandates'].insert_one(row)
        self._audit('hand_down', row, seq=seq)
        return HandDown(True, key, 'ok', seq)

    def admit(self, action):
        previous = self.db['effects'].find_one({'_id': action.action_id})
        if previous is not None:
            same = previous['args_hash'] == args_hash(action.args)
            return Admission(same, action.action_id, 'ok' if same else 'same_id_different_args', previous['admitted_seq'], replayed=same)
        attempts = 1
        if self.retry_once is not None:
            # Read authority on attempt one, then discard that attempt before writes.
            self._reason(self._chain(action.mandate_id))
            hook, self.retry_once = self.retry_once, None
            hook()
            attempts = 2
        chain = self._chain(action.mandate_id)
        reason = self._reason(chain)
        mandate = chain[-1]
        seq = self._tick(mandate['root_id'])
        if reason == 'ok' and action.tool not in mandate['tools']:
            reason = 'tool_not_allowed'
        if reason == 'ok' and action.target not in mandate['targets']:
            reason = 'target_not_allowed'
        if reason != 'ok':
            self._audit('deny', mandate, action_id=action.action_id, reason=reason, seq=seq, detail={'attempts': attempts})
            return Admission(False, action.action_id, reason, seq, attempts=attempts)
        row = effect_for(action, 'stopline', root_id=mandate['root_id'], chain=[m['_id'] for m in chain], seq=seq)
        self.db['effects'].insert_one(row)
        self._audit('allow', mandate, action_id=action.action_id, seq=seq, detail={'attempts': attempts})
        return Admission(True, action.action_id, 'ok', seq, attempts=attempts)

    def claim(self, action_id, by):
        effect = self.db['effects'].find_one({'_id': action_id})
        if not effect or effect['state'] != 'admitted':
            return Claim(False, action_id, effect['state'] if effect else 'missing', 'not_admitted')
        effect.update(state='sent', claimed_by=by, sent_at=now())
        self._save('effects', effect)
        return Claim(True, action_id, 'sent')

    def stop(self, *, mandate_id, by):
        mandate = self.db['mandates'].find_one({'_id': mandate_id})
        seq = self._tick(mandate['root_id'])
        revoked = []
        for row in self.db['mandates'].find({'root_id': mandate['root_id']}):
            if mandate_id in [m['_id'] for m in self._chain(row['_id'])]:
                row.update(status='revoked', revoked_by=by, revoked_at_seq=seq, revoked_at=self._clock())
                self._save('mandates', row)
                revoked.append(row['_id'])
        covered = []
        for effect in self.db['effects'].find({'run_id': mandate['run_id']}):
            if mandate_id in effect['chain'] and effect['admitted_seq'] < seq:
                covered.append(effect['_id'])
                if effect['state'] == 'admitted':
                    effect.update(state='cancelled', finished_at=now())
                    self._save('effects', effect)
        receipt = dict(_id=f'receipt-{mandate_id}', run_id=mandate['run_id'], root_id=mandate['root_id'],
                       mandate_id=mandate_id, stopped_by=by, stop_seq=seq, stopped_at=self._clock(),
                       revoked=revoked, covered=covered, status='open', closed_at=None)
        self.db['receipts'].insert_one(receipt)
        self._audit('stop', mandate, seq=seq)
        self._close_receipts()
        return self.db['receipts'].find_one({'_id': receipt['_id']})

    def _close_receipts(self):
        for receipt in self.db['receipts'].find({'status': 'open'}):
            if all(self.db['effects'].find_one({'_id': key})['state'] in ('applied', 'rejected', 'cancelled')
                   for key in receipt['covered']):
                receipt.update(status='closed', closed_at=self._clock())
                self._save('receipts', receipt)

    def record_outcome(self, action_id, state, evidence):
        effect = self.db['effects'].find_one({'_id': action_id})
        if effect['state'] == 'sent':
            effect.update(state=state, evidence=evidence, finished_at=now())
            self._save('effects', effect)
        self._close_receipts()
        return effect

    def reconcile(self, action_id, target):
        effect = self.db['effects'].find_one({'_id': action_id})
        if effect['state'] != 'sent':
            return effect
        try:
            found = target.lookup(action_id)
            evidence = found.evidence if found.found else target.fence(action_id)
            return self.record_outcome(action_id, 'applied' if found.applied else 'rejected', evidence)
        except TargetUnreachable:
            return effect

    def receipt(self, receipt_id):
        receipt = self.db['receipts'].find_one({'_id': receipt_id})
        return ReceiptView(receipt, [], receipt['status'] == 'open')

    def chain(self, action_id):
        effect = self.db['effects'].find_one({'_id': action_id})
        return ChainView(action_id, effect, self._chain(effect['mandate_id']) if effect else [], None,
                         self.db['audit'].find({'action_id': action_id}))

    def undo(self, *args, **kwargs):
        raise NotImplementedError('undo is outside the finite proof model')


class FakeTarget:
    """SQLite-backed protocol fake with an independent, append-only apply log."""

    def __init__(self, name, path=':memory:'):
        self.name, self.path = name, str(path)
        self.connection = sqlite3.connect(self.path)
        self.connection.executescript('''
            CREATE TABLE IF NOT EXISTS outcomes (action_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, payload TEXT NOT NULL);
        ''')
        self.reachable = True
        self.lose_next = False

    def _require_reachable(self):
        if not self.reachable:
            raise TargetUnreachable('fake target unavailable')

    def _lookup(self, action_id):
        row = self.connection.execute('SELECT payload FROM outcomes WHERE action_id = ?', (action_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def send(self, action):
        self._require_reachable()
        stored = self._lookup(action.action_id)
        if stored is not None:
            same = stored.get('args_hash') == args_hash(action.args) and stored.get('tool') == action.tool and stored.get('target') == action.target
            return SendResult(stored['applied'] and same, stored['evidence'], 'ok' if same else 'fenced')
        evidence = dict(action_id=action.action_id, system=self.name, at_epoch=time.time())
        event = dict(evidence, event='applied', args=action.args, tool=str(action.tool), target=action.target)
        payload = dict(applied=True, evidence=evidence, args_hash=args_hash(action.args), tool=str(action.tool), target=action.target)
        with self.connection:
            self.connection.execute('INSERT INTO outcomes VALUES (?, ?)', (action.action_id, json.dumps(payload)))
            self.connection.execute('INSERT INTO events(payload) VALUES (?)', (json.dumps(event),))
        if self.lose_next:
            self.lose_next, self.reachable = False, False
            raise TargetTimeout('fake target applied; reply lost')
        return SendResult(True, evidence)

    def lookup(self, action_id):
        self._require_reachable()
        stored = self._lookup(action_id)
        return Lookup(bool(stored), bool(stored and stored['applied']), stored['evidence'] if stored else {})

    def fence(self, action_id):
        self._require_reachable()
        stored = self._lookup(action_id)
        if stored is not None:
            return stored['evidence']
        evidence = dict(action_id=action_id, system=self.name, at_epoch=time.time())
        with self.connection:
            self.connection.execute('INSERT INTO outcomes VALUES (?, ?)', (action_id, json.dumps(dict(applied=False, evidence=evidence))))
            self.connection.execute('INSERT INTO events(payload) VALUES (?)', (json.dumps(dict(evidence, event='fenced')),))
        return evidence

    def state(self):
        logs = [json.loads(row[0]) for row in self.connection.execute('SELECT payload FROM events ORDER BY id')]
        return {'system': self.name, 'reachable': self.reachable, 'log': logs}

    def inverse_of(self, action):
        return None

    def cut(self):
        self.lose_next = True

    def restore(self):
        self.reachable = True

    def restart(self):
        self.connection.close()
        self.connection = sqlite3.connect(self.path)
        self.restore()

    def close(self):
        self.connection.close()


class FakeSender:
    def __init__(self, gate, targets, sender_id='sender'):
        self.gate, self.targets, self.sender_id = gate, targets, sender_id
        self.claimed = set()

    def claim(self, action_id):
        result = self.gate.claim(action_id, self.sender_id)
        if result.claimed:
            self.claimed.add(action_id)
        return result

    def send(self, action_id):
        if action_id not in self.claimed:
            raise NotClaimed(action_id)
        effect = self.gate.db['effects'].find_one({'_id': action_id})
        try:
            result = self.targets[system_of(effect['target'])].send(action_for(effect))
        except (TargetTimeout, TargetUnreachable):
            return effect
        return self.gate.record_outcome(action_id, 'applied' if result.applied else 'rejected', result.evidence)

    def reconcile(self, action_id):
        effect = self.gate.db['effects'].find_one({'_id': action_id})
        return self.gate.reconcile(action_id, self.targets[system_of(effect['target'])])
