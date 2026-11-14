import copy
import queue
import time

import pytest

from stopline.voice.announcer import Watcher


def matches(row, query):
    for key, wanted in query.items():
        value = row.get(key)
        if isinstance(wanted, dict):
            if '$in' in wanted and value not in wanted['$in']:
                return False
            if '$gte' in wanted and (value is None or value < wanted['$gte']):
                return False
        elif value != wanted:
            return False
    return True


class Collection:
    def __init__(self, rows=()):
        self.rows = copy.deepcopy(list(rows))

    def find(self, query):
        return copy.deepcopy([row for row in self.rows if matches(row, query)])

    def find_one(self, query):
        return next(iter(self.find(query)), None)


class Stream:
    def __init__(self, events):
        self.events = events
        self.resume_token = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def try_next(self):
        try:
            event = self.events.get(timeout=.01)
        except queue.Empty:
            return None
        if isinstance(event, Exception):
            raise event
        self.resume_token = event['_id']
        return event


class Database:
    def __init__(self, receipt_records):
        receipt, effects, refusals = receipt_records
        self.receipts = Collection([receipt])
        self.effects = Collection(effects)
        self.audit = Collection(refusals)
        self.runs = Collection()
        self.events = queue.Queue()
        self.calls = []

    def watch(self, pipeline, **kwargs):
        self.calls.append((pipeline, kwargs))
        return Stream(self.events)

    def emit(self, collection, operation, document, **extra):
        self.events.put(dict(_id={'token': len(self.calls)}, ns={'coll': collection},
                             operationType=operation, fullDocument=copy.deepcopy(document),
                             documentKey={'_id': document['_id']}, **extra))


def wait_for(predicate):
    deadline = time.monotonic() + 3
    while not predicate() and time.monotonic() < deadline:
        time.sleep(.01)
    assert predicate()


@pytest.fixture
def running(receipt_records, monkeypatch):
    monkeypatch.delenv('STOPLINE_VOICE', raising=False)
    db = Database(receipt_records)
    spoken = []
    watcher = Watcher(db, speaker=spoken.append, settle_seconds=.08).start()
    assert watcher.ready.wait(2)
    yield db, spoken, watcher
    watcher.close()
    watcher.thread.join(2)
    assert not watcher.thread.is_alive()


def test_duplicate_stop_and_close_are_spoken_once(running):
    db, spoken, watcher = running
    receipt = db.receipts.rows[0]
    db.emit('receipts', 'insert', receipt)
    db.emit('receipts', 'insert', receipt)
    wait_for(lambda: len(spoken) == 2)
    assert spoken == ['Stop confirmed. One completed. One unknown. Two blocked.',
                      'The receipt is open.']
    receipt['status'] = 'closed'
    db.effects.rows[1]['state'] = 'applied'
    for _ in range(2):
        db.emit('receipts', 'update', receipt, updateDescription={'updatedFields': {'status': 'closed'}})
    wait_for(lambda: len(spoken) == 4)
    assert spoken[2:] == ['The router answered. The traffic shift was applied.', 'The receipt is closed.']


def test_reads_refusals_after_settling(running):
    db, spoken, watcher = running
    later_refusal = db.audit.rows.pop()
    db.emit('receipts', 'insert', db.receipts.rows[0])
    time.sleep(.025)
    assert not spoken
    db.audit.rows.append(later_refusal)
    wait_for(lambda: len(spoken) == 2)
    assert 'Two blocked.' in spoken[0]


def test_reconnect_resumes_and_deduplicates(running, caplog):
    db, spoken, watcher = running
    receipt = db.receipts.rows[0]
    db.emit('receipts', 'insert', receipt)
    wait_for(lambda: len(spoken) == 2)
    db.events.put(RuntimeError('secret-that-must-not-be-logged'))
    db.emit('receipts', 'insert', receipt)
    wait_for(lambda: len(db.calls) == 2)
    time.sleep(.15)
    assert len(spoken) == 2
    assert db.calls[1][1]['resume_after'] == {'token': 1}
    assert 'secret-that-must-not-be-logged' not in caplog.text


def test_baseline_uses_target_clock_and_only_completed_actions(running):
    db, spoken, watcher = running
    run = dict(_id='baseline', mode='flag', stop_flag=True, stop_at_local=100)
    db.runs.rows.append(run)
    db.effects.rows += [
        dict(_id='late', run_id='baseline', state='applied', evidence={'at_epoch': 101, 'action_id': 'late', 'system': 'cluster'}),
        dict(_id='early', run_id='baseline', state='applied', evidence={'at_epoch': 99, 'action_id': 'early', 'system': 'router'}),
        dict(_id='unknown', run_id='baseline', state='sent', evidence=None),
        dict(_id='not-run', run_id='elsewhere', state='applied', evidence={'at_epoch': 101}),
    ]
    for _ in range(2):
        db.emit('runs', 'update', run)
    wait_for(lambda: len(spoken) == 1)
    assert spoken == ['Stop pressed. One action ran after the stop.']


def test_close_before_settle_keeps_stop_before_close(running):
    db, spoken, watcher = running
    receipt = db.receipts.rows[0]
    db.emit('receipts', 'insert', receipt)
    receipt['status'] = 'closed'
    db.effects.rows[1]['state'] = 'applied'
    db.emit('receipts', 'update', receipt)
    wait_for(lambda: len(spoken) == 4)
    assert spoken[0].startswith('Stop confirmed.')
    assert 'unknown' not in ' '.join(spoken)
    assert spoken[-2:] == ['The router answered. The traffic shift was applied.', 'The receipt is closed.']


def test_run_filter_and_stopline_run_flag_are_ignored(running):
    db, spoken, watcher = running
    watcher.run_filter = 'r1'
    other = dict(db.receipts.rows[0], run_id='r2')
    db.emit('receipts', 'insert', other)
    db.emit('runs', 'update', dict(_id='r1', mode='stopline', stop_flag=True))
    time.sleep(.15)
    assert not spoken


def test_off_watch_does_not_touch_database(monkeypatch):
    from stopline.voice import watch
    monkeypatch.setenv('STOPLINE_VOICE', 'off')
    assert watch(object()) is None


def test_missing_baseline_clock_does_not_claim_zero(running):
    db, spoken, watcher = running
    run = dict(_id='b', mode='fixed', stop_flag=True, stop_at_local=None)
    db.runs.rows.append(run)
    db.emit('runs', 'update', run)
    wait_for(lambda: bool(spoken))
    assert spoken == ['Stop pressed. The outcome is unknown.']
