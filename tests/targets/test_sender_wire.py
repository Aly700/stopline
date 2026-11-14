"""The shared worker HTTP wrapper driving the real sender and router."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from stopline.contract import NotClaimed
from stopline.targets import build_sender
from stopline.wire import PREFIX, RemoteSender, sender_routes
from .conftest import shift


def test_worker_http_calls_claim_then_deliver_and_read_evidence(gate, router):
    action = shift('run:agent:worker-2:0')
    gate.admit(action)
    app = FastAPI()
    app.include_router(sender_routes(build_sender(gate, {'router': router})))
    remote = RemoteSender()
    remote._http.close()
    with TestClient(app, base_url='http://testserver' + PREFIX) as client:
        remote._http = client
        with pytest.raises(NotClaimed):
            remote.send(action.action_id)
        assert remote.claim(action.action_id).claimed
        effect = remote.send(action.action_id)
        assert effect['state'] == 'applied'
        assert effect['evidence'] == router.lookup(action.action_id).evidence
        assert remote.reconcile(action.action_id)['state'] == 'applied'
