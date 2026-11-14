"""A finite, ordered catalogue; no random sampling or unstated repetitions."""

from dataclasses import dataclass

from stopline.contract import DEMO_PLAN, WORKER_ONE, WORKER_TWO, HoldPoint


@dataclass(frozen=True)
class Schedule:
    name: str
    kind: str
    order: tuple[str, str] = (WORKER_ONE, WORKER_TWO)
    worker: str = WORKER_ONE
    step: int = 0
    point: str = HoldPoint.BEFORE_CHECK
    first: str = ''


def schedules() -> tuple[Schedule, ...]:
    cases = []
    for order in ((WORKER_ONE, WORKER_TWO), (WORKER_TWO, WORKER_ONE)):
        order_name = '12' if order[0] == WORKER_ONE else '21'
        for worker, steps in DEMO_PLAN.items():
            for step in range(len(steps)):
                for point in HoldPoint:
                    name = f'hold/{order_name}/{worker.split(":")[1]}/{step}/{point}'
                    cases.append(Schedule(name, 'hold', order, worker, step, point))
    for kind, operation in (('parent_stop', 'admit'), ('hand_down', 'grant')):
        for first in ('stop', operation):
            cases.append(Schedule(f'{kind}/{first}-first', kind, first=first))
    cases.extend(Schedule(kind, kind) for kind in (
        'expiry', 'retry', 'paused_send', 'lost_reply', 'router_restart',
        'late_duplicate', 'receipt_closure',
    ))
    return tuple(cases)


BOUNDS = {
    'workers': 2,
    'steps_per_worker': 2,
    'tree_depth': 2,
    'root_depth': 0,
    'tree': 'operator root -> repair agent -> two workers',
    'hold_points': [str(point) for point in HoldPoint],
    'worker_orders': ['12', '21'],
    'repetitions': 1,
    'scope': 'Named interleavings only; not an exhaustive concurrency or distributed-clock proof.',
}
