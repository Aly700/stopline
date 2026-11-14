"""Finite schedule evidence and the page's three-line M-key summary."""


def summary(db) -> list[str]:
    result = db['proof_results'].find_one(sort=[('created_at', -1), ('_id', -1)])
    if result is None:
        return ['No schedule results stored.', 'Run python -m stopline.proof --out results.json.',
                'Bounds: 2 workers, depth 2, 4 hold points.']
    totals = result['totals']
    stopline = totals['stopline']
    label = 'FAKE model' if result['backend'] == 'fake' else 'Integrated components'
    keys = ('admitted_after_stop', 'missing_from_receipt', 'duplicate_effects', 'closed_without_evidence')
    counters = '/'.join('n/a' if stopline.get(key) is None else str(stopline[key]) for key in keys)
    status = '' if result.get('complete') else ' INCOMPLETE;'
    return [
        f"{label}:{status} StopLine admitted/missing/duplicates/closed without evidence: {counters}.",
        f"After stop: flag {totals['flag']['started_after_stop_unsent']}, StopLine {stopline['started_after_stop_unsent']} unsent; {stopline['sent_before_stop_landed_after']} in-flight landed.",
        f"{len(result['passed'])} passed/{len(result['failed'])} failed/{len(result['skipped'])} skipped; "
        '2 workers, depth 2, 4 holds; added delay median/worst '
        f"{stopline['added_delay_ms']['median']}/{stopline['added_delay_ms']['worst']} ms.",
    ]


__all__ = ['summary']
