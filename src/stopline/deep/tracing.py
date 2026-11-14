"""Bounded trace updates containing only StopLine decisions and timing."""

from concurrent.futures import Future
from contextlib import contextmanager
from contextvars import copy_context
from datetime import datetime
import asyncio
import logging
import re
import threading
from uuid import uuid4

from stopline.contract import OPERATOR, REPAIR_AGENT, WORKER_ONE, WORKER_TWO, Reason

CALL_SECONDS = 5
LOGGER = logging.getLogger(__name__)
TIMING_FIELDS = frozenset({
    'id', 'trace_id', 'parent_run_id', 'dotted_order', 'start_time', 'end_time',
    'run_type', 'session_id', 'reference_example_id', 'first_token_time',
})
NAMES = frozenset({'LangGraph', 'model', 'tools', 'task', 'rollback_deployment',
                   'restart_deployment', 'shift_traffic', 'cluster-worker', 'traffic-worker'})
DECISION_TAGS = frozenset({'stopline:allowed', 'stopline:refused'})


def unavailable():
    LOGGER.warning('LangSmith update unavailable; the repair continues without it.')


def safe_call(operation):
    """Bound SDK calls even if a transport ignores its own request timeout."""
    result = Future()
    context = copy_context()
    def execute():
        try:
            result.set_result(context.run(operation))
        except BaseException:
            result.set_exception(RuntimeError('LangSmith update unavailable.'))
    threading.Thread(target=execute, daemon=True, name='repair-trace-update').start()
    try:
        return result.result(timeout=CALL_SECONDS)
    except Exception:
        unavailable()
        return None


async def safe_async(operation):
    try:
        return await asyncio.wait_for(operation(), timeout=CALL_SECONDS)
    except Exception:
        unavailable()
        return None


def decision_metadata(values):
    """An allowlist of field names AND value shapes; never forward record dumps."""
    safe = {}
    identity = values.get('stopline_action_id')
    if isinstance(identity, str) and re.fullmatch(r'r[a-zA-Z0-9_-]{1,40}-worker-[12]-[0-9]{1,6}', identity):
        safe['stopline_action_id'] = identity
    decision = values.get('stopline_decision')
    if decision in ('allowed', 'refused'):
        safe['stopline_decision'] = decision
    reason = values.get('stopline_reason')
    if isinstance(reason, str) and reason in {str(item) for item in Reason}:
        safe['stopline_reason'] = str(reason)
    counter = values.get('stopline_counter')
    if type(counter) is int and counter >= 0:
        safe['stopline_counter'] = counter
    who = values.get('stopline_stopped_by')
    if isinstance(who, str) and who in (OPERATOR, REPAIR_AGENT, WORKER_ONE, WORKER_TWO):
        safe['stopline_stopped_by'] = who
    when = values.get('stopline_stopped_at')
    if isinstance(when, datetime):
        when = when.isoformat()
    if isinstance(when, str) and len(when) <= 40:
        try:
            safe['stopline_stopped_at'] = datetime.fromisoformat(when).isoformat()
        except ValueError:
            pass
    return safe


def scrub_operations(operations, run_id):
    cleaned = []
    for operation in operations:
        item = {key: value for key, value in operation.items() if key in TIMING_FIELDS}
        metadata = decision_metadata((operation.get('extra') or {}).get('metadata') or {})
        tags = [run_id] + sorted(set(operation.get('tags') or []) & DECISION_TAGS)
        name = operation.get('name')
        item.update(name=name if name in NAMES or name == run_id else 'repair step',
                    inputs={}, outputs={}, extra={'metadata': metadata}, serialized={},
                    tags=tags, session_name='stopline')
        if operation.get('error'):
            item['error'] = 'The step did not complete.'
        cleaned.append(item)
    return cleaned


