"""Read-only projections of the shared records. No local event history."""
from datetime import datetime, timezone

from bson import ObjectId

from stopline.contract import action_id_for


COLLECTIONS = ('effects', 'audit', 'receipts', 'mandates', 'tasks')
OUTCOMES = {'admitted': 'waiting', 'sent': 'unknown', 'applied': 'completed',
            'rejected': 'no effect', 'cancelled': 'blocked'}


def serial(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or timezone.utc).isoformat()
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, dict):
        return {str(key): serial(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [serial(item) for item in value]
    return value


def epoch(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or timezone.utc).timestamp()
    return float(value or 0)


def permission_chain(permission_id, permissions):
    chain, seen = [], set()
    while permission_id in permissions and permission_id not in seen:
        seen.add(permission_id)
        permission = permissions[permission_id]
        chain.append(permission)
        permission_id = permission.get('parent_id')
    return list(reversed(chain))


def action_row(effect, run, permissions, held):
    state = effect['state']
    row = dict(effect, action_id=effect['_id'], kind='action', outcome=OUTCOMES.get(state, 'unknown'),
               held=effect['_id'] in held, at=effect.get('admitted_at'), seq=effect.get('admitted_seq'))
    row['chain'] = [permissions[mid] for mid in effect.get('chain', []) if mid in permissions]
    evidence = effect.get('evidence')
    target_time = (evidence or {}).get('at_epoch')
    boundary = run.get('stop_at_local')
    started = target_time if target_time is not None else epoch(effect.get('admitted_at'))
    row['target_at_epoch'] = target_time
    row['ran_after_stop'] = bool(
        run['mode'] in ('flag', 'fixed') and boundary is not None
        and (target_time is not None or not evidence) and started > boundary)
    if row['ran_after_stop']:
        row['outcome'] = 'RAN AFTER STOP'
    row['reconciled'] = False
    return row


def refusal_row(entry, run, permissions, held):
    detail = entry.get('detail') or {}
    return dict(action_id=entry.get('action_id'), id=str(entry['_id']), kind='refusal', outcome='refused',
                principal=entry['principal'], tool=detail.get('tool', 'action'), target=detail.get('target', ''),
                args=detail.get('args', {}), reason=entry['reason'], detail=detail, at=entry['at'], seq=entry.get('seq'),
                chain=permission_chain(entry.get('mandate_id'), permissions), held=entry.get('action_id') in held,
                state=None, evidence=None, undo=None, policy_version=entry.get('policy_version'))


def read_state(db):
    # A single Atlas round trip, including the current run, not one read per row.
    pipeline = [{'$sort': {'created_at': -1, '_id': -1}}, {'$limit': 1}]
    pipeline += [{'$lookup': {'from': name, 'localField': '_id', 'foreignField': 'run_id', 'as': name}}
                 for name in COLLECTIONS]
    bundle = next(iter(db['runs'].aggregate(pipeline)), None)
    if bundle is None:
        return dict(run=None, rows=[], omitted_rows=0, stop=None, receipt=None, workers=[], permissions=[])
    records = {name: bundle.pop(name, []) for name in COLLECTIONS}
    run = bundle
    permissions = {row['_id']: row for row in records['mandates']}
    held = {action_id_for(run['_id'], item['principal'], item['step']) for item in run.get('holds', [])}
    effects = {row['_id']: row for row in records['effects']}
    rows = [action_row(row, run, permissions, held) for row in effects.values()]
    by_action = {row['action_id']: row for row in rows}
    for entry in records['audit']:
        if entry['kind'] != 'deny' or not entry.get('action_id'):
            continue
        existing = by_action.get(entry['action_id'])
        if existing is None:
            rows.append(refusal_row(entry, run, permissions, held))
        elif existing['outcome'] == 'blocked':
            # A refused claim on an action that already has its own row: say why on that row.
            existing['refusal'] = dict(reason=entry['reason'], detail=entry.get('detail') or {})
    receipts = sorted(records['receipts'], key=lambda row: row['stop_seq'])
    receipt = next((row for row in receipts if row['_id'] == run.get('receipt_id')), receipts[-1] if receipts else None)
    stop = (dict(seq=receipt['stop_seq'], at=receipt['stopped_at'], by=receipt['stopped_by']) if receipt else
            dict(seq=None, at=run['stop_at'], by='person:operator') if run['mode'] != 'stopline' and run.get('stop_at') else None)
    for row in rows:
        if run['mode'] == 'stopline':
            row['after_stop'] = bool(stop and (row.get('seq') or 0) >= (stop['seq'] or 0))
            row['_order'] = (row.get('seq') or 0, epoch(row.get('at')), row['action_id'])
        else:
            target_time = (row.get('evidence') or {}).get('at_epoch')
            when = target_time if target_time is not None else epoch(row.get('at'))
            row['after_stop'] = row.get('ran_after_stop', False)
            row['_order'] = (when, row['action_id'])
    rows.sort(key=lambda row: row['_order'])
    for row in rows:
        row.pop('_order')
    receipt_view = None
    if receipt:
        covered = [action_row(effects[aid], run, permissions, held) for aid in receipt['covered'] if aid in effects]
        covered.sort(key=lambda row: (row.get('seq') or 0, row['action_id']))
        receipt_rows = {row['action_id']: row for row in covered}
        revoked = set(receipt['revoked'])
        for entry in sorted(records['audit'], key=lambda row: epoch(row['at'])):
            if entry['kind'] != 'deny' or not entry.get('action_id'):
                continue
            if entry.get('mandate_id') not in revoked or epoch(entry['at']) < epoch(receipt['stopped_at']):
                continue
            row = refusal_row(entry, run, permissions, held)
            if row['action_id'] not in receipt_rows:
                receipt_rows[row['action_id']] = row   # shown as refused, the same word as in the actions list
            elif receipt_rows[row['action_id']]['outcome'] == 'blocked':
                receipt_rows[row['action_id']].update(reason=entry['reason'], detail=entry['detail'])
        # Missing covered records are visibly unknown; never infer closure in this layer.
        for aid in receipt['covered']:
            if aid not in receipt_rows:
                receipt_rows[aid] = dict(action_id=aid, tool='action', target=aid, args={}, principal='',
                    outcome='unknown', chain=[], state=None, evidence=None, undo=None)
        receipt_view = dict(receipt, rows=list(receipt_rows.values()))
    # The stage saves this note only after it has looked up outstanding sends.
    # Pair it with target evidence and the stored finish time; a closed receipt
    # alone does not establish that an action was reconciled.
    checked_router = any(task.get('note') == 'Restored the router and checked the outstanding sends.'
                         for task in records['tasks'])
    if receipt and checked_router:
        for row in rows + receipt_view['rows']:
            effect = effects.get(row['action_id'], {})
            evidence = effect.get('evidence') or {}
            row['reconciled'] = bool(
                row['action_id'] in receipt['covered'] and effect.get('state') == 'applied'
                and evidence.get('system') == 'router' and evidence.get('action_id') == row['action_id']
                and epoch(effect.get('finished_at')) > epoch(receipt['stopped_at']))
    return serial(dict(run=run, rows=rows[-8:], omitted_rows=max(0, len(rows) - 8), stop=stop,
                       receipt=receipt_view, workers=records['tasks'], permissions=list(permissions.values())))


def read_chain(db, action_id):
    effect = db['effects'].find_one({'_id': action_id})
    audit = list(db['audit'].find({'action_id': action_id}).sort('at', 1))
    if effect is None and not audit:
        return None
    run_id = effect['run_id'] if effect else audit[0]['run_id']
    permissions = {row['_id']: row for row in db['mandates'].find({'run_id': run_id})}
    if effect and effect.get('chain'):
        chain = [permissions[mid] for mid in effect['chain'] if mid in permissions]
    else:
        chain = permission_chain(audit[-1].get('mandate_id'), permissions) if audit else []
    version = effect.get('policy_version') if effect else audit[-1].get('policy_version')
    policy = db['policies'].find_one({'_id': version}) if version is not None else None
    return serial(dict(action_id=action_id, effect=effect, permissions=chain, policy=policy, audit=audit))
