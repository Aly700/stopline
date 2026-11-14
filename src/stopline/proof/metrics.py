"""Pure reductions of stored records and target-owned logs.

Sequence numbers order authority decisions. Target clocks and the persisted local
stop marker order sends/effects on this one host. Effect.sent_at is a claim time;
it is deliberately NOT used as evidence of transport dispatch.
"""

from collections import Counter
from statistics import median
from typing import Any

COUNT_KEYS = (
    'admitted_after_stop', 'started_after_stop_unsent',
    'sent_before_stop_landed_after', 'missing_from_receipt',
    'duplicate_effects', 'closed_without_evidence',
)
INVARIANTS = ('admitted_after_stop', 'missing_from_receipt', 'duplicate_effects', 'closed_without_evidence')
DEFINITIONS = {
    'admitted_after_stop': 'Distinct effects in the stopped chain with admitted_seq > stop_seq. Baselines: not applicable.',
    'started_after_stop_unsent': 'Distinct applied action IDs whose first target apply is after stop_at_local and have no transport send at or before that marker. A claim is not a transport send.',
    'sent_before_stop_landed_after': 'Distinct applied action IDs whose first target apply is after stop_at_local and have a transport send at or before it. Reported separately.',
    'missing_from_receipt': 'Distinct (receipt, effect) pairs in the stopped chain with admission before stop_seq absent from covered. Missing receipt counts every pre-stop effect in that chain.',
    'duplicate_effects': 'Sum of max(0, actual applied target log entries per (system, action_id) - 1). Replayed responses are not apply entries.',
    'closed_without_evidence': 'Closed receipts with an omitted covered action, missing effect, nonterminal effect, or no matching target evidence. Cancelled requires no dispatch and no target application.',
    'added_delay_ms': 'Per matched action attempt: max(0, authority check + claim wall time minus fixed-mode check time). Median and maximum of pooled samples, excluding holds, target I/O, setup, stop and result persistence. Fakes are not an Atlas latency benchmark.',
    'stop_marker': 'runs.stop_at_local recorded after stop returns (or flag is set), on the same host as targets. Authority order uses Mongo sequence, never client timestamps.',
}


def _has_evidence(effect: dict, logs: list[dict], sends: list[dict]) -> bool:
    action_id = effect['_id']
    relevant = [row for row in logs if row['action_id'] == action_id]
    if effect['state'] == 'cancelled':
        return not any(s['action_id'] == action_id for s in sends) and not any(
            row.get('event') == 'applied' for row in relevant)
    event = {'applied': 'applied', 'rejected': 'fenced'}.get(effect['state'])
    evidence = effect.get('evidence') or {}
    system = effect.get('target', '').split('/', 1)[0]
    if event is None or evidence.get('action_id') != action_id or evidence.get('system') != system:
        return False
    return any(row.get('event') == event and row.get('system') == system
               and row.get('at_epoch') == evidence.get('at_epoch') for row in relevant)


def measure(data: dict[str, Any]) -> dict[str, int | None]:
    effects = {e['_id']: e for e in data['effects']}
    logs, sends = data['target_logs'], data['sends']
    stop = data['run'].get('stop_at_local')
    applications = [row for row in logs if row.get('event') == 'applied']
    frequencies = Counter((row['system'], row['action_id']) for row in applications)
    first_apply: dict[str, float] = {}
    for row in applications:
        key = row['action_id']
        first_apply[key] = min(first_apply.get(key, float('inf')), row['at_epoch'])
    late = {key for key, at in first_apply.items() if stop is not None and at > stop}
    before = {row['action_id'] for row in sends if stop is not None and row['at_epoch'] <= stop}
    counts = dict.fromkeys(COUNT_KEYS, 0)
    counts['duplicate_effects'] = sum(max(0, count - 1) for count in frequencies.values())
    counts['started_after_stop_unsent'] = len(late - before)
    counts['sent_before_stop_landed_after'] = len(late & before)
    if data['run']['mode'] != 'stopline':
        for key in ('admitted_after_stop', 'missing_from_receipt', 'closed_without_evidence'):
            counts[key] = None
        return counts
    admitted_late = set()
    for receipt in data['receipts']:
        subtree = [e for e in effects.values() if receipt['mandate_id'] in e.get('chain', [])]
        expected = {e['_id'] for e in subtree
                    if e.get('admitted_seq') is not None and e['admitted_seq'] < receipt['stop_seq']}
        admitted_late.update(e['_id'] for e in subtree
                             if e.get('admitted_seq') is not None and e['admitted_seq'] > receipt['stop_seq'])
        covered = set(receipt['covered'])
        missing = expected - covered
        counts['missing_from_receipt'] += len(missing)
        if receipt['status'] == 'closed' and (missing or any(
            key not in effects or not _has_evidence(effects[key], logs, sends) for key in covered
        )):
            counts['closed_without_evidence'] += 1
    counts['admitted_after_stop'] = len(admitted_late)
    if stop is not None and not data['receipts']:
        # The run's stop sequence is captured independently of the receipt list.
        stop_seq = data['run'].get('stop_seq')
        mandate_id = data['run'].get('agent_mandate_id')
        relevant = [e for e in effects.values() if mandate_id in e.get('chain', [])]
        if stop_seq is not None:
            counts['missing_from_receipt'] = sum(e['admitted_seq'] < stop_seq for e in relevant)
            counts['admitted_after_stop'] = sum(e['admitted_seq'] > stop_seq for e in relevant)
    return counts


def aggregate(rows: list[dict]) -> dict:
    totals = {}
    controls = {r['name']: r for r in rows if r['mode'] == 'fixed' and r['status'] != 'skipped'}
    for mode in ('stopline', 'flag', 'fixed'):
        selected = [r for r in rows if r['mode'] == mode and r.get('counts') is not None]
        counts = {}
        for key in COUNT_KEYS:
            values = [r['counts'][key] for r in selected if r['counts'][key] is not None]
            counts[key] = sum(values) if values else None
        samples = []
        for row in selected:
            control = controls.get(row['name'])
            if control is None:
                continue
            base = {t['key']: t['duration_ms'] for t in control['evidence']['timings']}
            for sample in row['evidence']['timings']:
                if sample['key'] in base:
                    samples.append(max(0.0, sample['duration_ms'] - base[sample['key']]))
        totals[mode] = {
            **counts,
            'added_delay_ms': {'median': round(median(samples), 4) if samples else None,
                               'worst': round(max(samples), 4) if samples else None,
                               'samples': len(samples)},
        }
    return totals
