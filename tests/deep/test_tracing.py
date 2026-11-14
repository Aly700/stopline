from stopline.deep.config import ModelConfig


def test_trace_operations_contain_no_payloads_or_errors():
    from stopline.deep.tracing import scrub_operations
    secret = 'private-model-credential'
    uri = 'mongodb+srv://private-connection'
    operations = [{
        'id': '123', 'trace_id': '123', 'name': secret, 'run_type': 'tool',
        'inputs': {'name': secret}, 'outputs': {'answer': uri},
        'error': f'{secret} {uri}', 'extra': {'metadata': {'database': uri}},
        'serialized': {'api_key': secret}, 'tags': [secret], 'session_name': uri,
    }]
    scrubbed = scrub_operations(operations, 'rtest')
    assert secret not in repr(scrubbed)
    assert uri not in repr(scrubbed)
    assert scrubbed[0]['session_name'] == 'stopline'
    assert scrubbed[0]['tags'] == ['rtest']


def test_key_reader_selects_only_named_assignments(monkeypatch, tmp_path):
    from stopline.deep import config
    monkeypatch.setattr(config.Path, 'home', lambda: tmp_path)
    for _, names, _, _ in config.PROVIDERS:
        for name in names:
            monkeypatch.delenv(name, raising=False)
    (tmp_path / '.stopline_env').write_text(
        "MONGODB_URI='private-database-value'\nexport GOOGLE_API_KEY = 'google-value'\nANTHROPIC_API_KEY='anthropic-value'\n"
    )
    chosen = config.select_model()
    assert chosen.provider == 'google_genai'
    assert chosen.key == 'google-value'
    assert 'google-value' not in repr(chosen)
    monkeypatch.setenv('GEMINI_API_KEY', 'gemini-value')
    assert config.select_model().key == 'gemini-value'


def test_tracing_is_disabled_without_key_even_if_ambient_enabled(monkeypatch):
    import pytest
    pytest.importorskip('langsmith')
    from langsmith.run_helpers import get_tracing_context
    from stopline.deep.tracing import trace_run
    monkeypatch.setenv('LANGSMITH_TRACING', 'true')
    with trace_run('', 'rtest') as trace:
        assert get_tracing_context()['enabled'] is False
        assert trace is None


def test_tracing_setup_failure_does_not_prevent_repair(monkeypatch):
    import pytest
    ls = pytest.importorskip('langsmith')
    from stopline.deep.tracing import trace_run
    def unavailable(**kwargs):
        raise RuntimeError('private tracing setup response')
    monkeypatch.setattr(ls, 'Client', unavailable)
    with trace_run('test-value', 'rtest') as trace:
        assert trace is None


class FakeSmith:
    otel_exporter = None
    def __init__(self):
        from types import SimpleNamespace
        self.feedback = []
        self.runs = SimpleNamespace(retrieve=self.retrieve, get_url=self.get_url)
        self.traces = SimpleNamespace(list_runs=self.list_runs)
        self.spans = []
        self.url = 'https://smith.langchain.com/o/test/projects/p/project/r/root'

    def flush(self, *, timeout):
        assert timeout <= 5

    def read_project(self, **kwargs):
        from types import SimpleNamespace
        return SimpleNamespace(id='project')

    async def retrieve(self, run_id, **kwargs):
        from types import SimpleNamespace
        assert kwargs['timeout'] <= 5
        return SimpleNamespace(id=run_id)

    async def get_url(self, run_id, **kwargs):
        from types import SimpleNamespace
        assert kwargs['trace_id'] == run_id
        assert kwargs['timeout'] <= 5
        return SimpleNamespace(url=self.url)

    async def list_runs(self, trace_id, **kwargs):
        from types import SimpleNamespace
        return SimpleNamespace(items=self.spans)

    def create_feedback(self, **kwargs):
        assert kwargs['stop_after_attempt'] == 1
        self.feedback.append(kwargs)


def test_current_tool_span_carries_allow_and_stop_metadata(monkeypatch):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from stopline.contract import Action, Admission, OPERATOR, Reason, WORKER_TWO
    from stopline.deep.tracing import Trace, scrub_operations
    import langsmith
    span = SimpleNamespace(metadata={}, tags=[])
    monkeypatch.setattr(langsmith, 'get_current_run_tree', lambda: span)
    trace = Trace(FakeSmith())
    action = Action('rtest-worker-2-0', 'rtest', 'permission', WORKER_TWO, 'shift_traffic', 'router/region-b', {'percent': 25})
    trace.decision(action, Admission(True, action.action_id, Reason.OK, seq=4))
    assert span.metadata == {'stopline_action_id': action.action_id, 'stopline_decision': 'allowed',
                             'stopline_reason': 'ok', 'stopline_counter': 4}
    assert span.tags == ['stopline:allowed']
    at = datetime(2026, 11, 14, 20, 36, tzinfo=timezone.utc)
    trace.decision(action, Admission(False, action.action_id, Reason.REVOKED, seq=9,
                                    detail={'revoked_by': OPERATOR, 'revoked_at': at, 'secret': 'private-value'}))
    assert span.metadata['stopline_counter'] == 9
    assert span.metadata['stopline_stopped_by'] == OPERATOR
    assert span.metadata['stopline_stopped_at'] == at.isoformat()
    assert span.tags == ['stopline:refused']
    payload = scrub_operations([{'name': 'shift_traffic', 'extra': {'metadata': span.metadata}, 'tags': span.tags}], 'rtest')[0]
    assert payload['extra']['metadata'] == span.metadata
    assert payload['tags'] == ['rtest', 'stopline:refused']
    assert 'private-value' not in repr(payload)


