"""Real component execution on Atlas and an owned router on port 8796.

Eight independent schedules may overlap, each with its own root and mock
cluster. Router cut/restart cases run alone. This changes throughput, not the
bounded two-worker interleaving within a schedule. Gate setup runs once.
Observer records buffer locally, then are persisted and reread before counting;
all authority/effect records are written by the unmodified real gate.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import json
import os
from pathlib import Path
import socket
import signal
import sqlite3
import subprocess
import sys
import time
import threading
from unittest.mock import patch
from uuid import uuid4

from pymongo.errors import OperationFailure

from stopline.proof.backend import ObservedTarget, Runtime
from stopline.proof.store import MemoryDatabase

PORT = 8796
URL = f'http://127.0.0.1:{PORT}'


class RouterProcess:
    def __init__(self, directory):
        self.store = Path(directory) / 'router.sqlite'
        self.process = None

    def start(self):
        # Refuse an occupied port; never reuse or terminate someone else's router.
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(('127.0.0.1', PORT))
        environment = dict(os.environ, STOPLINE_ROUTER_PORT=str(PORT))
        self.process = subprocess.Popen(
            [sys.executable, '-m', 'stopline.targets.router', '--port', str(PORT), '--store', str(self.store)],
            env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError('owned router exited during startup')
            with socket.socket() as probe:
                probe.settimeout(0.1)
                if probe.connect_ex(('127.0.0.1', PORT)) == 0:
                    return
            time.sleep(0.02)
        raise TimeoutError('owned router startup timeout')

    def stop(self):
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
            self.process = None

    def restart(self):
        self.stop()
        self.start()

    def logs(self):
        # Read the target's own durable ledger even while its HTTP reply path is cut.
        with sqlite3.connect(f'file:{self.store}?mode=ro', uri=True, timeout=2) as db:
            return [json.loads(row[0]) for row in db.execute('SELECT evidence FROM actions ORDER BY log_id')]


class BufferedDatabase:
    """Buffer observer/run records; authority collections always go to Atlas."""
    def __init__(self, db):
        self.real = db
        self.local = MemoryDatabase()
        self.name = db.name

    def __getitem__(self, name):
        return self.local[name] if name == 'runs' or name.startswith('proof_') else self.real[name]


class BeforeRetryRead:
    """Intercept only this gate's next effects read, before a retry snapshot exists."""
    def __init__(self, collection, hook):
        self.collection, self.hook = collection, hook

    def find_one(self, *args, **kwargs):
        self.hook()
        return self.collection.find_one(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.collection, name)


class RetryDatabase:
    def __init__(self, db, hook):
        self.original = db
        self.effects = BeforeRetryRead(db.effects, hook)

    def __getattr__(self, name):
        return getattr(self.original, name)


@dataclass
class RealRuntime(Runtime):
    router_process: RouterProcess = None
    setup_done: bool = True

    def logs(self, target):
        rows = self.router_process.logs() if target.name == 'router' else target.target.state()['log']
        return [dict(row, event='applied' if row['applied'] else 'fenced') for row in rows]

    def expire(self, mandate_id=None):
        if mandate_id:
            self.gate.db.mandates.update_one({'_id': mandate_id}, [{'$set': {'expires_at': '$$NOW'}}])

    def force_retry(self, callback):
        """Inject one transient abort; real with_transaction retries after a real stop.

        No target or model call occurs inside the transaction. The stop uses its
        own session before the retry's first read, after the first attempt aborts.
        The gate's returned attempts value and deny audit are stored by Driver.
        """
        gate, original_db, context = self.gate, self.gate.db, self.gate._context
        phase = [0]

        def abort_once(session, mandate_id):
            answer = context(session, mandate_id)
            if phase[0] == 0:
                phase[0] = 1
                raise OperationFailure('forced proof transaction retry', code=112,
                                       details={'errorLabels': ['TransientTransactionError']})
            return answer

        def before_read():
            if phase[0] == 1:
                phase[0] = 2
                gate.db, gate._context = original_db, context
                callback()

        gate._context = abort_once
        gate.db = RetryDatabase(original_db, before_read)

    def restart_router(self):
        self.router_process.restart()
        self.targets['router'].fault_active = False

    def paused_send(self, driver):
        """Pause the owned process, observe real request bytes, stop, then resume.

        Socket send completion, not entry to an adapter, establishes dispatch.
        The target's SQLite application timestamp establishes later arrival.
        A timed-out reply is reconciled through the real sender after resume.
        """
        from stopline.contract import WORKER_TWO
        action_id = driver.action(WORKER_TWO, 0).action_id
        sent = threading.Event()
        worker_id = [None]
        outgoing = bytearray()
        original = socket.socket.send

        def observe_send(sock, data, *args, **kwargs):
            count = original(sock, data, *args, **kwargs)
            if threading.get_ident() == worker_id[0] and not sent.is_set():
                try:
                    router_socket = sock.getpeername()[1] == PORT
                except OSError:
                    router_socket = False
                if router_socket:
                    outgoing.extend(bytes(data[:count]))
                    headers, separator, body = outgoing.partition(b'\r\n\r\n')
                    if separator and b'content-length:' in headers.lower():
                        length = int(headers.lower().split(b'content-length:', 1)[1].split(b'\r\n', 1)[0])
                        if len(body) >= length:
                            driver.db['proof_sends'].update_one({'action_id': action_id}, {'$set': {
                                'at_epoch': time.time(), 'dispatch_evidence': 'socket request bytes sent',
                            }})
                            sent.set()
            return count

        def deliver():
            worker_id[0] = threading.get_ident()
            driver.step(WORKER_TWO, 0)

        self.router_process.process.send_signal(signal.SIGSTOP)
        try:
            with patch.object(socket.socket, 'send', observe_send), ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(deliver)
                try:
                    if not sent.wait(30):
                        raise TimeoutError('request bytes were not sent to the paused router')
                    driver.stop()
                finally:
                    self.router_process.process.send_signal(signal.SIGCONT)
                pending.result(timeout=15)
        finally:
            self.router_process.process.send_signal(signal.SIGCONT)
        deadline = time.monotonic() + 5
        while not any(row['action_id'] == action_id for row in self.router_process.logs()):
            if time.monotonic() >= deadline:
                raise TimeoutError('dispatched request did not reach the resumed target')
            time.sleep(0.02)
        driver.reconcile(action_id)


