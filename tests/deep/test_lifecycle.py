import asyncio
import threading
import time

import pytest

from test_runtime import fake_model, message, rig, runtime
from stopline.contract import REPAIR_AGENT, WORKER_ONE


def test_success_finalizes_all_three_tasks(rig):
    run = runtime(rig)
    from stopline.deep.agent import build_agent, invoke_agent
    graph = build_agent(run, fake_model([message()]))
    asyncio.run(invoke_agent(run, graph))
    assert {task['status'] for task in rig[0].tasks.find()} == {'done'}
    assert rig[0].runs.find_one()['status'] == 'done'


def test_arbitrary_model_errors_become_plain_notes(rig):
    run = runtime(rig)
    from stopline.deep.agent import invoke_agent
    class Broken:
        async def ainvoke(self, *args, **kwargs):
            raise RuntimeError('private-key-value mongodb+srv://private-uri-value')
    asyncio.run(invoke_agent(run, Broken()))
    notes = ' '.join(t['note'] for t in rig[0].tasks.find())
    assert 'private' not in notes and 'mongodb' not in notes
    assert {task['status'] for task in rig[0].tasks.find()} == {'stopped'}


def test_public_start_saves_all_records_before_model_runs(rig, monkeypatch):
    from stopline.deep import config, start_agent_run, agent
    db, gate, sender = rig
    monkeypatch.setattr(config, 'select_model', lambda: config.ModelConfig('google_genai', 'fake', 'test-value'))
    monkeypatch.setattr(config, 'read_setting', lambda _: '')
    monkeypatch.setattr(config, 'packages_available', lambda _: True)
    entered = threading.Event()
    def make_model(_):
        assert len(db.tasks.find()) == 3
        assert len(db.mandates.find()) == 4
        entered.set()
        return fake_model([message()])
    monkeypatch.setattr(config, 'make_model', make_model)
    result = start_agent_run(db, gate, sender, {}, cluster='mock')
    assert result.ok and not result.pending
    assert result.message == 'Started a repair run planned by a language model.'
    assert entered.wait(1)
    deadline = time.monotonic() + 2
    while db.runs.find_one()['status'] == 'running' and time.monotonic() < deadline:
        time.sleep(0.01)
    assert db.runs.find_one()['status'] == 'done'


def test_declined_handoff_finishes_existing_tasks(rig, monkeypatch):
    from stopline.deep import config, start_agent_run
    from stopline.contract import HandDown, Reason
    db, gate, sender = rig
    monkeypatch.setattr(config, 'select_model', lambda: config.ModelConfig('google_genai', 'fake', 'test-value'))
    monkeypatch.setattr(config, 'read_setting', lambda _: '')
    monkeypatch.setattr(config, 'packages_available', lambda _: True)
    original = gate.hand_down
    def refuse_worker(**kw):
        if kw['holder'] == WORKER_ONE:
            return HandDown(False, None, Reason.REVOKED)
        return original(**kw)
    gate.hand_down = refuse_worker
    result = start_agent_run(db, gate, sender, {}, cluster='mock')
    assert not result.ok
    deadline = time.monotonic() + 1
    while any(t['status'] == 'running' for t in db.tasks.find()) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(db.tasks.find()) == 3
    assert {t['status'] for t in db.tasks.find()} == {'stopped'}


def test_finalization_can_retry_after_a_database_error(rig):
    run = runtime(rig)
    db, _, _ = rig
    real_find = db.runs.find_one
    collection = db.runs
    calls = 0
    def transient(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError('database unavailable')
        return real_find(*args, **kwargs)
    collection.find_one = transient
    db.runs = collection
    with pytest.raises(RuntimeError):
        run.finish()
    run.finish()
    assert {task['status'] for task in db.tasks.find()} == {'done'}