class Trace:
    def __init__(self, client):
        self.client = client
        self.id = uuid4()
        self.project_id = None

    def bind(self, run):
        run.trace = self
        run.db.runs.update_one({'_id': run.run_id}, {'$set': {
            'trace_id': str(self.id), 'trace_url': None,
        }})

    def decision(self, action, result, *, counter=None):
        def annotate():
            from langsmith import get_current_run_tree
            span = get_current_run_tree()
            if span is None:
                return
            allowed = getattr(result, 'allowed', getattr(result, 'claimed', False))
            values = {
                'stopline_action_id': action.action_id,
                'stopline_decision': 'allowed' if allowed else 'refused',
                'stopline_reason': result.reason,
                'stopline_counter': getattr(result, 'seq', counter),
            }
            if not allowed and result.reason in (Reason.REVOKED, Reason.CANCELLED_BY_STOP):
                values.update(stopline_stopped_by=result.detail.get('revoked_by'),
                              stopline_stopped_at=result.detail.get('revoked_at'))
            for field in tuple(span.metadata):
                if field.startswith('stopline_'):
                    del span.metadata[field]
            span.metadata.update(decision_metadata(values))
            span.tags[:] = [tag for tag in span.tags if tag not in DECISION_TAGS]
            span.tags.append('stopline:allowed' if allowed else 'stopline:refused')
        safe_call(annotate)

    def complete(self, run):
        try:
            asyncio.run(self._complete(run))
        except Exception:
            unavailable()
            # Trace failure must never change the repair's final status.
            try:
                run.db.runs.update_one({'_id': run.run_id}, {'$set': {'trace_written': False}})
            except Exception:
                pass

    async def _complete(self, run):
        await asyncio.to_thread(safe_call, lambda: self.client.flush(timeout=4))
        project = await asyncio.to_thread(safe_call, lambda: self.client.read_project(project_name='stopline'))
        written, url = False, None
        if project is not None:
            self.project_id = str(project.id)
            root = await safe_async(lambda: self.client.runs.retrieve(
                str(self.id), project_id=self.project_id, selects=['ID'], timeout=4))
            written = root is not None
            response = await safe_async(lambda: self.client.runs.get_url(
                str(self.id), project_id=self.project_id, trace_id=str(self.id), timeout=4))
            if response is not None:
                url = response.url
            saved = run.db.runs.find_one({'_id': run.run_id}) or {}
            receipt = (run.db.receipts.find_one({'_id': saved['receipt_id']})
                       if saved.get('receipt_id') else run.db.receipts.find_one({'run_id': run.run_id}))
            receipt_status = (receipt or {}).get('status', 'none')
            if receipt_status not in ('closed', 'open', 'none'):
                receipt_status = 'none'
            feedback = {'stopline_stopped': saved.get('status') == 'stopped',
                        'stopline_actions_allowed': run.actions_allowed,
                        'stopline_actions_refused': run.actions_refused,
                        'stopline_receipt': receipt_status}
            for key, value in feedback.items():
                field = 'value' if key == 'stopline_receipt' else 'score'
                await asyncio.to_thread(safe_call, lambda key=key, value=value, field=field:
                    self.client.create_feedback(run_id=self.id, key=key, session_id=self.project_id,
                                                stop_after_attempt=1, **{field: value}))
        run.db.runs.update_one({'_id': run.run_id}, {'$set': {
            'trace_written': written, 'trace_url': url,
        }})


@contextmanager
def trace_run(key, run_id):
    from langsmith import Client, tracing_context

    if not key:
        with tracing_context(enabled=False):
            yield None
        return
    from urllib3.util.retry import Retry
    client = safe_call(lambda: Client(
        api_key=key, api_url='https://api.smith.langchain.com',
        hide_inputs=True, hide_outputs=True, hide_metadata=decision_metadata,
        omit_traced_runtime_info=True, timeout_ms=4000, retry_config=Retry(total=0),
        run_ops_buffer_size=100, run_ops_buffer_timeout_ms=100,
        tracing_error_callback=lambda _: unavailable(),
        process_buffered_run_ops=lambda ops: scrub_operations(ops, run_id)))
    if client is None:
        with tracing_context(enabled=False):
            yield None
        return
    trace = Trace(client)
    with tracing_context(enabled=True, client=client, project_name='stopline', tags=[run_id]):
        yield trace


def verify_tool_spans(db, record):
    """Compare the saved audit decisions with LangSmith's repair-tool spans."""
    from .config import read_setting
    if not record.get('trace_id') or not record.get('trace_url'):
        return False
    from uuid import UUID
    with trace_run(read_setting('LANGSMITH_API_KEY'), record['_id']) as trace:
        if trace is None:
            return False
        trace.id = UUID(record['trace_id'])
        return bool(safe_call(lambda: asyncio.run(_verify_tool_spans(trace, db, record))))


async def _verify_tool_spans(trace, db, record):
    project = await asyncio.to_thread(safe_call, lambda: trace.client.read_project(project_name='stopline'))
    if project is None:
        return False
    response = await safe_async(lambda: trace.client.traces.list_runs(
        str(trace.id), project_id=str(project.id),
        selects=['ID', 'NAME', 'RUN_TYPE', 'METADATA', 'TAGS'], timeout=4))
    if response is None:
        return False
    spans = {span.metadata.get('stopline_action_id'): span for span in response.items or []
             if span.name in ('rollback_deployment', 'restart_deployment', 'shift_traffic')
             and span.metadata}
    decisions = list(db.audit.find({'run_id': record['_id'], 'kind': {'$in': ['allow', 'deny']}}))
    expected = {}
    for entry in decisions:
        action_id = entry.get('action_id')
        if not action_id:
            continue
        values = {'stopline_action_id': action_id,
                  'stopline_decision': 'allowed' if entry['kind'] == 'allow' else 'refused',
                  'stopline_reason': entry['reason'], 'stopline_counter': entry['seq']}
        if entry['reason'] in (Reason.REVOKED, Reason.CANCELLED_BY_STOP):
            values.update(stopline_stopped_by=entry['detail'].get('revoked_by'),
                          stopline_stopped_at=entry['detail'].get('revoked_at'))
        expected[action_id] = decision_metadata(values)
    return bool(expected) and set(spans) == set(expected) and all(
        all(span.metadata.get(key) == value for key, value in values.items())
        and 'stopline:' + values['stopline_decision'] in (span.tags or [])
        for action_id, values in expected.items() for span in [spans[action_id]])
