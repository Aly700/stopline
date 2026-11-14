# Demo targets and sender

Start the router in its own process:

```sh
uv run python -m stopline.targets.router --port 8766 --store /tmp/stopline-router.sqlite
```

`--store` defaults to `stopline-router.sqlite` in the system temporary directory.
The router binds only to `127.0.0.1`. It needs no database connection.

```python
from stopline.targets import build_targets, build_sender, reset_all

targets = build_targets("mock")
sender = build_sender(gate, targets)
claim = sender.claim(action_id)
if claim.claimed:
    effect = sender.send(action_id)
```

Only a successful claim made by that sender instance permits a delivery attempt.
A timeout or connection failure leaves the effect `sent`. A new sender can call
`reconcile(action_id)` after the target becomes reachable. No automatic delivery
retry is needed: reconciliation reads the target's own record or installs a fence.
Baseline workers may call adapters directly, as specified by the shared contract.

`build_targets("kind")` imports `KubeCluster` from `.kube` only when requested.
`build_targets("mock")` works without that module or any Kubernetes installation.

The router's SQLite transaction commits its weights, action fingerprint and log
entry together. A fingerprint includes tool, target and arguments. Same-id replays
return the original result; changed arguments are refused. Rejected actions and
fences are also durable records, so a delayed request cannot later take effect.
Evidence includes `action_id`, `system`, `at_epoch`, `applied`, `reason` and
`log_id`. Successful shifts also contain `before` and `weights`.

**Fence race:** `fence(id)` returns an existing record if delivery won the race
between lookup and fence. The caller must inspect `evidence["applied"]`; if true,
record `applied`, not `rejected`. An absent id becomes a durable rejected record
with `fenced: true`. A later send cannot create another effect for either kind
of record.

Traffic inverses set the previous region-B weight. `Inverse.expect` is
`{"weights": {"region-a": ..., "region-b": ...}, "last_action_id": "forward-id"}`,
matching the state left by the forward action. Checking the id also refuses an
undo after later actions move the weights away and then back. The same condition is included in `Inverse.args["expect"]`
so the router checks it atomically with the undo, even if state changes after the
gate checks it. Mock rollback and restart have no inverse.

`cut()` arms one request: the router commits it, withholds its answer, and closes
subsequent ordinary connections without an HTTP response. The `/restore` and
`/reset` control endpoints remain available on the same listener. Restore resumes
ordinary requests and preserves evidence. Reset restores 100/0 weights and clears
the log and fences. The mock cluster resets checkout-api to v2 and checkout-worker
to v1. Call `reset_all(targets)` between runs after workers and sends have stopped.

The HTTP surface is GET `/state`, GET `/lookup/{action_id}`, and POST `/send`,
`/fence`, `/cut`, `/restore`, `/reset`. Send takes the serialized shared `Action`;
fence takes `{"action_id": "..."}`. Controls take `{}`.

Run the bounded tests, which launch isolated router processes on ephemeral ports:

```sh
uv run pytest tests/targets -o addopts=""
```

These tests use their own small fake GateAPI. They exercise the actual sender and
targets, including killing a sender process after target commit. They do not
claim to verify the real gate's MongoDB transactions, stop logic, or receipts.
