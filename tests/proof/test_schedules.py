from stopline.contract import DEMO_PLAN, HoldPoint
from stopline.proof.schedules import schedules


def test_every_step_hold_and_both_worker_orders_are_named():
    cases = schedules()
    holds = [s for s in cases if s.kind == 'hold']
    assert len(holds) == 32
    assert len({s.name for s in cases}) == len(cases)
    for worker, steps in DEMO_PLAN.items():
        for step in range(len(steps)):
            for point in HoldPoint:
                assert len([s for s in holds if (s.worker, s.step, s.point) == (worker, step, point)]) == 2


def test_faults_and_serialization_orders_are_present():
    cases = schedules()
    assert {s.kind for s in cases} >= {
        'parent_stop', 'hand_down', 'expiry', 'retry', 'paused_send',
        'lost_reply', 'router_restart', 'late_duplicate', 'receipt_closure',
    }
    assert len([s for s in cases if s.kind == 'parent_stop']) == 2
    assert len([s for s in cases if s.kind == 'hand_down']) == 2
