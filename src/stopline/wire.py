"""Lets a worker process use the sender that lives in the main process.

The main process mounts sender_routes(sender) on its web app. A worker builds
RemoteSender() and uses it wherever a Sender is expected, so the worker never
calls a target system itself.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.encoders import jsonable_encoder

from .contract import PAGE_PORT, Claim, NotClaimed, Sender

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
