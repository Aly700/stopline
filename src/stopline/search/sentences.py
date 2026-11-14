"""Deterministic descriptions of stored audit events; no generated answers."""
from datetime import datetime

NAMES = {'person:operator': 'Operator', 'agent:repair': 'Repair agent',
         'agent:worker-1': 'Worker one', 'agent:worker-2': 'Worker two'}
REASONS = {
    'ok': 'permission refused', 'revoked': 'permission stopped',
    'expired': 'permission expired', 'unknown_mandate': 'permission not found',
    'tool_not_allowed': 'action outside permission', 'target_not_allowed': 'target outside permission',
    'limit_exceeded': 'permission limit reached', 'wider_than_parent': 'permission wider than its parent',
    'tree_too_large': 'permission tree too large', 'same_id_different_args': 'action id reused with different arguments',
    'cancelled_by_stop': 'blocked by the stop', 'not_admitted': 'action was not allowed to be sent',
    'not_claimed': 'action was not claimed for sending', 'not_authorised': 'permission refused',
    'no_undo': 'no undo', 'state_changed_since': 'target changed since the action',
    'fenced': 'stopped in time at the target', 'target_unreachable': 'target unreachable',
}


def person(value):
    return NAMES.get(value, value or 'Unknown')


def clock(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return 'Unknown time'
    return value.strftime('%H:%M:%S') if isinstance(value, datetime) else 'Unknown time'


def action_words(record):
    target = (record.get('target') or '').rsplit('/', 1)[-1]
    args = record.get('args') or {}
    kind = record.get('tool')
    if kind == 'rollback':
        return f'roll back {target}'
    if kind == 'restart':
        return f'restart {target}'
    if kind == 'shift_traffic':
        return f"shift {args.get('percent', '?')}% to region B"
    return 'perform the action'


def sentence_for(entry, effect=None):
    """Describe one event, using effect data only to fill missing action details.

    Permission chains may contain resolved mandate dicts. Unresolved ids are
    omitted rather than guessing who held them. Times use the record's zone.
    """
    detail = entry.get('detail') or {}
    effect = effect or {}
    action = action_words({**effect, **detail})
    who = person(entry.get('principal'))
    prefix = f"{clock(entry.get('at'))} {who}"
    kind = entry['kind']
    if kind == 'allow':
        chain = detail.get('chain') or effect.get('chain') or []
        holders = [person(item.get('holder')) for item in chain if isinstance(item, dict) and item.get('holder')]
        under = ' under ' + ' → '.join(holders) if holders else ''
        replay = ' again for the same action' if detail.get('replayed') else ''
        return f'{prefix} was allowed to {action}{under}{replay}.'
    if kind == 'deny':
        if detail.get('operation') == 'stop':
            action = 'stop the permission'
        elif detail.get('holder') and not entry.get('action_id'):
            action = f"hand permission to {person(detail['holder'])}"
        reason = REASONS.get(entry.get('reason'), 'permission refused')
        if detail.get('revoked_by'):
            reason = f"stopped by {person(detail['revoked_by'])}"
            if detail.get('revoked_at'):
                reason += f" at {clock(detail['revoked_at'])}"
        return f'{prefix} asked to {action}. Refused: {reason}.'
    if kind == 'hand_down':
        return f"{prefix} handed permission to {person(detail.get('holder'))}."
    if kind == 'stop':
        holder = detail.get('holder')
        permission = f"the {person(holder)}'s permission" if holder else 'the permission'
        covered = detail.get('covered', [])
        count = len(covered)
        return f"{prefix} stopped {permission}; {count} {'action' if count == 1 else 'actions'} on the receipt."
    if kind == 'claim':
        owner = person(effect.get('principal')) if effect.get('principal') else who
        return f"{prefix} claimed {owner}'s action to {action} for sending; its outcome was still unknown."
    if kind == 'outcome':
        state = detail.get('state')
        outcome = {'applied': 'completed', 'rejected': 'had no effect',
                   'cancelled': 'was blocked before sending'}.get(state, 'had an unknown outcome')
        return f"{prefix}'s action to {action} {outcome}."
    if kind == 'undo':
        # The undo event records admission, not successful reversal.
        original = detail.get('reverses') or effect.get('reverses')
        suffix = f' for action {original}' if original else ''
        return f'{prefix} requested undo of the action to {action}{suffix}.'
    raise ValueError('Unknown audit event kind.')
