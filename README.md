# StopLine

Stop a tree of agents mid-run, and account for every action it had already started.

Built in one day, Saturday 14 November 2026, at Agents in Context Toronto (a MongoDB hackathon), for the problem statement "Agents you can audit".

## The problem

An agent that hands work to other agents is hard to stop. The usual answer is a stop flag that each worker reads before its next step. A worker can read the flag, and then act after someone pressed stop. A flag also keeps no record of what was already on its way to another system.

Press `1` on the page to watch that happen: a restart lands on a Kubernetes Deployment one second after the stop.

## What StopLine does

- **Permission is data in Atlas.** A permission records who granted it, to whom, for which tools and targets, with what limits, until when. A parent agent hands a narrower permission to a worker.
- **Every action passes one check in Atlas before it runs.** The decision, the action record and the audit entry commit in one transaction.
- **Stop** revokes a permission and everything handed down from it. In the same transaction it cancels work that was allowed but not yet sent, and writes a receipt that lists every action already started.
- **The receipt closes only on evidence.** An action that was sent and never answered stays "unknown". StopLine asks the target system about that one action by its id. If the target applied it, that is recorded. If the target has no record, StopLine places a fence for that id so a late arrival is refused, and records that it had no effect. Waiting never closes a receipt.
- **Undo** is a new action that reverses an earlier one, through the same check, under the permission of the person who stopped the run. Actions with no inverse are marked "no undo".
- **A reviewer's report** rebuilds a whole run from the stored records and re-checks the guarantees from them.

## How the stop is made exact

1. **One shared counter per grant.** MongoDB transactions conflict on writes, never on reads, so reading "my parent is still active" protects nothing. Every check, every hand-down and every stop increments the same counter on the root permission document inside its transaction. Two of them cannot both commit from the same snapshot. The driver retries the loser, which then sees the new state.
2. **Claim before send.** An action is allowed, then claimed with a conditional update on its own document, then sent. The stop cancels every allowed-but-unclaimed action in its transaction, so a claim and a stop conflict on that document.
3. **A receipt covers a fixed list.** The stop takes a sequence number from the shared counter. The receipt covers every action allowed under that permission before that number, and closes when each one is applied, shown to have had no effect, or cancelled.

`tests/sandbox` checks the database behaviour this rests on. `tests/gate` and `tests/concurrency` drive two sessions through named interleavings.

## MongoDB features used

Multi-document transactions with snapshot reads and majority writes, the driver's `with_transaction` retry, `$graphLookup` to find everything below a permission, a `$jsonSchema` validator on the audit collection, pipeline updates with `$$NOW` so every decision carries the server's time, and change streams that feed the page and tell workers to halt early.

## What the agents act on

- **A Kubernetes cluster.** Two Deployments in a local kind cluster. A rollback or a restart is one patch that also writes the action's id onto the Deployment, so the cluster's own history is the evidence. Workers hold no cluster access in StopLine mode. Only the sender does.
- **A traffic router** written for this project: its own process, its own SQLite store, a log keyed by action id. It can be cut off in the middle of a call.
- A mock cluster behind the same interface, for tests and as a fallback.

## Run it

Needs Python 3.12, [uv](https://docs.astral.sh/uv/), and a MongoDB Atlas cluster (any replica set). The real cluster target also needs Docker, kind and kubectl.

```sh
uv sync
export MONGODB_URI='mongodb+srv://...'      # or a line MONGODB_URI='...' in ~/.stopline_env

# real cluster target
kind create cluster --name stopline
demo-app/build.sh
k8s/up.sh
uv run python -m stopline                   # then open http://127.0.0.1:8785

# or, with no cluster
uv run python -m stopline --cluster mock
```

Keys on the page:

| Key | What it does |
|---|---|
| `1` | Baseline run: workers read a stop flag |
| `2` | StopLine run: every action passes the check |
| `S` | Stop |
| `K` | Cut the router off mid-call and kill worker two |
| `W` | Resume worker two from its saved plan |
| `R` | Restore the router and settle what is still unknown |
| `U` | Undo what has an inverse |
| `V` | Show the database rejecting a malformed audit entry |
| `M` | The measured numbers |

`--voice` speaks the receipt aloud (needs `ELEVENLABS_API_KEY`). With `GEMINI_API_KEY` set, a language model plans the repair and the check decides what may run; without it the repair agent follows a saved plan.

## Tests

```sh
scripts/test-all.sh                                   # every test directory
uv run pytest tests/e2e -o addopts=""                 # both demo runs, end to end
STOPLINE_E2E_CLUSTER=kind uv run pytest tests/e2e -o addopts=""   # the same on the real cluster
```

Tests create their own databases and drop them.

## Limits

- An action sent before the stop can still land after it. StopLine lists it and proves its outcome. It does not pull it back.
- Expiry is judged by the server's clock when the action is checked, not when the target acts.
- The shared counter is a deliberately hot document. It suits a bounded tree (16 permissions under one grant here), not thousands of workers.
- Only tools that go through the check are covered.
- The router is a test system, and the demo's timing is controlled by a test schedule that holds a worker at a named point. The page labels a held step.

## Layout

```
src/stopline/contract.py   shared names, record shapes, interfaces
src/stopline/db.py         the one place that opens a connection
src/stopline/gate/         the check, the stop, receipts, undo
src/stopline/targets/      router, cluster adapters, the sender
src/stopline/agents/       repair agent, workers, the stage behind the page's keys
src/stopline/view/         the page
src/stopline/proof/        the schedule runner behind the numbers
src/stopline/report/       the reviewer's report
src/stopline/voice/        spoken receipt
src/stopline/llm.py        language-model planner
src/stopline/app.py        wires it together
k8s/, demo-app/            the cluster target
```
