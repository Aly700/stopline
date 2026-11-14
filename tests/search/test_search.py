import copy
import json
import logging
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from stopline.contract import AuditKind, Reason
from stopline.search.sentences import clock
from stopline.search import AuditSearch, search_routes, sentence_for
from stopline.search.voyage import DIMENSIONS, MODEL, VoyageClient

AT = datetime(2026, 11, 14, 14, 28, 26, tzinfo=timezone.utc)
SECRET = 'private-test-credential'


def entry(kind='deny', **changes):
    row = dict(_id='audit-1', run_id='run-1', kind=kind, principal='agent:worker-2',
               mandate_id='worker', root_id='root', action_id='action-1', policy_version=1,
               policy_hash='a' * 64, reason='revoked' if kind == 'deny' else 'ok', seq=5, at=AT,
               detail=dict(tool='shift_traffic', target='router/region-b', args={'percent': 50},
                           revoked_by='person:operator', revoked_at=AT))
    row.update(changes)
    return row


class Cursor(list):
    def sort(self, key, direction=1):
        keys = key if isinstance(key, list) else [(key, direction)]
        for field, order in reversed(keys):
            super().sort(key=lambda row: row.get(field, ''), reverse=order < 0)
        return self

    def limit(self, n):
        return Cursor(self[:n])


def matches(row, query):
    return all(row.get(k) in v['$in'] if isinstance(v, dict) and '$in' in v
               else row.get(k) == v for k, v in query.items())


class Collection:
    def __init__(self, rows=()):
        self.rows = copy.deepcopy(list(rows))
        self.writes = 0

    def find(self, query=None, projection=None):
        return Cursor(copy.deepcopy([r for r in self.rows if matches(r, query or {})]))

    def find_one(self, query=None, sort=None):
        rows = self.find(query)
        if sort:
            rows.sort(sort)
        return rows[0] if rows else None

    def update_one(self, query, update, upsert=False):
        self.writes += 1
        found = self.find_one(query)
        if found:
            return SimpleNamespace(upserted_id=None)
        row = copy.deepcopy(update.get('$setOnInsert', update.get('$set', {})))
        self.rows.append(row)
        return SimpleNamespace(upserted_id=row['_id'])

    def aggregate(self, pipeline, **kwargs):
        raise RuntimeError('index unavailable ' + SECRET)


class DB:
    name = 'stopline_test_search_fake'

    def __init__(self, entries=None):
        self.collections = {name: Collection() for name in
                            ['audit', 'audit_search', 'effects', 'mandates', 'runs', 'receipts']}
        self.collections['audit'] = Collection(entries if entries is not None else [entry()])
        self.collections['runs'] = Collection([{'_id': 'run-1', 'created_at': AT}])

    def __getitem__(self, name):
        return self.collections[name]


@pytest.fixture
def voyage_server(monkeypatch):
    monkeypatch.setenv('VOYAGE_API_KEY', SECRET)
    state = SimpleNamespace(calls=[], status=200, delay=0, malformed=False)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            state.calls.append(body)
            time.sleep(state.delay)
            vectors = []
            for i, text in enumerate(body['input']):
                deny = 'Refused' in text or 'refused' in text
                vector = [1.0, 0.0] if deny else [0.0, 1.0]
                vectors.append({'index': i, 'embedding': vector + [0.0] * (DIMENSIONS - 2)})
            payload = {'data': list(reversed(vectors))} if state.status == 200 else {'error': SECRET}
            if state.malformed:
                payload = {'data': [{'index': 0, 'embedding': [float('nan')]}]}
            self.send_response(state.status)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            try:
                self.wfile.write(json.dumps(payload).encode())
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.client = VoyageClient(endpoint=f'http://127.0.0.1:{server.server_port}/v1/embeddings')
    yield state
    server.shutdown()
    server.server_close()
    thread.join(timeout=1)


@pytest.mark.parametrize('kind,detail,phrase', [
    ('allow', {'chain': [{'holder': 'person:operator'}, {'holder': 'agent:repair'},
                         {'holder': 'agent:worker-2'}]}, 'was allowed to shift 50% to region B under Operator → Repair agent → Worker two'),
    ('deny', {}, 'Refused: stopped by Operator at '),
    ('hand_down', {'holder': 'agent:worker-1'}, 'handed permission to Worker one'),
    ('stop', {'holder': 'agent:repair', 'covered': ['a', 'b', 'c']}, "stopped the Repair agent's permission; 3 actions on the receipt"),
    ('claim', {}, 'claimed'),
    ('outcome', {'state': 'applied'}, 'completed'),
    ('undo', {'reverses': 'earlier'}, 'requested undo'),
])
def test_sentences_cover_each_kind(kind, detail, phrase):
    row = entry(kind)
    row['detail'].update(detail)
    assert phrase in sentence_for(row)
    assert sentence_for(row).startswith(clock(row['at']) + ' ')


