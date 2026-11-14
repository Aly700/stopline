"""Lets a worker process use things that live in the main process.

The main process mounts sender_routes(sender) on its web app. A worker builds
RemoteSender() and uses it wherever a Sender is expected, so a worker in
stopline mode never calls a target system itself.

The in-process mock cluster is shared the same way: target_routes(name, adapter)
in the main process, RemoteTarget(name) in a baseline worker, which calls the
target directly with no check in front of it. That is what the baseline is.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.encoders import jsonable_encoder

from .contract import (
    PAGE_PORT, Action, Claim, Lookup, NotClaimed, Sender, SendResult, TargetAdapter, TargetTimeout,
    TargetUnreachable,
)

PREFIX = "/internal/sender"


def sender_routes(sender: Sender) -> APIRouter:
    routes = APIRouter(prefix=PREFIX)

    @routes.post("/claim/{action_id}")
    def claim(action_id: str) -> Any:
        return jsonable_encoder(asdict(sender.claim(action_id)))

    @routes.post("/send/{action_id}")
    def send(action_id: str) -> Any:
        try:
            return jsonable_encoder(sender.send(action_id))
        except NotClaimed as refused:
            raise HTTPException(status_code=409, detail=str(refused)) from None

    @routes.post("/reconcile/{action_id}")
    def reconcile(action_id: str) -> Any:
        return jsonable_encoder(sender.reconcile(action_id))

    return routes


class RemoteSender:
    """A Sender that forwards each call to the main process. Dates in a returned effect are ISO strings."""

    def __init__(self, base_url: str = f"http://127.0.0.1:{PAGE_PORT}", timeout: float = 30.0) -> None:
        self._http = httpx.Client(base_url=base_url + PREFIX, timeout=timeout)

    def _post(self, call: str, action_id: str) -> Any:
        answer = self._http.post(f"/{call}/{action_id}")
        if answer.status_code == 409:
            raise NotClaimed(answer.json().get("detail", action_id))
        answer.raise_for_status()
        return answer.json()

    def claim(self, action_id: str) -> Claim:
        return Claim(**self._post("claim", action_id))

    def send(self, action_id: str) -> dict[str, Any]:
        return self._post("send", action_id)

    def reconcile(self, action_id: str) -> dict[str, Any]:
        return self._post("reconcile", action_id)


def target_routes(name: str, adapter: TargetAdapter) -> APIRouter:
    """Expose one target adapter of the main process to worker processes, unchecked."""
    routes = APIRouter(prefix=f"/internal/target/{name}")

    def answer(call: Any) -> Any:
        try:
            return jsonable_encoder(asdict(call()))
        except TargetTimeout as lost:
            raise HTTPException(status_code=504, detail=str(lost)) from None
        except TargetUnreachable as down:
            raise HTTPException(status_code=503, detail=str(down)) from None

    @routes.post("/send")
    def send(action: dict[str, Any]) -> Any:
        return answer(lambda: adapter.send(Action(**action)))

    @routes.post("/lookup/{action_id}")
    def lookup(action_id: str) -> Any:
        return answer(lambda: adapter.lookup(action_id))

    return routes


class RemoteTarget:
    """Enough of a TargetAdapter for a baseline worker: send and lookup, forwarded to the main process."""

    def __init__(self, name: str, base_url: str = f"http://127.0.0.1:{PAGE_PORT}", timeout: float = 15.0) -> None:
        self.name = name
        self._http = httpx.Client(base_url=f"{base_url}/internal/target/{name}", timeout=timeout)

    def _post(self, path: str, body: Any = None) -> Any:
        try:
            answer = self._http.post(path, json=body)
        except httpx.TimeoutException as lost:
            raise TargetTimeout(str(lost)) from None
        except httpx.RequestError as down:
            raise TargetUnreachable(str(down)) from None
        if answer.status_code == 504:
            raise TargetTimeout(answer.json().get("detail", ""))
        if answer.status_code == 503:
            raise TargetUnreachable(answer.json().get("detail", ""))
        answer.raise_for_status()
        return answer.json()

    def send(self, action: Action) -> SendResult:
        return SendResult(**self._post("/send", asdict(action)))

    def lookup(self, action_id: str) -> Lookup:
        return Lookup(**self._post(f"/lookup/{action_id}"))
