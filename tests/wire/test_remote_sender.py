from fastapi import FastAPI
from fastapi.testclient import TestClient

from stopline.contract import Claim, EffectState, NotClaimed, Reason
from stopline.wire import PREFIX, RemoteSender, sender_routes

import pytest


class FakeSender:
    def __init__(self):
        self.claimed = set()

    def claim(self, action_id):
        self.claimed.add(action_id)
        return Claim(claimed=True, action_id=action_id, state=EffectState.SENT)

    def send(self, action_id):
        if action_id not in self.claimed:
            raise NotClaimed(action_id)
        return {"_id": action_id, "state": EffectState.APPLIED}

    def reconcile(self, action_id):
        return {"_id": action_id, "state": EffectState.SENT}


def remote_over(app):
    remote = RemoteSender()
    remote._http = TestClient(app, base_url="http://testserver" + PREFIX)
    return remote


def test_a_worker_reaches_the_sender_over_http():
    app = FastAPI()
    app.include_router(sender_routes(FakeSender()))
    remote = remote_over(app)
    claim = remote.claim("a-1")
    assert claim.claimed and claim.reason == Reason.OK
    assert remote.send("a-1")["state"] == "applied"
    assert remote.reconcile("a-1")["state"] == "sent"


def test_sending_something_unclaimed_is_refused():
    app = FastAPI()
    app.include_router(sender_routes(FakeSender()))
    with pytest.raises(NotClaimed):
        remote_over(app).send("never-claimed")