def test_refusal_uses_attempted_arguments_over_effect():
    effect = dict(tool='shift_traffic', target='router/region-b', args={'percent': 25})
    assert 'shift 50%' in sentence_for(entry(), effect)


def test_every_reason_has_plain_words():
    for reason in Reason:
        row = entry(reason=reason, detail={})
        text = sentence_for(row)
        assert '_' not in text


def test_batch_and_local_retrieval(voyage_server):
    db = DB([entry(), entry('allow', _id='audit-2')])
    service = AuditSearch(db, voyage=voyage_server.client)
    assert service.index_run('run-1') == 2
    assert service.index_run('run-1') == 0
    assert len(voyage_server.calls) == 1
    request = voyage_server.calls[0]
    assert request['input_type'] == 'document'
    assert request['model'] == MODEL
    assert request['output_dimension'] == DIMENSIONS
    assert len(request['input']) == 2
    result = service.ask('run-1', 'what was refused after the stop')
    assert result['ok'] and result['engine'] == 'local ranking'
    assert result['rows'][0]['kind'] == 'deny'
    assert set(result['rows'][0]) == {'audit_id', 'action_id', 'kind', 'at', 'text', 'score'}
    assert len(voyage_server.calls) == 2
    assert voyage_server.calls[-1]['input_type'] == 'query'
    assert db['audit'].writes == 0


@pytest.mark.parametrize('failure,expected', [
    ('rate', 'Voyage is rate limited; try again in 20 seconds'),
    ('timeout', 'Voyage took too long; try again.'),
    ('missing', 'Voyage key is missing; set VOYAGE_API_KEY or ~/.voyage_key.'),
    ('network', 'Voyage is unavailable; try again.'),
    ('malformed', 'Voyage returned invalid embeddings; try again.'),
])
def test_failures_are_plain(voyage_server, monkeypatch, tmp_path, caplog, failure, expected):
    caplog.set_level(logging.DEBUG)
    if failure == 'rate':
        voyage_server.status = 429
    elif failure == 'timeout':
        voyage_server.delay = 0.2
        voyage_server.client.timeout = 0.04
    elif failure == 'missing':
        monkeypatch.delenv('VOYAGE_API_KEY')
        monkeypatch.setattr('stopline.search.voyage.KEY_FILE', tmp_path / 'absent')
    elif failure == 'network':
        voyage_server.client.endpoint = 'http://127.0.0.1:1/v1/embeddings'
    else:
        voyage_server.malformed = True
    started = time.monotonic()
    result = AuditSearch(DB(), voyage=voyage_server.client).ask('run-1', 'what was refused')
    assert time.monotonic() - started < 12
    assert result == dict(ok=False, engine='local ranking', rows=[], message=expected)
    assert len(voyage_server.calls) <= 1
    assert SECRET not in caplog.text + repr(result)


def test_route_defaults_to_latest_run_and_always_200(voyage_server):
    db = DB()
    app = FastAPI()
    app.include_router(search_routes(db, search=AuditSearch(db, voyage=voyage_server.client)))
    with TestClient(app) as client:
        response = client.get('/api/ask', params={'q': 'what was refused'})
        assert response.status_code == 200
        assert response.json()['rows'][0]['audit_id'] == 'audit-1'
        assert client.get('/api/ask').status_code == 200
        assert client.get('/api/ask').json()['ok'] is False



class AtlasCollection(Collection):
    def __init__(self, rows=()):
        super().__init__(rows)
        self.indexes = []
        self.creates = []
        self.pipelines = []
        self.empty_queries = 0
        self.stale_queries = 0
        self.ready = True

    def create_index(self, key):
        return key

    def list_search_indexes(self, name=None):
        return copy.deepcopy(self.indexes)

    def create_search_index(self, model):
        self.creates.append(model.document)
        self.indexes = [dict(name=model.document['name'], queryable=self.ready,
                             latestDefinition=model.document['definition'])]
        return model.document['name']

    def aggregate(self, pipeline, **kwargs):
        self.pipelines.append(pipeline)
        if self.empty_queries:
            self.empty_queries -= 1
            return []
        rows = self.find(pipeline[0]['$vectorSearch']['filter'])
        if self.stale_queries:
            self.stale_queries -= 1
            rows = rows[:1]
        return [dict(row, score=0.9 if row['kind'] == 'deny' else 0.7) for row in rows]


