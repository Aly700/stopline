# Finite schedule measurements

Run `uv run python -m stopline.proof --out results.json`. The default backend is
**fake**, explicitly labelled in the JSON, table, and M-key summary. It uses a
serial protocol fake for the gate and sender, a fake cluster, and a fake router
whose independent SQLite journal survives reopening its database connection.
It does not start a router HTTP process, a real worker, or Kubernetes.

The CLI creates a scratch database with `stopline.db.test_database("proof")`,
batches the source records into it, reads them back to compute counts, and drops
it in `finally`. The only write to the destination database (default `stopline`)
is one document in `proof_results`. `--database NAME` changes that destination.
`--offline` runs the same fake schedules and writes JSON without accessing Atlas.
`summary(db)` reads the latest stored result and always returns three strings.

There are 43 schedule names and 129 mode/schedule combinations. Thirty-two names
place a stop at each of four hold points of each worker step in both worker
orders. The other eleven cover both orders of parent stop/admission and hand-down/
stop, expiry, retry, paused dispatch, lost reply, router restart, late duplication,
and receipt closure with a fenced late arrival. Every applicable schedule runs
once. Twenty-four baseline combinations are explicitly skipped: baselines have
no claim, hand-down, transaction retry, or receipt operations.

This is a bounded interleaving model: two worker routines, two steps each, root at
depth zero, repair agent at depth one, workers at depth two. It is not exhaustive
thread scheduling, a network model, or a proof of real MongoDB transactions. The
fake retry discards an authority read, stops, then checks again. Expiry advances
the fake gate's clock. A router restart reopens its persistent SQLite connection.

Authority counters use the stored chain and admission/stop sequence numbers.
Target counters use the target's own apply journal and the persisted local stop
marker. A sender-boundary journal records dispatch separately: `Effect.sent_at`
is the claim time and cannot establish that the request left before the stop.
Consequently StopLine can have target starts after stop following a pre-stop
claim. Those are shown as late unsent starts. A request dispatched before stop
and applied after it is shown separately as in-flight. No such arrival is hidden.
Target application is the execution-start boundary in this instant-apply model;
requests that are deduplicated or fenced do not start another effect.

Receipt closure requires terminal effects with matching independent target
evidence, or a cancelled effect that was never claimed, dispatched or applied.
Checks inspect intermediate stored snapshots as well as the final state, so a
premature closure cannot disappear after reconciliation. Failed connections are
not evidence of dispatch. Missing evidence, missing required schedules, and
unexercised holds/faults cannot produce a passing report. Baselines
have no admissions or receipts, so those fields are `null`, not zero. Added delay
is paired check-plus-claim wall time above the fixed-mode check for the same
schedule and worker step, clamped at zero. Median and worst pool matched samples;
setup, holds, target I/O, stops, and result persistence are excluded. Fake timings
are measured, but do not predict Atlas gate latency. Timing samples vary between
runs; safety counts are the repeatability assertion.

Run `uv run pytest tests/proof -o addopts=""` for the local checks. Set
`STOPLINE_PROOF_ATLAS=1` on that command to include the bounded scratch-storage
round trip and verify that the scratch database has no collections after drop.
The reported elapsed field includes scratch cleanup and excludes final JSON
and publication; the CLI also prints total command wall time including those
operations and uses that total for its six-minute exit-status check.

The JSON includes every source snapshot, counter definition, named schedule,
status, skip reason, and backend provenance. Recompute with
`stopline.proof.runner.finalize(result)` after loading the JSON. It never trusts
previously computed counters.

After the implementations are available, the factory seam is
`backend.py:build_runtime`: `build_gate(db)`,
`build_targets(cluster="mock", router_url=...)`, and `build_sender(gate, targets)`.
`--backend real` selects these factories and never silently falls back to fakes.
The real path still needs expiry/retry hooks, a router process restart controller,
and complete target-owned journals with `event`, `action_id`, `system`, and
`at_epoch`; unsupported schedules are explicitly skipped. The current branch
has no gate or targets packages, so integrated numbers are not yet available.
The real factory rejects a non-proof scratch database or a non-loopback router.
It restores injected faults on exceptional exits. Unsupported required schedules
make the result incomplete even when every measured counter happens to be zero.
