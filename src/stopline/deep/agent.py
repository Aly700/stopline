"""A Deep Agents supervisor and two restricted LangChain subagents."""

import asyncio
import time

from stopline.contract import REPAIR_AGENT, WORKER_ONE, WORKER_TWO
from .runtime import RunEnded

MODEL_SECONDS = 20
DEFAULT_INCIDENT = ('checkout-api version v2 is failing after a release; the last good version is v1; '
                    'checkout-worker should be restarted after a rollback; traffic can be moved to region B in steps')
RULES = ('Make a short repair decision and use only the supplied tools. '
         'Call one tool at a time. As soon as any tool answers REFUSED, stop and report; do not retry. '
         'Never claim a change happened without tool evidence.')


def is_rate_limit(error):
    return (getattr(error, 'status_code', None) == 429 or getattr(error, 'code', None) == 429
            or type(error).__name__ in ('RateLimitError', 'ResourceExhausted'))


def guard(run, allowed, prompt):
    from langchain.agents.middleware import AgentMiddleware
    from langchain_core.messages import SystemMessage

    class Guard(AgentMiddleware):
        async def awrap_model_call(self, request, handler):
            run.check()
            # Remove all ambient filesystem, shell and todo tools from model input.
            filtered = [t for t in request.tools if getattr(t, 'name', None) in allowed]
            request = request.override(tools=filtered, system_message=SystemMessage(content=prompt))
            for attempt in range(3):
                run.check()
                run.model_calls += 1
                try:
                    return await asyncio.wait_for(handler(request), timeout=min(MODEL_SECONDS, run.deadline - time.monotonic()))
                except Exception as error:
                    if is_rate_limit(error) and attempt < 2:
                        await asyncio.sleep(0.25 * (attempt + 1))
                        continue
                    sentence = ('Stopped because a model call took too long.' if isinstance(error, TimeoutError)
                                else 'Stopped because the model could not answer.')
                    run.halt(sentence)
                    raise RunEnded(sentence) from None

        async def awrap_tool_call(self, request, handler):
            await asyncio.to_thread(run.count_tool)
            name = request.tool_call['name']
            if name not in allowed:
                run.halt('Stopped because the model requested an unavailable tool.')
                raise RunEnded(run.reason)
            if name == 'task':
                worker = request.tool_call['args'].get('subagent_type')
                if worker not in ('cluster-worker', 'traffic-worker'):
                    run.halt('Stopped because the requested worker is unavailable.')
                    raise RunEnded(run.reason)
                sentence = ('Delegated cluster work to Worker one.' if worker == 'cluster-worker'
                            else 'Delegated traffic work to Worker two.')
                await asyncio.to_thread(run.note, REPAIR_AGENT, sentence)
            return await handler(request)

    return Guard()


def build_agent(run, model, *, incident=None, worker_models=None):
    from deepagents import create_deep_agent, CompiledSubAgent
    from langchain.agents import create_agent
    from langchain_core.tools import tool

    @tool
    async def rollback_deployment(name: str) -> str:
        """Roll back the named checkout deployment to the last good version v1."""
        return await asyncio.to_thread(run.act, WORKER_ONE, 'rollback', f'cluster/{name}', {'to_version': 'v1'}, counted=True)

    @tool
    async def restart_deployment(name: str) -> str:
        """Restart the named checkout deployment after the rollback."""
        return await asyncio.to_thread(run.act, WORKER_ONE, 'restart', f'cluster/{name}', {}, counted=True)

    @tool
    async def shift_traffic(percent: int) -> str:
        """Set region B's traffic percentage, moving gradually up to 50 percent."""
        return await asyncio.to_thread(run.act, WORKER_TWO, 'shift_traffic', 'router/region-b', {'percent': percent}, counted=True)

    cluster_model, traffic_model = worker_models or (model, model)
    cluster_prompt = (RULES + ' You are Worker one. Choose cluster actions for the delegated incident. '
                      'If a rollback is needed, restart checkout-worker only after it succeeds.')
    traffic_prompt = (RULES + ' You are Worker two. Choose gradual traffic shifts for the delegated incident, '
                      'up to 50% to region B. Check each answer before the next shift.')
    subagents = []
    for name, description, worker_model, tools, prompt in (
        ('cluster-worker', 'Worker one repairs checkout deployments.', cluster_model,
         [rollback_deployment, restart_deployment], cluster_prompt),
        ('traffic-worker', 'Worker two moves traffic gradually to region B.', traffic_model,
         [shift_traffic], traffic_prompt),
    ):
        graph = create_agent(model=worker_model, tools=tools, system_prompt=prompt,
                             middleware=[guard(run, {t.name for t in tools}, prompt)])
        subagents.append(CompiledSubAgent(name=name, description=description, runnable=graph))
    prompt = (RULES + '\nIncident: ' + (incident or DEFAULT_INCIDENT) + '\n'
              'Delegate cluster work to cluster-worker and traffic work to traffic-worker. '
              'Use only these two workers. Delegate sequentially. Move traffic gradually. '
              'Do not run repair tools yourself.')
    return create_deep_agent(model=model, subagents=subagents, system_prompt=prompt,
                             middleware=[guard(run, {'task'}, prompt)])


async def invoke_agent(run, graph, *, callbacks=None, trace_id=None):
    try:
        async with asyncio.timeout(max(0, run.deadline - time.monotonic())):
            await graph.ainvoke({'messages': [{'role': 'user', 'content': 'Repair the incident using both workers.'}]},
                               config={'run_name': run.run_id, 'tags': [run.run_id],
                                       'recursion_limit': 60, 'callbacks': callbacks or [],
                                       **({'run_id': trace_id} if trace_id else {})})
    except RunEnded:
        pass
    except TimeoutError:
        run.halt('Stopped after the two-minute time limit.')
    except BaseException:
        run.halt('Stopped because the repair could not continue.')
    finally:
        run.finish()
