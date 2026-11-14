"""Small shared pieces of the two demo targets."""

import time

from stopline.contract import Action, Reason, SendResult, args_hash


def fingerprint(action: Action) -> str:
    return args_hash({'tool': action.tool, 'target': action.target, 'args': action.args})


def evidence(action_id: str, system: str, applied: bool, reason: str = Reason.OK, **detail) -> dict:
    return {'action_id': action_id, 'system': system, 'at_epoch': time.time(),
            'applied': applied, 'reason': reason, **detail}


def result(record: dict) -> SendResult:
    return SendResult(record['applied'], record, record.get('reason', Reason.OK))
