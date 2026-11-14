"""The router's weights and action ledger commit in one SQLite transaction."""

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3

from stopline.contract import Action, Lookup, Reason, TARGET_REGION_B, Tool
from .common import evidence, fingerprint, result


class RouterStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        with self.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS router (id INTEGER PRIMARY KEY CHECK(id=1), '
                       'percent INTEGER NOT NULL, last_action_id TEXT)')
            db.execute('INSERT OR IGNORE INTO router VALUES (1, 0, NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS actions (log_id INTEGER PRIMARY KEY AUTOINCREMENT, '
                       'action_id TEXT NOT NULL UNIQUE, fingerprint TEXT, evidence TEXT NOT NULL)')

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _weights(percent):
        return {'region-a': 100 - percent, 'region-b': percent}

    @staticmethod
    def _record(db, action_id):
        return db.execute('SELECT * FROM actions WHERE action_id=?', (action_id,)).fetchone()

    @staticmethod
    def _insert(db, record, signature=None):
        cursor = db.execute('INSERT INTO actions (action_id, fingerprint, evidence) VALUES (?, ?, ?)',
                            (record['action_id'], signature, '{}'))
        record['log_id'] = cursor.lastrowid
        db.execute('UPDATE actions SET evidence=? WHERE log_id=?', (json.dumps(record), cursor.lastrowid))
        return record

    def send(self, action: Action):
        signature = fingerprint(action)
        with self.transaction() as db:
            prior = self._record(db, action.action_id)
            if prior:
                if prior['fingerprint'] is not None and prior['fingerprint'] != signature:
                    return result(evidence(action.action_id, 'router', False, Reason.SAME_ID_DIFFERENT_ARGS))
                return result(json.loads(prior['evidence']))
            current = db.execute('SELECT percent, last_action_id FROM router WHERE id=1').fetchone()
            before = self._weights(current['percent'])
            percent = action.args.get('percent')
            expect = action.args.get('expect')
            matches = expect is None or expect == before
            if isinstance(expect, dict) and 'weights' in expect:
                matches = expect['weights'] == before and (
                    'last_action_id' not in expect or expect['last_action_id'] == current['last_action_id'])
            reason = Reason.OK
            if action.tool != Tool.SHIFT:
                reason = Reason.TOOL_NOT_ALLOWED
            elif action.target != TARGET_REGION_B:
                reason = Reason.TARGET_NOT_ALLOWED
            elif type(percent) is not int or not 0 <= percent <= 100:
                reason = Reason.LIMIT_EXCEEDED
            elif not matches:
                reason = Reason.STATE_CHANGED
            if reason != Reason.OK:
                record = evidence(action.action_id, 'router', False, reason, fenced=True, weights=before)
            else:
                db.execute('UPDATE router SET percent=?, last_action_id=? WHERE id=1',
                           (percent, action.action_id))
                record = evidence(action.action_id, 'router', True, before=before,
                                  weights=self._weights(percent))
            return result(self._insert(db, record, signature))

    def lookup(self, action_id: str) -> Lookup:
        with self.transaction() as db:
            row = self._record(db, action_id)
            if row:
                record = json.loads(row['evidence'])
                return Lookup(True, record['applied'], record)
            return Lookup(False, False, evidence(action_id, 'router', False))

    def fence(self, action_id: str) -> dict:
        """If send won the race, return its applied evidence instead of claiming no effect."""
        with self.transaction() as db:
            row = self._record(db, action_id)
            if row:
                return json.loads(row['evidence'])
            return self._insert(db, evidence(action_id, 'router', False, Reason.FENCED, fenced=True))

    def state(self) -> dict:
        with self.transaction() as db:
            row = db.execute('SELECT * FROM router WHERE id=1').fetchone()
            return {'system': 'router', 'reachable': True, 'weights': self._weights(row['percent']),
                    'last_action_id': row['last_action_id'],
                    'log': [json.loads(item[0]) for item in db.execute('SELECT evidence FROM actions ORDER BY log_id')]}

    def reset(self) -> None:
        with self.transaction() as db:
            db.execute('UPDATE router SET percent=0, last_action_id=NULL WHERE id=1')
            db.execute('DELETE FROM actions')
            db.execute("DELETE FROM sqlite_sequence WHERE name='actions'")
