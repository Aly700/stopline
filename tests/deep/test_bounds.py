import asyncio
import time

import pytest

from test_runtime import fake_model, message, rig, runtime
from stopline.contract import Claim, OPERATOR, Reason, WORKER_ONE, WORKER_TWO


def test_stop_between_admission_and_claim_never_sends(rig):
    run = runtime(rig)
    db, gate, _ = rig
    admit = gate.admit
    def stop_after_allow(action):
        result = admit(action)
        gate.stop(mandate_id=db.runs.find_one()['agent_mandate_id'], by=OPERATOR)
        return result
    gate.admit = stop_after_allow
    assert run.act(WORKER_ONE, 'restart', 'cluster/checkout-worker', {}).startswith('REFUSED:')
    assert not db.send_calls.find()


def test_parallel_tool_batch_cannot_run_after_refusal(rig):
    run = runtime(rig)
    from stopline.deep.agent import build_agent, invoke_agent
    from langchain_core.messages import AIMessage
    db, gate, _ = rig
    gate.stop(mandate_id=db.runs.find_one()['agent_mandate_id'], by=OPERATOR)
    root = fake_model([message('task', {'subagent_type': 'cluster-worker', 'description': 'Repair cluster.'})])
    worker = fake_model([AIMessage(content='', tool_calls=[
        {'name': 'rollback_deployment', 'args': {'name': 'checkout-api'}, 'id': 'a'},
        {'name': 'restart_deployment', 'args': {'name': 'checkout-worker'}, 'id': 'b'},
    ])])
    asyncio.run(invoke_agent(run, build_agent(run, root, worker_models=(worker, worker))))
    assert len(db.audit.find({'kind': 'deny'})) == 1
    assert not db.send_calls.find()
    assert db.runs.find_one()['status'] == 'stopped'


def test_builtin_tools_are_not_exposed_or_executable(rig):
    run = runtime(rig)
    from stopline.deep.agent import build_agent, invoke_agent
    bad = fake_model([message('write_file', {'file_path': '/unexpected', 'content': 'bad'})])
    asyncio.run(invoke_agent(run, build_agent(run, bad)))
    assert run.halted.is_set()
    assert not rig[0].effects.find()


@pytest.mark.parametrize('rate_limit, expected', [(True, 3), (False, 1)])
def test_retries_only_rate_limits_twice(rig, rate_limit, expected):
    run = runtime(rig)
    from stopline.deep.agent import guard
    from stopline.deep.runtime import RunEnded
    from langchain.agents.middleware.types import ModelRequest
    request = ModelRequest(model=fake_model([message()]), messages=[], tools=[])
    class Failure(Exception):
        status_code = 429 if rate_limit else 401
    async def handler(request):
        raise Failure('private provider response')
    with pytest.raises(RunEnded, match='model could not answer'):
        asyncio.run(guard(run, set(), 'Repair.').awrap_model_call(request, handler))
    assert run.model_calls == expected
    assert 'private' not in run.reason


def test_model_timeout_stops_graph_and_finalizes_tasks(rig, monkeypatch):
    run = runtime(rig)
    from stopline.deep import agent
    monkeypatch.setattr(agent, 'MODEL_SECONDS', 0.02)
    slow = fake_model([message()])
    slow.sleep = 0.1
    asyncio.run(agent.invoke_agent(run, agent.build_agent(run, slow)))
    assert run.model_calls == 1
    assert 'too long' in run.reason
    assert {t['status'] for t in rig[0].tasks.find()} == {'stopped'}


def test_run_timeout_stops_a_waiting_graph(rig):
    run = runtime(rig)
    from stopline.deep.agent import invoke_agent
    run.deadline = time.monotonic() + 0.02
    class Waiting:
        async def ainvoke(self, *args, **kwargs):
            await asyncio.sleep(5)
    started = time.monotonic()
    asyncio.run(invoke_agent(run, Waiting()))
    assert time.monotonic() - started < 0.5
    assert {t['status'] for t in rig[0].tasks.find()} == {'stopped'}


def test_delegation_counts_toward_shared_tool_limit(rig):
    run = runtime(rig)
    from stopline.deep.agent import build_agent, invoke_agent
    run.tool_calls = 10
    root = fake_model([message('task', {'subagent_type': 'cluster-worker', 'description': 'Repair.'})])
    asyncio.run(invoke_agent(run, build_agent(run, root)))
    assert run.tool_calls == 10
    assert not rig[0].effects.find()
    assert 'ten tool calls' in run.reason
