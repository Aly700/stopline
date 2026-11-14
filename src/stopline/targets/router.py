"""Run with python -m stopline.targets.router --port 8766 --store /tmp/router.sqlite.

The test cut drops ordinary requests. Control endpoints stay reachable so the
same adapter can restore the service without restarting its process.
"""

import argparse
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import tempfile
import threading
from urllib.parse import quote, unquote

import httpx

from stopline.contract import (ROUTER_PORT, ROUTER_URL, Action, Inverse, Lookup, SendResult,
                               TargetTimeout, TargetUnreachable, Tool)
from .router_store import RouterStore


class RouterAdapter:
    name = 'router'

    def __init__(self, base_url: str = ROUTER_URL, timeout: float = 1.0):
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout

    def _call(self, path, payload=None, *, sending=False):
        try:
            with httpx.Client(timeout=self.timeout, trust_env=False) as client:
                response = client.request('GET' if payload is None else 'POST', self.base_url + path,
                                          json=payload)
                response.raise_for_status()
                return response.json()
        except httpx.ConnectError:
            raise TargetUnreachable('router refused the connection') from None
        except (httpx.HTTPError, ValueError):
            error = TargetTimeout if sending else TargetUnreachable
            raise error('router did not return a valid answer') from None

    def send(self, action: Action) -> SendResult:
        return SendResult(**self._call('/send', asdict(action), sending=True))

    def lookup(self, action_id: str) -> Lookup:
        return Lookup(**self._call('/lookup/' + quote(action_id, safe='')))

    def fence(self, action_id: str) -> dict:
        return self._call('/fence', {'action_id': action_id})

    def state(self) -> dict:
        try:
            return self._call('/state')
        except Exception:
            return {'system': self.name, 'reachable': False}

    def inverse_of(self, action: Action) -> Inverse | None:
        if action.tool != Tool.SHIFT or action.target != 'router/region-b':
            return None
        record = self.lookup(action.action_id)
        if not record.found or not record.applied:
            return None
        expect = {'weights': record.evidence['weights'], 'last_action_id': action.action_id}
        # Keep the condition in args too: send enforces it in the weight transaction.
        return Inverse(Tool.SHIFT, action.target,
                       {'percent': record.evidence['before']['region-b'], 'expect': expect}, expect)

    def cut(self) -> None:
        self._call('/cut', {})

    def restore(self) -> None:
        self._call('/restore', {})

    def reset(self) -> None:
        self._call('/reset', {})


class RouterServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, store):
        self.store = store
        self.lock = threading.Lock()
        self.mode = 'healthy'
        self.restored = threading.Event()
        super().__init__(address, RouterHandler)


class RouterHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _disconnect(self):
        self.close_connection = True
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def _handle(self):
        hang = False
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 <= size <= 65536:
                raise ValueError('request too large')
            data = json.loads(self.rfile.read(size)) if size else {}
            with self.server.lock:
                control = self.command == 'POST' and self.path in ('/cut', '/restore', '/reset')
                if self.server.mode == 'offline' and not control:
                    self._disconnect()
                    return
                store = self.server.store
                if control:
                    if self.path == '/cut':
                        if self.server.mode != 'offline':
                            self.server.mode = 'armed'
                            self.server.restored.clear()
                    else:
                        if self.path == '/reset':
                            store.reset()
                        self.server.mode = 'healthy'
                        self.server.restored.set()
                    answer = {'ok': True}
                elif self.command == 'POST' and self.path == '/send':
                    answer = asdict(store.send(Action(**data)))
                    if self.server.mode == 'armed':
                        self.server.mode = 'offline'
                        hang = True
                elif self.command == 'POST' and self.path == '/fence':
                    answer = store.fence(data['action_id'])
                elif self.command == 'GET' and self.path.startswith('/lookup/'):
                    answer = asdict(store.lookup(unquote(self.path[len('/lookup/'):])))
                elif self.command == 'GET' and self.path == '/state':
                    answer = store.state()
                else:
                    self.send_error(404)
                    return
            if hang:
                # The commit already happened. Leave time to kill the caller before its timeout.
                self.server.restored.wait(30)
                self._disconnect()
                return
            body = json.dumps(answer).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (KeyError, TypeError, ValueError):
            self.send_error(400, 'invalid request')
        except (BrokenPipeError, ConnectionResetError):
            pass


def main():
    parser = argparse.ArgumentParser(description='StopLine demo traffic router')
    parser.add_argument('--port', type=int, default=ROUTER_PORT)
    parser.add_argument('--store', default=str(Path(tempfile.gettempdir()) / 'stopline-router.sqlite'))
    args = parser.parse_args()
    with RouterServer(('127.0.0.1', args.port), RouterStore(args.store)) as server:
        try:
            server.serve_forever(poll_interval=0.1)
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
