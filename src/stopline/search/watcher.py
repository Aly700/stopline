"""Warm audit embeddings at receipt insertion and closure."""
import logging
import threading

import pymongo

from .core import service_for
from .voyage import SearchError

RECONNECT_SECONDS = 2.0
log = logging.getLogger(__name__)


class ReceiptWatcher(threading.Thread):
    def __init__(self, db, search):
        super().__init__(name='audit-search-receipts', daemon=True)
        self.db = db
        self.search = search
        self.message = ''
        self._halt = threading.Event()
        self._seen = set()
        self._resume = None

    def stop(self):
        self._halt.set()

    def _notice(self, message):
        if self.message != message:
            self.message = message
            log.warning('%s', message)

    def process(self, event):
        """Consume each run transition once, even after a stream replay."""
        try:
            operation = event.get('operationType')
            receipt = event.get('fullDocument') or {}
            fields = (event.get('updateDescription') or {}).get('updatedFields', {})
            if operation == 'insert':
                phase = 'insert'
            elif (operation == 'replace' and receipt.get('status') == 'closed') or (
                    operation == 'update' and (fields.get('status') == 'closed' or fields.get('closed_at'))):
                phase = 'closed'
            else:
                return
            if not receipt:
                with pymongo.timeout(2):
                    receipt = self.db['receipts'].find_one(event.get('documentKey') or {}) or {}
            run_id = receipt.get('run_id')
            if not run_id or (run_id, phase) in self._seen:
                return
            self._seen.add((run_id, phase))
            # An already closed insertion contains both transitions' evidence.
            if operation == 'insert' and receipt.get('status') == 'closed':
                self._seen.add((run_id, 'closed'))
            self.search.index_run(run_id)
            self.message = ''
        except SearchError as error:
            self._notice(str(error))
        except Exception:
            self._notice('Audit indexing is unavailable; questions can try again.')

    def run(self):
        while not self._halt.is_set():
            try:
                options = dict(full_document='updateLookup', max_await_time_ms=500)
                if self._resume:
                    options['resume_after'] = self._resume
                with pymongo.timeout(3):
                    stream = self.db['receipts'].watch([
                        {'$match': {'operationType': {'$in': ['insert', 'update', 'replace']}}}
                    ], **options)
                with stream:
                    while not self._halt.is_set():
                        with pymongo.timeout(3):
                            event = stream.try_next()
                        if event is not None:
                            self.process(event)
                        self._resume = stream.resume_token or self._resume
            except Exception as error:
                if getattr(error, 'code', None) in (136, 260, 286):
                    self._resume = None
                self._notice('Receipt watching was interrupted; reconnecting.')
                self._halt.wait(RECONNECT_SECONDS)


def watch(db, *, search=None):
    """Start daemon workers without delaying startup; return a stoppable watcher."""
    try:
        service = search if search is not None else service_for(db)
        worker = ReceiptWatcher(db, service)
        worker.start()

        def provision():
            try:
                if not service.ensure_index():
                    worker._notice(service.index_message)
            except Exception:
                worker._notice('Atlas Vector Search is unavailable; using local ranking.')

        threading.Thread(target=provision, name='audit-search-index', daemon=True).start()
        return worker
    except Exception:
        log.warning('Receipt watching is unavailable; questions can still be asked.')
        return None
