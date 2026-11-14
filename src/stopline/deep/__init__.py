"""Optional language-model repair runs; importing needs no model packages."""

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
    from pymongo import timeout

    run_id = 'r' + secrets.token_hex(5)
    tracing_key = config.read_setting('LANGSMITH_API_KEY')
    try:
        with timeout(1.7):
            create_records(db, gate, run_id, selected.model, cluster, bool(tracing_key))
    except Exception:
        try:
            Run(db, gate, sender, run_id).halt('The repair could not start.')
            db.runs.update_one({'_id': run_id}, {'$set': {'status': 'stopped'}})
            db.tasks.update_many({'run_id': run_id}, {'$set': {'status': 'stopped', 'note': 'The repair could not start.'}})
        except Exception:
            pass
        return StageResult(False, 'The repair could not start.', run_id=run_id)
    run = Run(db, gate, sender, run_id)

    def execute():
        try:
            import asyncio
            from langsmith import tracing_context
            from .agent import build_agent, invoke_agent
            # Always disable inherited ambient tracing unless explicitly configured.
            with tracing_context(enabled=False):
                model = config.make_model(selected)
                graph = build_agent(run, model, incident=incident)
                asyncio.run(invoke_agent(run, graph))
        except BaseException:
            run.halt('Stopped because the repair could not continue.')
        finally:
            try:
                run.finish()
            except BaseException:
                pass

    threading.Thread(target=execute, name=f'repair-{run_id}', daemon=True).start()
    return StageResult(True, 'Started a repair run planned by a language model.', run_id=run_id)
