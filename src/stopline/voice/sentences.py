"""Short spoken statements derived only from the receipt's stored facts."""

from collections import Counter
from collections.abc import Iterable

from stopline.contract import AuditEntry, Effect, Receipt, Tool


# Exactly the clips produced for the demo's open receipt, its close, and flag run.
DEMO_SENTENCES = (
    'Stop confirmed. One completed. One unknown. Two blocked.',
    'The receipt is open.',
    'The router answered. The traffic shift was applied.',
    'The receipt is closed.',
    'Stop pressed. One action ran after the stop.',
)


def _number(count: int) -> str:
    words = ('no', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight',
             'nine', 'ten', 'eleven', 'twelve', 'thirteen', 'fourteen', 'fifteen', 'sixteen')
    return words[count] if 0 <= count < len(words) else str(count)


def _chunks(sentences: list[str]) -> list[str]:
    """Keep each audio clip short; never split a sentence in the middle."""
    chunks: list[str] = []
    for sentence in sentences:
        if chunks and len((chunks[-1] + ' ' + sentence).split()) <= 8:
            chunks[-1] += ' ' + sentence
        else:
            chunks.append(sentence)
    return chunks


def sentences_for_receipt(
    receipt: Receipt,
    effects: Iterable[Effect],
    refusals: Iterable[AuditEntry] = (),
    *,
    closing: bool = False,
) -> list[str]:
    """Return short clips in speaking order.

    Blocked means a cancelled action or a distinct refusal after this stop.
    A refused retry of a covered action is not counted a second time. Missing
    covered records remain unknown. ``closing`` selects the later announcement.
    """
    covered = set(receipt['covered'])
    actions = {e['_id']: e for e in effects if e['_id'] in covered}
    if closing and receipt['status'] == 'closed':
        shifts = [e for e in actions.values() if e.get('tool') == Tool.SHIFT
                  and e.get('target', '').startswith('router/')]
        if len(shifts) == 1 and shifts[0]['state'] in {'applied', 'rejected'}:
            outcome = 'applied' if shifts[0]['state'] == 'applied' else 'refused'
            return _chunks(['The router answered.', f'The traffic shift was {outcome}.',
                            'The receipt is closed.'])
        return ['The receipt is closed.']

    counts: Counter[str] = Counter()
    labels = {'applied': 'completed', 'cancelled': 'blocked', 'rejected': 'refused'}
    for action_id in covered:
        counts[labels.get(actions.get(action_id, {}).get('state'), 'unknown')] += 1
    denied = {
        row['action_id'] for row in refusals
        if row.get('action_id') and row['action_id'] not in covered
        and row.get('kind') == 'deny' and row.get('run_id') == receipt['run_id']
        and row.get('mandate_id') in receipt['revoked']
        and row.get('at') is not None and row['at'] >= receipt['stopped_at']
    }
    counts['blocked'] += len(denied)
    sentences = ['Stop confirmed.']
    for label in ('completed', 'unknown', 'blocked', 'refused'):
        if counts[label]:
            sentences.append(f'{_number(counts[label]).capitalize()} {label}.')
    status = 'closed' if receipt['status'] == 'closed' else 'open'
    sentences.append(f'The receipt is {status}.')
    return _chunks(sentences)


def sentence_for_baseline(count: int) -> str:
    """Count completed target actions whose own clock is after the stop."""
    if count <= 0:
        return 'Stop pressed. No completed action after the stop.'
    noun = 'action' if count == 1 else 'actions'
    return f'Stop pressed. {_number(count).capitalize()} {noun} ran after the stop.'