def test_atlas_index_and_filter(voyage_server):
    db = DB()
    atlas = db.collections['audit_search'] = AtlasCollection()
    service = AuditSearch(db, voyage=voyage_server.client)
    assert service.ensure_index(wait_seconds=0.1)
    assert service.ensure_index(wait_seconds=0.1)
    assert len(atlas.creates) == 1
    definition = atlas.creates[0]
    assert definition['type'] == 'vectorSearch'
    assert definition['definition']['fields'] == [
        dict(type='vector', path='embedding', numDimensions=DIMENSIONS, similarity='cosine'),
        dict(type='filter', path='run_id'),
    ]
    result = service.ask('run-1', 'what was refused')
    assert result['ok'] and result['engine'] == 'atlas vector search'
    stage = atlas.pipelines[0][0]['$vectorSearch']
    assert stage['filter'] == {'run_id': 'run-1'}
    assert stage['path'] == 'embedding'
    assert stage['numCandidates'] >= 20 * stage['limit']
    assert len(stage['queryVector']) == DIMENSIONS


def test_atlas_waits_for_new_entries_not_just_any_result(voyage_server):
    db = DB([entry('allow', _id='old'), entry(_id='new')])
    atlas = db.collections['audit_search'] = AtlasCollection()
    atlas.stale_queries = 2
    service = AuditSearch(db, voyage=voyage_server.client)
    result = service.ask('run-1', 'what was refused')
    assert result['engine'] == 'atlas vector search'
    assert len(atlas.pipelines) == 3
    assert result['rows'][0]['audit_id'] == 'new'
    assert len(voyage_server.calls) == 2


def test_empty_atlas_is_bounded_and_falls_back(voyage_server, monkeypatch):
    db = DB()
    atlas = db.collections['audit_search'] = AtlasCollection()
    atlas.empty_queries = 1000
    monkeypatch.setattr('stopline.search.core.INGEST_SECONDS', 0.08)
    service = AuditSearch(db, voyage=voyage_server.client)
    started = time.monotonic()
    result = service.ask('run-1', 'what was refused')
    assert result['ok'] and result['engine'] == 'local ranking'
    assert 1 < len(atlas.pipelines) < 10
    assert time.monotonic() - started < 12


def test_index_creation_timeout_does_not_raise(voyage_server):
    db = DB()
    atlas = db.collections['audit_search'] = AtlasCollection()
    atlas.ready = False
    service = AuditSearch(db, voyage=voyage_server.client)
    started = time.monotonic()
    assert service.ensure_index(wait_seconds=0.05) is False
    assert time.monotonic() - started < 1
    assert service.index_message


def test_local_results_never_cross_runs(voyage_server):
    db = DB([entry(), entry(_id='other', run_id='run-2')])
    service = AuditSearch(db, voyage=voyage_server.client)
    service.index_run('run-2')
    result = service.ask('run-1', 'what was refused')
    assert [row['audit_id'] for row in result['rows']] == ['audit-1']


def test_key_file_and_environment_precedence(voyage_server, tmp_path, monkeypatch):
    from stopline.search.voyage import load_key
    path = tmp_path / 'credential'
    path.write_text(' file-value\n')
    monkeypatch.setattr('stopline.search.voyage.KEY_FILE', path)
    assert load_key() == SECRET
    monkeypatch.delenv('VOYAGE_API_KEY')
    assert load_key() == 'file-value'


