"""Finite measurements for M. Only explicitly real component results are shown.

Run with STOPLINE_ROUTER_PORT=8796 before importing stopline, then
python -m stopline.proof --out results.json. The CLI owns an isolated router
process, uses a fresh proof scratch database, and publishes only proof_results.
Fake fixtures remain available for local tests and are never shown on the page.

Bounds: two worker routines, two steps each, operator root at depth zero,
repair agent at one, workers at two; four holds in both worker orders. This is
a named interleaving experiment, not exhaustive thread scheduling. Baseline
counterparts of claim/delegation/retry/receipt holds pause before dispatch; they
do not acquire authority features. --backend fake is only an offline fixture.
All real target times and the local stop marker use this host's clock. Authority
order uses Atlas sequence numbers. Intermediate receipt snapshots prevent a
later reconciliation from concealing a premature close. The JSON retains the
source records, definitions, schedule checks and provenance for recomputation.
"""


def summary_numbers(result):
    """N = named schedules with stored evidence for both flag and StopLine.

    K and Z count schedules, not actions: at least one target application after
    the local stop marker with no dispatch before it, for flag and StopLine.
    L counts StopLine actions dispatched before stop but first applied after it.
    Coverage is checked by action ID against stored receipts, not assumed.
    Counterexamples include every StopLine late-start schedule in the result.
    """
    from stopline.proof.metrics import measure

    rows = {(r['mode'], r['name']): r for r in result['rows']
            if r['status'] != 'skipped' and 'evidence' in r
            and r['evidence']['run'].get('stop_at_local') is not None}
    names = sorted({name for mode, name in rows if mode == 'flag' and ('stopline', name) in rows})
    values = {'N': len(names), 'K': 0, 'Z': 0, 'L': 0, 'all_covered': True, 'counterexamples': []}
    values['counterexamples'] = sorted(name for (mode, name), row in rows.items()
                                       if mode == 'stopline' and measure(row['evidence'])['started_after_stop_unsent'] > 0)
    for name in names:
        flag = measure(rows['flag', name]['evidence'])
        data = rows['stopline', name]['evidence']
        stopline = measure(data)
        values['K'] += int(flag['started_after_stop_unsent'] > 0)
        values['Z'] += int(stopline['started_after_stop_unsent'] > 0)
        values['L'] += stopline['sent_before_stop_landed_after']
        stop = data['run']['stop_at_local']
        early = {s['action_id'] for s in data['sends'] if s.get('dispatched', True) and s['at_epoch'] <= stop}
        first = {}
        for entry in data['target_logs']:
            if entry['event'] == 'applied':
                key = entry['action_id']
                first[key] = min(first.get(key, float('inf')), entry['at_epoch'])
        inflight = {key for key, at in first.items() if at > stop and key in early}
        covered = {key for receipt in data['receipts'] for key in receipt['covered']}
        values['all_covered'] &= inflight <= covered
    return values


def summary(db) -> list[str]:
    """Three short measured sentences from the latest REAL result, or no lines."""
    result = db['proof_results'].find_one({'producer': 'real'}, sort=[('created_at', -1), ('_id', -1)])
    if result is None:
        return []
    values = summary_numbers(result)
    n, k, z, landed = (values[key] for key in ('N', 'K', 'Z', 'L'))
    if not n:
        return []
    coverage = 'all on the receipt' if values['all_covered'] else 'NOT all on the receipt'
    prefix = '' if result.get('complete') else 'Incomplete: '
    return [
        f'{prefix}{n} stop schedules, each run with a stop flag and with StopLine.',
        f'Stop flag: an action started after the stop in {k} of {n}.',
        f'StopLine: {z} of {n}. Sent before the stop and landed after: {landed}, {coverage}.',
    ]


__all__ = ['summary']
