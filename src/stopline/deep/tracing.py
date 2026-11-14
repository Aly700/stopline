"""Run-scoped timing traces with no prompts, arguments, outputs or errors."""

from contextlib import contextmanager
import asyncio
from uuid import uuid4


# Preserve only SDK-generated identity and timing fields. In particular, do not
# transmit serialized clients, exception bodies, prompts, environment or metadata.
TIMING_FIELDS = frozenset({
    'id', 'trace_id', 'parent_run_id', 'dotted_order', 'start_time', 'end_time',
    'run_type', 'session_id', 'reference_example_id', 'first_token_time',
})
NAMES = frozenset({'LangGraph', 'model', 'tools', 'task', 'rollback_deployment',
                   'restart_deployment', 'shift_traffic', 'cluster-worker', 'traffic-worker'})


def scrub_operations(operations, run_id):
    cleaned = []
    for operation in operations:
        item = {key: value for key, value in operation.items() if key in TIMING_FIELDS}
        name = operation.get('name')
        item.update(name=name if name in NAMES or name == run_id else 'repair step',
                    inputs={}, outputs={}, extra={}, serialized={}, tags=[run_id], session_name='stopline')
        if operation.get('error'):
            item['error'] = 'The step did not complete.'
        cleaned.append(item)
    return cleaned


class Trace:
    def __init__(self, client):
        self.client = client
        self.id = uuid4()

    def written(self):
        try:
            self.client.flush(timeout=3)
            project = self.client.read_project(project_name='stopline')
            async def retrieve():
                return await self.client.runs.retrieve(str(self.id), project_id=str(project.id),
                                                       selects=['ID', 'NAME'], timeout=2)
            return asyncio.run(retrieve()) is not None
        except Exception:
            return False


@contextmanager
def trace_run(key, run_id):
    from langsmith import Client, tracing_context

    if not key:
        with tracing_context(enabled=False):
            yield None
        return
    from urllib3.util.retry import Retry

    try:
        client = Client(api_key=key, api_url='https://api.smith.langchain.com',
                        hide_inputs=True, hide_outputs=True, hide_metadata=True,
                        omit_traced_runtime_info=True, timeout_ms=2000,
                        retry_config=Retry(total=0), run_ops_buffer_size=100,
                        run_ops_buffer_timeout_ms=100,
                        process_buffered_run_ops=lambda ops: scrub_operations(ops, run_id))
    except Exception:
        with tracing_context(enabled=False):
            yield None
        return
    trace = Trace(client)
    with tracing_context(enabled=True, client=client, project_name='stopline', tags=[run_id]):
        yield trace
