"""Persist source evidence in scratch collections, then read it back to count."""

import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4

COLLECTIONS = ('runs', 'effects', 'receipts', 'audit', 'mandates', 'proof_sends',
               'proof_timings', 'proof_observations', 'proof_target_logs')


def persist_and_reload(result, db):
    batches = {name: [] for name in COLLECTIONS}
    for row in result['rows']:
        if 'evidence' not in row:
            continue
        data = row['evidence']
        run_id = data['run']['_id']
        batches['runs'].append(data['run'])
        for name in ('effects', 'receipts', 'audit', 'mandates'):
            batches[name].extend(data[name])
        for name in ('sends', 'timings', 'observations'):
            batches[f'proof_{name}'].extend(data[name])
        batches['proof_target_logs'].extend(dict(log, _id=uuid4().hex, run_id=run_id) for log in data['target_logs'])
    for name, records in batches.items():
        if records:
            db[name].insert_many(records)
    reload_evidence(result, db)
    result['sources']['record_storage'] = 'persisted and reread from scratch database; fakes still generated the gate, sender and target records'


def reload_evidence(result, db):
    # One scan per collection, not one network round trip per action or schedule.
    by_run = {}
    for name in COLLECTIONS:
        for record in db[name].find():
            run_id = record['_id'] if name == 'runs' else record['run_id']
            by_run.setdefault(run_id, {}).setdefault(name, []).append(record)
    for row in result['rows']:
        if 'evidence' not in row:
            continue
        old_run = row['evidence']['run']['_id']
        records = by_run.get(old_run, {})
        # Missing run evidence is a failure, not permission to reuse cached data.
        row['evidence'] = {'run': records['runs'][0]}
        for name in COLLECTIONS[1:]:
            key = name.removeprefix('proof_')
            row['evidence'][key] = records.get(name, [])


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f'not serializable: {type(value).__name__}')


def write_json(path, result):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Serialize before opening the destination so a conversion error cannot truncate it.
    payload = json.dumps(result, default=_json_default, indent=2, sort_keys=True) + '\n'
    temporary = path.with_name(path.name + '.tmp')
    try:
        temporary.write_text(payload)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
