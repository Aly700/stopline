"""Exercise real graph delegation with LangChain's deterministic chat model."""

import asyncio
import threading
import time

import pytest

from tests.agents.fakes import Database, Gate, Sender, Target
from stopline.contract import Admission, OPERATOR, REPAIR_AGENT, Reason, WORKER_ONE, WORKER_TWO


@pytest.fixture
def rig():
    db = Database({}, threading.RLock())
    gate = Gate(db)
    targets = {name: Target(db, name) for name in ('cluster', 'router')}
    return db, gate, Sender(db, gate, targets)


def runtime(rig):
    from stopline.deep.runtime import Run, create_records
    db, gate, sender = rig
    create_records(db, gate, 'rtest', 'fake', 'mock', False)
    return Run(db, gate, sender, 'rtest')


def test_each_action_is_admitted_claimed_and_sent_once(rig):
    run = runtime(rig)
    answer = run.act(WORKER_ONE, 'rollback', 'cluster/checkout-api', {'to_version': 'v1'})
    assert 'Allowed and applied' in answer
    assert 'action_id' in answer
    db, _, _ = rig
    assert len(db.effects.find()) == len(db.send_calls.find()) == 1
    assert db.effects.find()[0]['state'] == 'applied'


def test_stop_refuses_next_action_and_halts_all_workers(rig):
    run = runtime(rig)
    db, gate, _ = rig
    run.act(WORKER_ONE, 'rollback', 'cluster/checkout-api', {'to_version': 'v1'})
    gate.stop(mandate_id=db.runs.find_one() ['agent_mandate_id'], by=OPERATOR)
    answer = run.act(WORKER_TWO, 'shift_traffic', 'router/region-b', {'percent': 25})
    assert answer.startswith('REFUSED: Operator stopped the permission')
    from stopline.deep.runtime import RunEnded
    with pytest.raises(RunEnded):
        run.act(WORKER_ONE, 'restart', 'cluster/checkout-worker', {})
    run.finish()
    assert len(db.send_calls.find()) == 1
    assert len(db.audit.find({'kind': 'deny'})) == 1
    assert {t['status'] for t in db.tasks.find()} == {'stopped'}
    assert db.runs.find_one()['status'] == 'stopped'


def test_refused_action_is_never_claimed_or_sent(rig):
    run = runtime(rig)
    db, gate, sender = rig
    gate.admit = lambda a: Admission(False, a.action_id, Reason.LIMIT_EXCEEDED)
    sender.claim = lambda _: pytest.fail('refused action was claimed')
    assert run.act(WORKER_TWO, 'shift_traffic', 'router/region-b', {'percent': 75}).startswith('REFUSED: limit exceeded.')
    assert not db.send_calls.find()


def test_unique_ids_and_action_limit(rig):
    run = runtime(rig)
    from stopline.deep.runtime import RunEnded
    for _ in range(10):
        run.act(WORKER_TWO, 'shift_traffic', 'router/region-b', {'percent': 25})
    with pytest.raises(RunEnded):
        run.act(WORKER_TWO, 'shift_traffic', 'router/region-b', {'percent': 25})
    effects = rig[0].effects.find()
    assert len(effects) == len({e['_id'] for e in effects}) == 10


def test_deadline_prevents_actions(rig):
    run = runtime(rig)
    run.deadline = time.monotonic() - 1
    from stopline.deep.runtime import RunEnded
    with pytest.raises(RunEnded):
        run.act(WORKER_TWO, 'shift_traffic', 'router/region-b', {'percent': 25})
    assert not rig[0].effects.find()


def test_record_shapes_and_scopes(rig):
    runtime(rig)
    db, _, _ = rig
    run = db.runs.find_one()
    assert run['mode'] == 'stopline' and run['holds'] == []
    assert run['planner'] == 'deep agent · fake'
    assert len(db.tasks.find()) == 3
    mandates = {m['holder']: m for m in db.mandates.find()}
    assert mandates[REPAIR_AGENT]['limits'] == {'max_actions': 4, 'max_shift_percent': 50}
    assert mandates[WORKER_ONE]['limits'] == {'max_actions': 2, 'max_shift_percent': 0}
    assert mandates[WORKER_TWO]['limits'] == {'max_actions': 2, 'max_shift_percent': 50}
    assert mandates[OPERATOR]['limits']['max_actions'] == 12


def fake_model(messages):
    pytest.importorskip('deepagents')
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel

    class ToolChat(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    return ToolChat(responses=messages)


def message(name=None, args=None, identifier='call'):
    from langchain_core.messages import AIMessage
    return AIMessage(content='' if name else 'Finished.', tool_calls=[
        {'name': name, 'args': args, 'id': identifier}
    ] if name else [])


def test_deep_agent_delegates_both_workers_and_stops(rig):
    run = runtime(rig)
    from stopline.deep.agent import build_agent, invoke_agent
    root = fake_model([
        message('task', {'subagent_type': 'cluster-worker', 'description': 'Roll back and restart.'}),
        message('task', {'subagent_type': 'traffic-worker', 'description': 'Move traffic gradually.'}, 'delegate2'),
        message(),
    ])
    cluster = fake_model([
        message('rollback_deployment', {'name': 'checkout-api'}, 'rollback'),
        message('restart_deployment', {'name': 'checkout-worker'}, 'restart'),
        message(),
    ])
    traffic = fake_model([message('shift_traffic', {'percent': 25}, 'shift'), message()])
    db, gate, sender = rig
    original = sender.send
    def stop_after_second(action_id):
        result = original(action_id)
        if len(db.send_calls.find()) == 2:
            gate.stop(mandate_id=db.runs.find_one()['agent_mandate_id'], by=OPERATOR)
        return result
    sender.send = stop_after_second
    graph = build_agent(run, root, worker_models=(cluster, traffic))
    asyncio.run(invoke_agent(run, graph))
    assert len(db.send_calls.find()) == 2
    assert len(db.audit.find({'kind': 'deny'})) == 1
    assert {t['status'] for t in db.tasks.find()} == {'stopped'}
    assert run.model_calls >= 4
