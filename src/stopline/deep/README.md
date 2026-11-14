# Language-model repair runs

Install and demonstrate:

```sh
uv sync --extra agent
uv run --extra agent python -m stopline.deep
```

The command owns a mock cluster, a router process on port 8816, and a fresh
`stopline_test_deep_*` database. It stops the repair permission immediately
after the second applied action and requires the next action to be refused.
It removes its router and test database when finished. It prints model calls,
elapsed seconds, and whether the LangSmith trace could be read back.

The public entry points are `model_configured()` and
`start_agent_run(db, gate, sender, targets, *, cluster, incident=None)`.
The latter returns a `StageResult` and runs in a daemon thread. Assign its
`run_id` to the existing stage to use the existing stop command and page data.
The caller must have run `gate.setup()` already, as in the scripted demo.
If Atlas takes more than 1.5 seconds to create the permission tree, the result
has `pending=True`; the model starts only after all three task records and
four permissions are saved. A setup failure leaves a stopped run with a plain
note. Missing credentials or packages create no records.

The supervisor uses `create_deep_agent` and delegates to `cluster-worker` and
`traffic-worker`. Each worker is a compiled LangChain subagent with only its
repair tools. File, shell, and todo tools are removed from model requests and
blocked by middleware if requested. The built-in general-purpose worker is
also blocked. The `task` tool is internal delegation, counted against the
shared ten-call limit; it does not send a target action. Every rollback,
restart, and traffic shift has a unique action id and must pass `gate.admit`,
`sender.claim`, and `sender.send`. Agents never receive the target adapters.
The existing gate enforces two actions per worker, four across the repair,
and a 50% traffic ceiling. Any refusal stops the entire graph, including
already queued tool requests.

A run has a 120-second deadline, each model request has a 20-second timeout,
and only rate-limit responses get retries (at most two, after short waits).
The recorded model-call count includes retry attempts. Final task notes and
run status are saved on success, refusal, timeout, or error. An already sent
action can still complete after a deadline or stop; the existing receipt and
reconciliation rules remain authoritative.

Credentials are selected in this order: `GEMINI_API_KEY`, `GOOGLE_API_KEY`,
`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`. Each value comes from the environment
or its exact named assignment in `~/.stopline_env`; that file is never sourced.
Defaults are `gemini-3.1-flash-lite`, `claude-haiku-5-5`, and `gpt-6-luna`.
`STOPLINE_AGENT_MODEL` overrides the selected provider's model name.
Only the Google provider was exercised against a live model in this change.

`LANGSMITH_API_KEY` enables tracing for this run in project `stopline`, with
the StopLine run id as its name and tag. Without it, tracing is disabled even
if ambient tracing settings are enabled. Traces retain timing, tool names,
hierarchy, and an allowlist of StopLine decision fields. Prompts, arguments,
results, serialized clients, other metadata, and raw errors are removed before
transmission. Trace availability never grants permission to act.

Each repair-tool span carries `stopline_action_id`, `stopline_decision`,
`stopline_reason`, and `stopline_counter`, plus `stopline_stopped_by` and
`stopline_stopped_at` when a stop caused the refusal. Its tag is
`stopline:allowed` or `stopline:refused`. A claim cancelled after admission
updates that span to the refusal and retains the admission counter, matching
the claim's audit entry. Counts include both the admission and that refusal.

On completion, the root trace receives `stopline_stopped`,
`stopline_actions_allowed`, `stopline_actions_refused`, and `stopline_receipt`
feedback. The stopped flag mirrors the final run status; the receipt value
is `closed`, `open`, or `none`. The run record saves `trace_id` and the
`trace_url` returned by LangSmith. SDK calls have a five-second outer deadline
and four-second transport timeouts; failures emit a fixed, secret-free note
without changing the repair result. The demonstration also compares the
stored audit entries with the tool spans returned by LangSmith.

Offline checks:

```sh
uv run --extra agent pytest tests/deep --ignore=tests/deep/test_live.py
```

The live test runs the demonstration when a model credential is present:

```sh
uv run --extra agent pytest tests/deep/test_live.py -s
```

References checked on 2026-11-14:

- [Deep Agents quickstart](https://docs.langchain.com/oss/python/deepagents/quickstart)
- [Agent construction reference](https://reference.langchain.com/python/deepagents/graph/create_deep_agent)
- [Compiled subagents](https://docs.langchain.com/oss/python/deepagents/subagents)
- [Google chat model integration](https://docs.langchain.com/oss/python/integrations/chat/google_generative_ai)
- [Gemini 3.1 Flash-Lite](https://aistudio.google.com/docs/models/gemini-3.1-flash-lite)
- [LangSmith tracing configuration](https://docs.langchain.com/langsmith/trace-with-langchain)
- [LangSmith payload privacy](https://docs.langchain.com/langsmith/mask-inputs-outputs)
- [Current-span metadata and tags](https://docs.langchain.com/langsmith/add-metadata-tags)
- [Feedback API](https://reference.langchain.com/python/langsmith/client/Client/create_feedback)
- [Root URL and trace retrieval](https://docs.langchain.com/langsmith/smithdb-sdk-migration-runs)
- [Flush deadline](https://reference.langchain.com/python/langsmith/client/Client/flush)
- [Claude models](https://platform.claude.com/docs/en/models/overview)
- [OpenAI model](https://developers.openai.com/api/docs/models/gpt-6-luna)
