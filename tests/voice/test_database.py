"""Optional real change-stream check; always uses a disposable voice database."""

import copy
import os
import time

import pytest

from stopline import db as stopline_db
from stopline.voice.announcer import Watcher


def wait_for(predicate):
    deadline = time.monotonic() + 6
    while not predicate() and time.monotonic() < deadline:
        time.sleep(.02)
    assert predicate(), 'expected change-stream announcement did not arrive'


def test_real_receipt_and_baseline_changes(receipt_records, monkeypatch):
    if os.environ.get('STOPLINE_VOICE_DB_TEST') != '1':
        pytest.skip('set STOPLINE_VOICE_DB_TEST=1 for the database check')
    monkeypatch.delenv('STOPLINE_VOICE', raising=False)
    database = stopline_db.test_database('voice')
    watcher = None
    spoken = []
    try:
        for name in ('receipts', 'runs', 'effects', 'audit'):
            database.create_collection(name)
        receipt, effects, refusals = copy.deepcopy(receipt_records)
        database.effects.insert_many(effects)
        database.audit.insert_many(refusals)
        watcher = Watcher(database, speaker=spoken.append, settle_seconds=.15).start()
        assert watcher.ready.wait(10), 'change stream did not open'
        database.receipts.insert_one(receipt)
        wait_for(lambda: len(spoken) == 2)
        assert spoken == ['Stop confirmed. One completed. One unknown. Two blocked.',
                          'The receipt is open.']
        database.effects.update_one({'_id': 'shift'}, {'$set': {'state': 'applied'}})
        database.receipts.update_one({'_id': receipt['_id']}, {'$set': {'status': 'closed'}})
        wait_for(lambda: len(spoken) == 4)
        assert spoken[2:] == ['The router answered. The traffic shift was applied.',
                              'The receipt is closed.']
        database.effects.insert_one(dict(_id='late', run_id='baseline', state='applied',
                                          evidence={'action_id': 'late', 'system': 'cluster', 'at_epoch': 101}))
        database.runs.insert_one(dict(_id='baseline', mode='flag', stop_flag=False, stop_at_local=None))
        database.runs.update_one({'_id': 'baseline'}, {'$set': {'stop_flag': True, 'stop_at_local': 100}})
        wait_for(lambda: len(spoken) == 5)
        assert spoken[-1] == 'Stop pressed. One action ran after the stop.'
    finally:
        if watcher:
            watcher.close()
            watcher.thread.join(7)
        stopline_db.drop_test_database(database)
