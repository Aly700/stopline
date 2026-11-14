"""Optional retrieval over a separate collection of audit embeddings."""
import math
import threading
import time

import pymongo

from .sentences import sentence_for
from .voyage import SearchError, VoyageClient

COLLECTION = 'audit_search'
ASK_SECONDS = 11.0


def failure(message):
    return dict(ok=False, engine='local ranking', rows=[], message=message)


def remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise SearchError('Search took too long; try again.')
    return value


def cosine(left, right):
    if len(left) != len(right):
        return None
    try:
        norm = math.sqrt(sum(x * x for x in left) * sum(x * x for x in right))
        value = sum(x * y for x, y in zip(left, right)) / norm if norm else None
        return value if value is not None and math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def result_row(document, score):
    at = document['at']
    return dict(audit_id=str(document['_id']), action_id=document.get('action_id'),
                kind=document['kind'], at=at.isoformat() if hasattr(at, 'isoformat') else at,
                text=document['text'], score=score)


class AuditSearch:
    def __init__(self, db, *, voyage=None):
        self.db = db
        self.collection = db[COLLECTION]
        self.voyage = voyage or VoyageClient()
        self.model = self.voyage.model
        self.dimensions = self.voyage.dimensions
        self._index_lock = threading.Lock()

    def _writable(self):
        if self.db.name == 'stopline':
            raise SearchError('Search indexing is disabled for the reserved database.')

    def index_run(self, run_id, *, deadline=None):
        """Embed missing events in one request; failures have fixed public messages."""
        deadline = deadline if deadline is not None else time.monotonic() + ASK_SECONDS
        if not self._index_lock.acquire(timeout=remaining(deadline)):
            raise SearchError('Search is busy; try again.')
        try:
            with pymongo.timeout(remaining(deadline)):
                stored = {row['_id'] for row in self.collection.find({'run_id': run_id})}
                entries = [row for row in self.db['audit'].find({'run_id': run_id}) if row['_id'] not in stored]
                if not entries:
                    return 0
                self._writable()
                effects = {row['_id']: row for row in self.db['effects'].find({'run_id': run_id})}
                mandates = {row['_id']: row for row in self.db['mandates'].find({'run_id': run_id})}
                texts = []
                for entry in entries:
                    detail = dict(entry.get('detail') or {})
                    effect = effects.get(entry.get('action_id'))
                    ids = detail.get('chain') or (effect or {}).get('chain') or []
                    detail['chain'] = [mandates[mid] for mid in ids if mid in mandates]
                    if entry['kind'] == 'stop':
                        detail['holder'] = mandates.get(entry.get('mandate_id'), {}).get('holder')
                    texts.append(sentence_for({**entry, 'detail': detail}, effect))
                vectors = self.voyage.embed(texts, 'document', timeout=remaining(deadline))
                added = 0
                for entry, text, vector in zip(entries, texts, vectors):
                    remaining(deadline)
                    document = {key: entry.get(key) for key in ('_id', 'run_id', 'action_id', 'kind', 'at')}
                    document.update(text=text, embedding=vector, model=self.model)
                    result = self.collection.update_one({'_id': entry['_id']}, {'$setOnInsert': document}, upsert=True)
                    added += result.upserted_id is not None
                return added
        except SearchError:
            raise
        except Exception:
            raise SearchError('Audit search storage is unavailable; try again.') from None
        finally:
            self._index_lock.release()

    def _rank(self, run_id, vector, k, added, deadline):
        rows = []
        with pymongo.timeout(remaining(deadline)):
            for document in self.collection.find({'run_id': run_id, 'model': self.model}):
                remaining(deadline)
                score = cosine(vector, document.get('embedding', []))
                if score is not None:
                    rows.append(result_row(document, (1 + score) / 2))
        rows.sort(key=lambda row: (-row['score'], row['audit_id']))
        if not rows:
            return failure('No searchable audit entries are stored for this run.')
        return dict(ok=True, engine='local ranking', rows=rows[:k],
                    message='Atlas Vector Search is unavailable; using local ranking.')

    def ask(self, run_id, question, k=5):
        """Return only stored entries, with bounded I/O and no escaping failures."""
        deadline = time.monotonic() + ASK_SECONDS
        try:
            if not isinstance(question, str) or not question.strip():
                return failure('Type a question about the audit.')
            if len(question) > 4000:
                return failure('Keep the question under 4000 characters.')
            if type(k) is not int or not 1 <= k <= 100:
                return failure('Choose between 1 and 100 results.')
            with pymongo.timeout(remaining(deadline)):
                if not run_id:
                    latest = self.db['runs'].find_one({}, sort=[('created_at', -1), ('_id', -1)])
                    if not latest:
                        return failure('No run is available to search.')
                    run_id = latest['_id']
                added = self.index_run(run_id, deadline=deadline)
                if self.collection.find_one({'run_id': run_id, 'model': self.model}) is None:
                    return failure('No searchable audit entries are stored for this run.')
                vector = self.voyage.embed([question.strip()], 'query', timeout=remaining(deadline))[0]
                return self._rank(run_id, vector, k, added, deadline)
        except SearchError as error:
            return failure(str(error))
        except Exception:
            return failure('Audit search is unavailable; try again.')


_services = {}
_services_lock = threading.Lock()


def service_for(db):
    # Share document batching between the HTTP endpoint and receipt watcher.
    key = (id(getattr(db, 'client', db)), db.name)
    with _services_lock:
        if key not in _services:
            _services[key] = AuditSearch(db)
        return _services[key]
