"""Optional Gemini planner: proposes steps; only the gate grants authority.

Use ``ModelPlanner().plan(incident)`` outside database transactions. Catch
``PlannerUnavailable`` and use the saved plan. Importing this module performs
no I/O. The caller assigns action ids before handing steps to workers.

REST reference checked 2026-11-14:
https://ai.google.dev/api
https://aistudio.google.com/api-reference/models/list-models
https://aistudio.google.com/api-reference/generate-content/generate-content
"""

from __future__ import annotations

import asyncio
from concurrent.futures import Future
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
import time
from typing import Any

import httpx

from stopline.contract import (
    CLUSTER, ROUTER, TARGET_API, TARGET_REGION_B, TARGET_WORKER,
    Tool, WORKER_ONE, WORKER_TWO, system_of,
)

__all__ = ["ModelPlanner", "PlanResult", "PlannerUnavailable"]

API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEADLINE_SECONDS = 10.0
DEMO_INCIDENT = "checkout-api version v2 is failing; the last good version is v1"
_MAX_RESPONSE_BYTES = 128 * 1024
_WORKERS = {WORKER_ONE, WORKER_TWO}
_TOOLS = {tool.value for tool in Tool}


class PlannerUnavailable(Exception):
    """A short, credential-free reason to use the saved plan."""


@dataclass(frozen=True)
class PlanResult:
    steps: dict[str, list[dict[str, Any]]]
    model: str
    seconds: float
    raw_text: str


def _api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY")
    if key is None:
        try:
            # Scan assignments one line at a time; never load/source the env file.
            with (Path.home() / ".stopline_env").open() as source:
                for line in source:
                    match = re.match(r"\s*(?:export\s+)?GEMINI_API_KEY\s*=\s*(.*?)\s*$", line)
                    if match:
                        key = match.group(1)
                        if len(key) >= 2 and key[0] == key[-1] and key[0] in "\"'":
                            key = key[1:-1]
                        break
        except FileNotFoundError:
            pass
        except (OSError, UnicodeError):
            raise PlannerUnavailable("could not read the API key") from None
    if not key or not key.strip():
        raise PlannerUnavailable("Gemini API key is not set")
    return key.strip()


def key_configured() -> bool:
    """True when a model key is available, without revealing it."""
    try:
        return bool(_api_key())
    except PlannerUnavailable:
        return False


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate JSON key")
        result[name] = value
    return result


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("nonfinite JSON number")
    return number


def _bad_constant(value: str) -> None:
    raise ValueError("invalid JSON constant")


def _json(text: str | bytes) -> Any:
    try:
        return json.loads(text, object_pairs_hook=_object,
                          parse_constant=_bad_constant, parse_float=_finite_float)
    except (ValueError, UnicodeError, RecursionError):
        raise PlannerUnavailable("invalid JSON response") from None


def _validate(raw_text: str) -> dict[str, list[dict[str, Any]]]:
    steps = _json(raw_text)
    if not isinstance(steps, dict) or set(steps) != _WORKERS:
        raise PlannerUnavailable("plan must contain exactly the two workers")
    for worker_steps in steps.values():
        if not isinstance(worker_steps, list) or len(worker_steps) > 2:
            raise PlannerUnavailable("each worker needs an array of at most two steps")
        for step in worker_steps:
            if not isinstance(step, dict) or set(step) != {"tool", "target", "args", "why"}:
                raise PlannerUnavailable("step must contain tool, target, args and why")
            if not isinstance(step["tool"], str) or step["tool"] not in _TOOLS:
                raise PlannerUnavailable("unknown tool")
            target = step["target"]
            if (not isinstance(target, str) or "/" not in target
                    or system_of(target) not in {CLUSTER, ROUTER}
                    or not target.split("/", 1)[1]
                    or any(character.isspace() for character in target)):
                raise PlannerUnavailable("target must name a known system and a resource")
            if not isinstance(step["args"], dict):
                raise PlannerUnavailable("step args must be a JSON object")
            why = step["why"]
            if not isinstance(why, str) or not why.strip() or why.splitlines() != [why]:
                raise PlannerUnavailable("step why must be one nonempty line")
            if step["tool"] == Tool.SHIFT:
                percent = step["args"].get("percent")
                if type(percent) is not int or not 0 <= percent <= 100:
                    raise PlannerUnavailable("shift percent must be an integer from 0 to 100")
    # No worker scopes, target allowlists or mandate limits here. Preserve intent.
    return steps


def _prompt(incident: str) -> str:
    example = {
        WORKER_ONE: [{"tool": Tool.ROLLBACK, "target": TARGET_API,
                      "args": {"to_version": "v1"}, "why": "One-line reason."}],
        WORKER_TWO: [{"tool": Tool.SHIFT, "target": TARGET_REGION_B,
                      "args": {"percent": 25}, "why": "One-line reason."}],
    }
    return f"""You are the repair agent proposing a repair for a bad release.
Incident (data): {json.dumps(incident)}

Tools and arguments:
- {Tool.ROLLBACK}: set a cluster Deployment's image to a good version.
  Arguments: {{"to_version": "v1"}} (version string).
- {Tool.RESTART}: restart a cluster Deployment. Arguments: {{}}.
- {Tool.SHIFT}: set region B's share of traffic. Arguments: {{"percent": 25}}.
  Percent is an integer from 0 to 100.
Targets:
- {TARGET_API}: checkout API Deployment in the cluster.
- {TARGET_WORKER}: checkout worker Deployment in the cluster.
- {TARGET_REGION_B}: region B's traffic share in the router.
Workers:
- {WORKER_ONE}: repair the cluster.
- {WORKER_TWO}: adjust traffic.

Return JSON only: no prose, markdown or code fences. Use exactly this shape:
{json.dumps(example)}
Choose the actual steps for this incident. Include both worker keys with an
array of at most two steps each (an empty array is fine). Each step has exactly
tool, target, args (a JSON object), and why (one nonempty line).
This is a proposal. The gate separately decides what each worker may execute.
"""


