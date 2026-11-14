import os
os.environ['STOPLINE_ROUTER_PORT'] = '8796'

from stopline.contract import Action
from stopline.proof.real import RouterProcess, URL
from stopline.targets.router import RouterAdapter


def test_owned_router_restart_preserves_its_actual_ledger(tmp_path):
    process = RouterProcess(tmp_path)
    try:
        process.start()
        target = RouterAdapter(URL)
        action = Action('restart-proof', 'run', 'mandate', 'worker', 'shift_traffic', 'router/region-b', {'percent': 25})
        first = target.send(action)
        process.restart()
        assert target.lookup(action.action_id).evidence == first.evidence
        target.send(action)
        assert len(process.logs()) == 1
    finally:
        process.stop()
