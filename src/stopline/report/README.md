# Saved run review

This package is optional. It does not import the gate, agents, targets or main
page. Importing it opens no database connection and registers no routes.

```python
from stopline.report import build_report, render_report, report_routes

report = build_report(db, run_id)
html = render_report(report)
app.include_router(report_routes(db))
```

The router adds `GET /report/{run_id}` for HTML and
`GET /api/report/{run_id}` for JSON. A missing run gives 404.

```sh
uv run python -m stopline.report RUN_ID
uv run python -m stopline.report RUN_ID --out run.html
uv run python -m stopline.report --out latest.html
```

Without an ID, the command selects the latest `runs.created_at`, using the ID
as a deterministic tie-breaker. It exits with status 1 if no run is found.
The command obtains its database only through `stopline.db`.

`build_report` reads the seven contract collections in one read-only snapshot
using `stopline.db.run_transaction`. It raises `LookupError("Run not found")`
for a missing run and returns a JSON-ready dictionary:

- `run`: the saved run, including its mode and holds.
- `permissions`: an indented tree represented as nested `children` lists, with
  scope, limits, expiry and stop attribution.
- `actions`: admissions and refused actions in server-time order, with frozen
  admission chains, policy references, evidence and undo links. An allow entry
  whose action record is missing remains visible with an unknown outcome.
- `refusals`: every deny entry, including repeated requests for the same action.
- `stops`: each stop's original receipt and fixed covered list, reconstructed
  outcomes, and later refusals kept separately. Missing receipts stay visible.
- `checks`: six named results with `id`, `name`, `passed`, `applicable`,
  `violations` and the offending `action_ids`.
- `policies`, `tasks`, `audit`: the stored policy definitions, saved work and
  chronological audit entries. Dates are UTC ISO strings; ObjectIds are strings.

Counter uniqueness means separate allocating operations under one root:
hand-down, admission and stop. Copies of the same operation in an action,
audit entry and receipt share a counter. Claims, outcomes, undo annotations
and refusals observe existing counters. Different roots may use equal values.
Both action records and allow entries are checked against stop counters.

`applied` becomes completed, `sent` becomes unknown, `cancelled` becomes blocked,
and `rejected` becomes `no effect` in JSON. The page calls the last outcome
“no change” to keep its vocabulary plain. An admitted action that has not yet
been sent is unknown, with that distinction stated in the evidence column.
Closed receipts must contain only terminal states, so an unfinished admitted
action fails that check too.

Baseline runs explicitly say “No check before actions”; permission guarantees
are marked not applicable. The six checks compare the saved records. They do
not contact targets or prove that the entire database has never been rewritten.

The HTML has inline styles, native expandable evidence and full record details,
and internal receipt/undo links. It needs no scripts, network or external files.
The short policy hashes in rows refer to the full hashes below the checks.
The JSON retains the original storage vocabulary and policy text; the page
translates storage vocabulary for the reader.

Run the database tests with `uv run pytest tests/report -o addopts=""`.
Generate both review fixtures with `uv run python -m tests.report.preview`.
Both use fresh `stopline_test_report_*` databases and drop them in `finally`.
The HTML fixtures are written only to the ignored `.lane/` directory.