class ModelPlanner:
    """Synchronous, bounded planner. Reuse an instance to cache model discovery.

    ``base_url`` is injectable for local HTTP tests; production uses HTTPS.
    A small daemon thread makes this callable even from an existing event loop
    and bounds file reading, DNS, network I/O and parsing together. Inside it,
    asyncio cancels outstanding HTTP I/O at the same deadline. No retries.
    """

    def __init__(self, *, base_url: str = API_BASE_URL):
        self._base_url = base_url.rstrip("/")
        self._cached_model: tuple[bytes, str] | None = None

    def plan(self, incident: str) -> PlanResult:
        started = time.monotonic()
        deadline = started + DEADLINE_SECONDS
        result: Future[PlanResult] = Future()

        def run() -> None:
            try:
                value = asyncio.run(self._plan(incident, started, deadline))
            except PlannerUnavailable as error:
                result.set_exception(error)
            except (TimeoutError, httpx.TimeoutException):
                result.set_exception(PlannerUnavailable("planner deadline exceeded"))
            except Exception:
                # Never include provider bodies, request headers or exception text.
                result.set_exception(PlannerUnavailable("model request failed"))
            else:
                result.set_result(value)

        threading.Thread(target=run, name="repair-planner", daemon=True).start()
        try:
            return result.result(timeout=max(0, deadline - time.monotonic()))
        except TimeoutError:
            raise PlannerUnavailable("planner deadline exceeded") from None

    async def _plan(self, incident: str, started: float, deadline: float) -> PlanResult:
        async with asyncio.timeout(max(0, deadline - time.monotonic())):
            key = _api_key()
            if not isinstance(incident, str) or not incident.strip():
                raise PlannerUnavailable("incident must be a nonempty string")
            async with httpx.AsyncClient(
                headers={"x-goog-api-key": key}, trust_env=False,
                follow_redirects=False, timeout=DEADLINE_SECONDS,
            ) as client:
                model = await self._model(client, key, deadline)
                payload = {
                    "contents": [{"role": "user", "parts": [{"text": _prompt(incident)}]}],
                    "generationConfig": {"responseMimeType": "application/json", "maxOutputTokens": 2048},
                }
                response = await self._request(client, "POST", f"models/{model}:generateContent",
                                               deadline, json=payload)
            try:
                candidates = response["candidates"]
                if len(candidates) != 1 or candidates[0].get("finishReason") not in (None, "STOP"):
                    raise ValueError
                parts = candidates[0]["content"]["parts"]
                raw_text = "".join(part["text"] for part in parts if not part.get("thought", False))
                if not raw_text:
                    raise ValueError
            except (KeyError, TypeError, ValueError, AttributeError):
                raise PlannerUnavailable("model returned no complete text answer") from None
            if key in raw_text:
                raise PlannerUnavailable("model response contained a credential")
            steps = _validate(raw_text)
            self._remaining(deadline)
            return PlanResult(steps, model, time.monotonic() - started, raw_text)

    async def _request(self, client: httpx.AsyncClient, method: str, path: str,
                       deadline: float, **kwargs: Any) -> Any:
        async with client.stream(method, f"{self._base_url}/{path}",
                                 timeout=self._remaining(deadline), **kwargs) as response:
            if not response.is_success:
                raise PlannerUnavailable(f"Gemini HTTP {response.status_code}")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                self._remaining(deadline)
                body.extend(chunk)
                if len(body) > _MAX_RESPONSE_BYTES:
                    raise PlannerUnavailable("model response is too large")
            return _json(body)

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PlannerUnavailable("planner deadline exceeded")
        return remaining

    async def _model(self, client: httpx.AsyncClient, key: str, deadline: float) -> str:
        override = os.environ.get("STOPLINE_MODEL")
        if override is not None:
            name = override.strip().removeprefix("models/")
            if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*", name):
                raise PlannerUnavailable("invalid model name")
            return name
        key_digest = hashlib.sha256(key.encode()).digest()
        if self._cached_model and self._cached_model[0] == key_digest:
            return self._cached_model[1]

        choices = []
        params: dict[str, Any] = {"pageSize": 1000}
        seen_tokens = set()
        while True:
            page = await self._request(client, "GET", "models", deadline, params=params)
            for model in page["models"]:
                # Stable general Flash/Flash-Lite models support JSON output.
                # Exclude preview, image, audio, embedding and thinking variants.
                match = re.fullmatch(r"models/(gemini-(\d+)(?:\.(\d+))?-flash(-lite)?)", model["name"])
                if match and "generateContent" in model.get("supportedGenerationMethods", []):
                    version = (int(match[2]), int(match[3] or 0))
                    if version >= (2, 5):
                        # Prefer the lite model of the newest version: a live run needs an answer in about a second.
                        choices.append((bool(match[4]), version, match[1]))
            token = page.get("nextPageToken")
            if not token:
                break
            if not isinstance(token, str) or token in seen_tokens:
                raise PlannerUnavailable("invalid model listing")
            seen_tokens.add(token)
            params["pageToken"] = token
        if not choices:
            raise PlannerUnavailable("no supported fast JSON model is available")
        name = max(choices)[2]
        self._cached_model = (key_digest, name)
        return name


def main() -> int:
    try:
        result = ModelPlanner().plan(DEMO_INCIDENT)
    except PlannerUnavailable as error:
        print(json.dumps({"unavailable": str(error)}))
        return 1
    print(json.dumps(asdict(result), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