def test_watch_reconnects_and_batches_only_receipt_transitions(monkeypatch):
    from stopline.search import watch
    db = DB()
    events = [
        {'_id': {'token': 1}, 'operationType': 'insert',
         'fullDocument': {'_id': 'receipt', 'run_id': 'run-1', 'status': 'open'}},
        {'_id': {'token': 2}, 'operationType': 'update',
         'fullDocument': {'_id': 'receipt', 'run_id': 'run-1', 'status': 'closed'},
         'updateDescription': {'updatedFields': {'status': 'closed'}}},
    ]

    class Stream:
        def __init__(self, rows):
            self.rows = iter(rows)
            self.resume_token = None

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def try_next(self):
            event = next(self.rows, None)
            if isinstance(event, Exception):
                raise event
            if event:
                self.resume_token = event['_id']
            else:
                time.sleep(0.005)
            return event

    calls = []
    opens = []

    def open_stream(*args, **kwargs):
        opens.append(kwargs)
        return Stream([events[0], RuntimeError(SECRET)] if len(opens) == 1 else events)

    db['receipts'].watch = open_stream
    service = SimpleNamespace(index_run=lambda run_id: calls.append(run_id), ensure_index=lambda: True)
    monkeypatch.setattr('stopline.search.watcher.RECONNECT_SECONDS', 0.01)
    worker = watch(db, search=service)
    try:
        deadline = time.monotonic() + 2
        while len(calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert calls == ['run-1', 'run-1']
        assert opens[1]['resume_after'] == {'token': 1}
        assert SECRET not in worker.message
    finally:
        worker.stop()
        worker.join(timeout=2)
    assert not worker.is_alive()


def test_watch_failure_never_escapes_or_retries_the_same_receipt(monkeypatch, caplog):
    from stopline.search.watcher import ReceiptWatcher
    db = DB()
    calls = []

    def failing(run):
        calls.append(run)
        raise RuntimeError(SECRET)

    worker = ReceiptWatcher(db, SimpleNamespace(index_run=failing))
    event = dict(operationType='insert', fullDocument=dict(run_id='run-1', status='open'))
    worker.process(event)
    worker.process(event)
    assert calls == ['run-1']
    assert SECRET not in worker.message + caplog.text


def test_cli_prints_rows_and_uses_database_factory(voyage_server, monkeypatch, capsys):
    from stopline.search import __main__ as command
    db = DB()
    service = AuditSearch(db, voyage=voyage_server.client)
    selected = []

    def database(name):
        selected.append(name)
        return db

    monkeypatch.setattr(command.storage, 'database', database)
    monkeypatch.setattr(command, 'AuditSearch', lambda chosen: service)
    assert command.main(['what was refused', '--run', 'run-1', '--database', 'example']) == 0
    output = capsys.readouterr().out
    assert selected == ['example']
    assert 'Search: local ranking' in output
    assert 'Worker two asked to shift 50% to region B' in output
    assert '[audit audit-1]' in output


def test_cli_sanitizes_database_failure(monkeypatch, capsys):
    from stopline.search import __main__ as command

    def broken(name):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(command.storage, 'database', broken)
    assert command.main(['anything']) == 0
    assert capsys.readouterr().out == 'Audit search is unavailable; check the database connection.\n'


def test_query_failure_uses_one_request_after_indexing(voyage_server):
    service = AuditSearch(DB(), voyage=voyage_server.client)
    service.index_run('run-1')
    voyage_server.status = 429
    result = service.ask('run-1', 'what was refused')
    assert not result['ok']
    assert result['message'] == 'Voyage is rate limited; try again in 20 seconds'
    assert [call['input_type'] for call in voyage_server.calls] == ['document', 'query']


def test_real_default_timeout_returns_within_twelve_seconds(voyage_server):
    voyage_server.delay = 11
    service = AuditSearch(DB(), voyage=voyage_server.client)
    started = time.monotonic()
    result = service.ask('run-1', 'what was refused')
    elapsed = time.monotonic() - started
    assert not result['ok']
    assert result['message'] == 'Voyage took too long; try again.'
    assert 9 <= elapsed < 12
    assert len(voyage_server.calls) == 1


def test_route_contains_unexpected_errors(monkeypatch):
    def broken(*args):
        raise RuntimeError(SECRET)

    app = FastAPI()
    app.include_router(search_routes(DB(), search=SimpleNamespace(ask=broken)))
    with TestClient(app) as client:
        response = client.get('/api/ask', params={'q': 'anything'})
    assert response.status_code == 200
    assert response.json() == dict(ok=False, engine='local ranking', rows=[],
                                   message='Audit search is unavailable; try again.')


def test_concurrent_indexing_uses_one_document_batch(voyage_server):
    from concurrent.futures import ThreadPoolExecutor
    voyage_server.delay = 0.05
    service = AuditSearch(DB(), voyage=voyage_server.client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(service.index_run, ['run-1', 'run-1']))
    assert sorted(results) == [0, 1]
    assert len(voyage_server.calls) == 1