def test_only_allowed_metadata_types_survive_scrubbing():
    from stopline.deep.tracing import scrub_operations
    bad = {'stopline_action_id': 'mongodb+srv://private', 'stopline_reason': 'private-key',
           'stopline_counter': 'private-key', 'stopline_stopped_by': 'private-key',
           'stopline_stopped_at': 'private-key', 'database': 'private-key'}
    result = scrub_operations([{'extra': {'metadata': bad}}], 'rtest')
    assert 'private' not in repr(result)
    assert result[0]['extra']['metadata'] == {}


def test_root_feedback_and_url_saved_on_run():
    import threading
    from tests.agents.fakes import Database
    from types import SimpleNamespace
    from stopline.deep.tracing import Trace
    db = Database({}, threading.RLock())
    db.runs.insert_one({'_id': 'rtest', 'status': 'stopped', 'receipt_id': 'receipt'})
    db.receipts.insert_one({'_id': 'receipt', 'status': 'closed'})
    run = SimpleNamespace(db=db, run_id='rtest', actions_allowed=2, actions_refused=1)
    client = FakeSmith()
    trace = Trace(client)
    trace.bind(run)
    assert db.runs.find_one()['trace_id'] == str(trace.id)
    trace.complete(run)
    saved = db.runs.find_one()
    assert saved['trace_url'] == client.url and saved['trace_written']
    values = {item['key']: item.get('score', item.get('value')) for item in client.feedback}
    assert values == {'stopline_stopped': True, 'stopline_actions_allowed': 2,
                      'stopline_actions_refused': 1, 'stopline_receipt': 'closed'}
    assert all(item['run_id'] == trace.id for item in client.feedback)


def test_failed_or_slow_calls_are_bounded_and_notes_are_plain(monkeypatch, caplog):
    import time
    from stopline.deep import tracing
    monkeypatch.setattr(tracing, 'CALL_SECONDS', 0.02)
    def fail():
        raise RuntimeError('private-key mongodb+srv://private')
    assert tracing.safe_call(fail) is None
    started = time.monotonic()
    assert tracing.safe_call(lambda: time.sleep(1)) is None
    assert time.monotonic() - started < 0.2
    assert 'private' not in caplog.text and 'mongodb' not in caplog.text
    assert len(caplog.records) == 2


def test_real_graph_attaches_metadata_to_the_worker_tool_span(monkeypatch):
    import asyncio
    import threading
    from copy import deepcopy
    from tests.agents.fakes import Database, Gate, Sender, Target
    from test_runtime import fake_model, message
    from stopline.contract import Admission, Reason
    from stopline.deep.runtime import Run, create_records
    from stopline.deep.agent import build_agent, invoke_agent
    from stopline.deep.tracing import Trace
    from langchain_core.tracers.langchain import LangChainTracer
    from langsmith import tracing_context

    class Recorder(FakeSmith):
        def __init__(self):
            super().__init__()
            self.operations = []
        def create_run(self, **kwargs):
            self.operations.append(deepcopy(kwargs))
        def update_run(self, run_id, **kwargs):
            self.operations.append(deepcopy(kwargs))

    db = Database({}, threading.RLock())
    gate = Gate(db)
    targets = {name: Target(db, name) for name in ('cluster', 'router')}
    create_records(db, gate, 'rtest', 'fake', 'mock', True)
    admit = gate.admit
    def numbered(action):
        admit(action)
        return Admission(True, action.action_id, Reason.OK, seq=7)
    gate.admit = numbered
    run = Run(db, gate, Sender(db, gate, targets), 'rtest')
    client = Recorder()
    trace = Trace(client)
    trace.bind(run)
    root = fake_model([message('task', {'subagent_type': 'cluster-worker', 'description': 'Roll back.'}), message()])
    worker = fake_model([message('rollback_deployment', {'name': 'checkout-api'}), message()])
    with tracing_context(enabled=True, client=client, project_name='stopline'):
        graph = build_agent(run, root, worker_models=(worker, worker))
        asyncio.run(invoke_agent(run, graph, callbacks=[LangChainTracer(client=client, project_name='stopline')], trace_id=trace.id))
    annotated = [op for op in client.operations if (op.get('extra') or {}).get('metadata', {}).get('stopline_action_id')]
    assert annotated
    assert any(op['name'] == 'rollback_deployment' and op['extra']['metadata']['stopline_counter'] == 7 for op in annotated)


def test_feedback_without_stop_or_receipt_and_failures_do_not_change_run(caplog):
    import threading
    from types import SimpleNamespace
    from tests.agents.fakes import Database
    from stopline.deep.tracing import Trace
    db = Database({}, threading.RLock())
    db.runs.insert_one({'_id': 'rtest', 'status': 'done'})
    run = SimpleNamespace(db=db, run_id='rtest', actions_allowed=1, actions_refused=0)
    client = FakeSmith()
    trace = Trace(client)
    trace.bind(run)
    trace.complete(run)
    values = {item['key']: item.get('score', item.get('value')) for item in client.feedback}
    assert values['stopline_stopped'] is False and values['stopline_receipt'] == 'none'
    def broken(**kwargs):
        raise RuntimeError('private-value mongodb+srv://private')
    client.read_project = broken
    trace.complete(run)
    assert db.runs.find_one()['status'] == 'done'
    assert db.runs.find_one()['trace_written'] is False
    assert 'private' not in caplog.text


def test_async_calls_timeout_without_leaking_errors(monkeypatch, caplog):
    import asyncio
    import time
    from stopline.deep import tracing
    monkeypatch.setattr(tracing, 'CALL_SECONDS', 0.02)
    async def slow():
        await asyncio.sleep(1)
    started = time.monotonic()
    assert asyncio.run(tracing.safe_async(slow)) is None
    assert time.monotonic() - started < 0.2
    assert len(caplog.records) == 1
