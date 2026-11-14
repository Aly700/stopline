"""Optional language-model repair runs; importing needs no model packages."""

from concurrent.futures import Future, TimeoutError
import secrets
import threading

from stopline.contract import StageResult
from . import config
from .config import model_configured

__all__ = ['start_agent_run', 'model_configured']


def start_agent_run(db, gate, sender, targets, *, cluster, incident=None) -> StageResult:
    try:
        selected = config.select_model()
    except ValueError:
        return StageResult(False, 'The model name is invalid.')
    if selected is None:
        return StageResult(False, 'No model API key is set.')
    if not config.packages_available(selected.provider):
        return StageResult(False, 'Install the agent extra to run a language model.')
    # Targets stay with the sender; neither the model nor its tools receive them.
    from .runtime import Run, create_records

    run_id = 'r' + secrets.token_hex(5)
    tracing_key = config.read_setting('LANGSMITH_API_KEY')
    run = Run(db, gate, sender, run_id)
    ready = Future()

    def execute():
        try:
            from pymongo import timeout
            # Slow Atlas transactions must not block the page's command thread.
            with timeout(10):
                create_records(db, gate, run_id, selected.model, cluster, bool(tracing_key))
            ready.set_result(True)
            import asyncio
            from .agent import build_agent, invoke_agent
            from .tracing import trace_run
            with trace_run(tracing_key, run_id) as trace:
                model = config.make_model(selected)
                graph = build_agent(run, model, incident=incident)
                asyncio.run(invoke_agent(run, graph, trace_id=trace.id if trace else None))
            if trace:
                db.runs.update_one({'_id': run_id}, {'$set': {'trace_written': trace.written()}})
        except BaseException:
            run.halt('Stopped because the repair could not continue.')
            if not ready.done():
                ready.set_result(False)
        finally:
            try:
                from pymongo import timeout
                with timeout(2):
                    run.finish()
                    if tracing_key:
                        db.runs.update_one({'_id': run_id, 'trace_written': {'$exists': False}},
                                           {'$set': {'trace_written': False}})
            except BaseException:
                pass

    threading.Thread(target=execute, name=f'repair-{run_id}', daemon=True).start()
    pending = False
    try:
        if not ready.result(timeout=1.5):
            return StageResult(False, 'The repair could not start.', run_id=run_id)
    except TimeoutError:
        pending = True
    return StageResult(True, 'Started a repair run planned by a language model.', pending=pending, run_id=run_id)