def real_runtime(db, router_process):
    from stopline.gate import build_gate
    from stopline.targets import build_sender, build_targets

    gate = build_gate(db)
    raw = build_targets('mock', router_url=URL)
    buffered = BufferedDatabase(db)
    targets = {name: ObservedTarget(target, buffered) for name, target in raw.items()}
    return RealRuntime(buffered, gate, targets, build_sender(gate, targets), 'real', router_process)


def run_real_suite(*, work_dir, scratch_db, router_url=URL):
    from stopline.contract import ROUTER_PORT
    from stopline.gate import build_gate
    from stopline.targets import build_targets, reset_all
    from stopline.proof.metrics import DEFINITIONS
    from stopline.proof.runner import Driver, finalize, utcnow
    from stopline.proof.schedules import BOUNDS, schedules
    from dataclasses import asdict

    if scratch_db is None or not scratch_db.name.startswith('stopline_test_proof_'):
        raise ValueError('real execution requires a proof scratch database')
    if ROUTER_PORT != PORT or router_url != URL:
        raise ValueError('set STOPLINE_ROUTER_PORT=8796 before importing stopline')
    if os.environ.get('STOPLINE_TARGETS_URL'):
        raise ValueError('unset STOPLINE_TARGETS_URL; proof owns its mock clusters')
    started = time.monotonic()
    directory = Path(work_dir)
    directory.mkdir(parents=True, exist_ok=True)
    result = dict(_id=uuid4().hex, created_at=utcnow(), backend='real', producer='real', schema_version=2,
                  bounds=dict(BOUNDS, concurrent_schedules=8), definitions=DEFINITIONS,
                  sources={'gate': 'real', 'sender': 'real', 'cluster': 'real mock-cluster adapter',
                           'stop': 'gate.stop then sender.sweep before hold release; measurement marker remains at gate commit',
                           'router': 'real HTTP process and target-owned SQLite ledger on port 8796',
                           'workers': 'deterministic protocol driver, not production worker processes',
                           'retry': 'one injected transient abort retried by PyMongo; real stop between attempts',
                           'expiry': 'stored mandate deadline set to Atlas $$NOW before next admission',
                           'paused_send': 'owned router SIGSTOP; real socket request bytes precede stop; SIGCONT then target application',
                           'baseline_counterparts': 'claim/delegation/retry/receipt checkpoints become pre-dispatch holds; no baseline authority feature is simulated',
                           'record_storage': 'real Atlas authority records plus persisted observer snapshots'},
                  schedule_count=len(schedules()), rows=[])
    controller = RouterProcess(directory)
    try:
        controller.start()
        build_gate(scratch_db).setup()
        reset_all(build_targets('mock', router_url=URL))

        def execute(item):
            index, case, mode = item
            row = dict(name=case.name, mode=mode, schedule=asdict(case), status='failed')
            runtime = real_runtime(scratch_db, controller)
            try:
                driver = Driver(runtime, case, mode, f'{result["_id"][:8]}-{mode}-{index}')
                row['evidence'] = driver.execute()
                row['status'] = 'passed'
            except Exception as error:
                row['error'] = type(error).__name__
                try:
                    row['evidence'] = driver.capture()
                except Exception:
                    pass
            finally:
                try:
                    runtime.close()
                except Exception as error:
                    row['cleanup_error'] = type(error).__name__
                    row.setdefault('error', f'cleanup/{type(error).__name__}')
            return row

        items = [(index, case, mode) for mode in ('stopline', 'flag', 'fixed') for index, case in enumerate(schedules())]
        ordinary = [item for item in items if item[1].kind not in ('lost_reply', 'router_restart', 'paused_send')]
        isolated = [item for item in items if item[1].kind in ('lost_reply', 'router_restart', 'paused_send')]
        with ThreadPoolExecutor(max_workers=8) as pool:
            rows = list(pool.map(execute, ordinary))
        rows.extend(execute(item) for item in isolated)
        by_key = {(row['mode'], row['name']): row for row in rows}
        result['rows'] = [by_key[mode, case.name] for _, case, mode in items]
    finally:
        controller.stop()
    result['elapsed_seconds'] = round(time.monotonic() - started, 3)
    result['under_six_minutes'] = result['elapsed_seconds'] < 360
    return finalize(result)
