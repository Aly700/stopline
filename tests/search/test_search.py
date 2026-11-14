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
    ('deny', {}, 'Refused: stopped by Operator at 14:28:26'),
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
    assert sentence_for(row).startswith('14:28:26 ')


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


def test_never_writes_reserved_database(voyage_server):
    db = DB()
    db.name = 'stopline'
    result = AuditSearch(db, voyage=voyage_server.client).ask('run-1', 'anything')
    assert result['ok'] is False
    assert not voyage_server.calls
    assert db['audit_search'].writes == 0
