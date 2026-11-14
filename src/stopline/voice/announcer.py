"""A read-only change-stream observer. No writes to the demo database."""

from __future__ import annotations

import threading
import time

from pymongo import timeout

from .client import enabled, note
from .sentences import sentence_for_baseline, sentences_for_receipt


class Watcher:
    def __init__(self, db, *, speaker, run_filter=None, settle_seconds=1.0):
        self.db = db
        self.speaker = speaker
        self.run_filter = run_filter
        self.settle_seconds = settle_seconds
        self.ready = threading.Event()
        self._stop = threading.Event()
        self._seen = set()
        self._pending = {}
        self._resume_token = None
        self.thread = threading.Thread(target=self._run, name='stopline-receipts', daemon=True)

    def start(self):
        self.thread.start()
        return self

    def close(self):
        """Signal shutdown; never join a database thread on the caller's path."""
        self._stop.set()

    def _run(self):
        pipeline = [{'$match': {'ns.coll': {'$in': ['receipts', 'runs']},
                                'operationType': {'$in': ['insert', 'update', 'replace']}}}]
        reported = False
        while not self._stop.is_set() and enabled():
            try:
                options = dict(full_document='updateLookup', max_await_time_ms=100)
                if self._resume_token is not None:
                    options['resume_after'] = self._resume_token
                with timeout(6):
                    stream = self.db.watch(pipeline, **options)
                with stream:
                    self.ready.set()
                    while not self._stop.is_set() and enabled():
                        with timeout(6):
                            change = stream.try_next()
                        if change:
                            self._receive(change)
                        self._resume_token = stream.resume_token
                        self._flush()
                        reported = False
            except Exception:
                if not reported:
                    note('watch reconnecting')
                    reported = True
                self._stop.wait(.5)

    def _receive(self, change):
        doc = change.get('fullDocument')
        if not doc:
            return
        collection = change['ns']['coll']
        run_id = doc.get('run_id') if collection == 'receipts' else doc['_id']
        if self.run_filter is not None:
            if callable(self.run_filter):
                if not self.run_filter(run_id):
                    return
            elif run_id != self.run_filter:
                return
        if collection == 'receipts':
            if change['operationType'] == 'insert':
                self._schedule('stop', doc['_id'], self.settle_seconds)
            elif doc['status'] == 'closed':
                # A very fast close must follow the pending stop announcement.
                opening = self._pending.get(('stop', doc['_id']), 0)
                delay = max(0, opening - time.monotonic())
                self._schedule('closed', doc['_id'], delay)
        elif collection == 'runs' and doc.get('mode') in {'flag', 'fixed'} and doc.get('stop_flag'):
            self._schedule('baseline', doc['_id'], self.settle_seconds)

    def _schedule(self, moment, document_id, delay):
        key = (moment, document_id)
        if key not in self._seen and key not in self._pending:
            self._pending[key] = time.monotonic() + delay

    def _flush(self):
        for key, due in sorted(list(self._pending.items()), key=lambda item: item[1]):
            if due > time.monotonic():
                continue
            moment, document_id = key
            # A failed read leaves the pending event intact for the reconnect.
            with timeout(6):
                sentences = self._sentences(moment, document_id)
            self._seen.add(key)
            del self._pending[key]
            try:
                for text in sentences:
                    if self._stop.is_set() or not enabled():
                        return
                    self.speaker(text)
            except Exception:
                note('announcement failed')

    def _sentences(self, moment, document_id):
        if moment == 'baseline':
            run = self.db.runs.find_one({'_id': document_id})
            if not run:
                return []
            stop_at = run.get('stop_at_local')
            if stop_at is None:
                return ['Stop pressed. The outcome is unknown.']
            effects = self.db.effects.find({'run_id': document_id, 'state': 'applied'})
            count = sum(1 for effect in effects
                        if (effect.get('evidence') or {}).get('at_epoch', float('-inf')) > stop_at)
            return [sentence_for_baseline(count)]

        receipt = self.db.receipts.find_one({'_id': document_id})
        if not receipt:
            return []
        effects = list(self.db.effects.find({'_id': {'$in': receipt['covered']}}))
        refusals = list(self.db.audit.find({
            'run_id': receipt['run_id'], 'kind': 'deny',
            'mandate_id': {'$in': receipt['revoked']}, 'at': {'$gte': receipt['stopped_at']},
        }))
        return sentences_for_receipt(receipt, effects, refusals, closing=moment == 'closed')
