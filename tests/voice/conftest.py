import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@pytest.fixture
def speech_server():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            self.server.requests.append((self.path, dict(self.headers), body))
            time.sleep(self.server.delay)
            self.send_response(self.server.status)
            self.send_header('Content-Type', self.server.content_type)
            self.end_headers()
            try:
                self.wfile.write(self.server.audio)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.requests = []
    server.status = 200
    server.delay = 0
    server.content_type = 'audio/mpeg'
    server.audio = b'ID3' + b'\x00' * 8000
    server.url = f'http://127.0.0.1:{server.server_port}'
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(2)


@pytest.fixture
def receipt_records():
    from datetime import datetime, timedelta, timezone

    stopped = datetime(2026, 11, 14, 19, tzinfo=timezone.utc)
    receipt = dict(_id='receipt-1', run_id='r1', root_id='root', mandate_id='repair',
                   stopped_by='person:operator', stop_seq=10, stopped_at=stopped,
                   revoked=['repair', 'w1', 'w2'], covered=['rollback', 'shift', 'restart'],
                   status='open', closed_at=None)
    effects = [
        dict(_id='rollback', run_id='r1', state='applied', tool='rollback', target='cluster/checkout-api'),
        dict(_id='shift', run_id='r1', state='sent', tool='shift_traffic', target='router/region-b'),
        dict(_id='restart', run_id='r1', state='cancelled', tool='restart', target='cluster/checkout-worker'),
    ]
    refusals = [dict(_id='deny-1', run_id='r1', action_id='shift-2', kind='deny',
                     mandate_id='w2', at=stopped + timedelta(seconds=.2), seq=10, reason='revoked')]
    return receipt, effects, refusals
