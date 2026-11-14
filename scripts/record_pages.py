#!/usr/bin/env python3
"""Capture an already running console; no database credentials are needed here.

Usage: uv run python scripts/record_pages.py --base http://127.0.0.1:8855
The instance must be dedicated to this capture: the keys start and stop runs.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

KEYS = ("1", "S", "2", "X", "K", "S", "W", "R", "U", "M", "V")
QUESTIONS = ("what was refused after the stop", "what did worker two do", "who stopped the run")
FORBIDDEN = ("mongodb", "sk-", "lsv2_", "AQ.", "/Users/")
POLL_SECONDS = 0.25
TIMEOUT_SECONDS = 180


class CaptureError(Exception):
    """Messages are deliberately independent of server bodies and credentials."""


def check_public(text):
    if any(token in text for token in FORBIDDEN):
        raise CaptureError("Captured content contains a forbidden string; no files were saved.")


class Recorder:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self.pool = ThreadPoolExecutor(max_workers=3)
        self.previous = None

    def request(self, path, *, post=False, html=False):
        try:
            request = Request(self.base + path, method="POST" if post else "GET")
            with urlopen(request, timeout=TIMEOUT_SECONDS if post else 30) as response:
                body = response.read().decode("utf-8")
            check_public(body)
            return body if html else json.loads(body)
        except (HTTPError, URLError, TimeoutError, ValueError):
            raise CaptureError("The console did not return a usable response; nothing was saved.") from None

    def sample(self):
        state = self.pool.submit(self.request, "/api/state")
        targets = self.pool.submit(self.request, "/api/targets")
        pair = {"state": state.result(), "targets": targets.result()}
        if pair["state"].get("pending"):
            raise CaptureError("State is pending; retry when the console is connected.")
        return pair

    def append_frame(self, step, pair, start):
        signature = json.dumps(pair, sort_keys=True)
        if signature != self.previous:
            step["frames"].append({"ms": round((time.monotonic() - start) * 1000), **pair})
            self.previous = signature

    @staticmethod
    def settled(index, state):
        ready = state.get("ready_key")
        workers = [worker for worker in state["workers"] if worker["principal"].startswith("agent:worker-")]
        finished = len(workers) == 2 and all(worker["status"] in {"done", "stopped", "killed"} for worker in workers)
        if index == 0:
            return ready == "S"
        if index == 1:
            return finished and any(row.get("ran_after_stop") for row in state["rows"])
        if index == 2:
            return ready == "K"  # X needs the permission created during startup.
        if index == 3:
            return ready == "K" and any(row.get("reason") == "limit_exceeded" and row.get("args", {}).get("percent") == 90 for row in state["rows"])
        if index == 4:
            return ready == "S"
        if index == 5:
            return ready == "W"
        if index == 6:
            return ready == "R" and finished
        if index == 7:
            return ready == "U" and state["receipt"]["status"] == "closed"
        if index == 8:
            return ready is None and state["receipt"]["status"] == "closed"
        return True

    def press(self, index):
        key = KEYS[index]
        step = {"key": key, "frames": []}
        # Finish each previous segment at the next readiness boundary. Never
        # retry a POST: an ambiguous response must not issue the command twice.
        start = time.monotonic()
        answer = self.pool.submit(self.request, "/api/key/" + key, post=True)
        operation = None
        last_operation = start
        stable_since = start
        while time.monotonic() - start < TIMEOUT_SECONDS:
            tick = time.monotonic()
            pair = self.sample()
            count = len(step["frames"])
            self.append_frame(step, pair, start)
            if len(step["frames"]) != count:
                stable_since = time.monotonic()
            if answer.done():
                step["answer"] = answer.result()
                if not step["answer"].get("ok"):
                    raise CaptureError(f"Key {key} was refused; no recording was saved.")
                # X has no operation route. Its refusal is confirmed in state.
                if step["answer"].get("pending") and key != "X" and operation is None and tick - last_operation >= .75:
                    result = self.request("/api/operation/" + key)
                    last_operation = tick
                    if not result.get("pending"):
                        if not result.get("ok"):
                            raise CaptureError(f"Key {key} failed; no recording was saved.")
                        operation = result
                        step["operation"] = result
                complete = not step["answer"].get("pending") or operation is not None or key == "X"
                if complete and self.settled(index, pair["state"]) and tick - start >= .75 and tick - stable_since >= .5:
                    return step
            time.sleep(max(0, POLL_SECONDS - (time.monotonic() - tick)))
        raise CaptureError(f"Timed out waiting after key {key}; no recording was saved.")

    def capture(self):
        # Starting a run has no ready_key; give the running instance a short
        # fixed pause before the first key, just as for the other unhinted keys.
        time.sleep(.75)
        initial = self.sample()
        # Keep each segment self-contained even when a key changes no records.
        data = {"version": 1, "recorded_at": datetime.now(timezone.utc).isoformat(),
                "initial": initial, "keys": []}
        run_ids = []
        for index, key in enumerate(KEYS):
            self.previous = None
            step = self.press(index)
            data["keys"].append(step)
            if key in {"1", "2"}:
                run_ids.append(step["frames"][-1]["state"]["run"]["_id"])
            print(f"Captured key {key}: {len(step['frames'])} frames.", flush=True)
        data["runs"] = [run for run in self.request("/api/runs") if run["id"] in run_ids]
        if len(data["runs"]) != 2:
            raise CaptureError("Both recorded runs must still be available.")
        data["states"] = {rid: self.request("/api/state?" + urlencode({"run_id": rid})) for rid in run_ids}
        # Use the complete accounting: state intentionally shows only eight rows.
        # Action ids are opaque and may not contain their run's id.
        action_ids = set()
        for rid in run_ids:
            report = self.request("/api/report/" + quote(rid, safe=""))
            action_ids.update(row["action_id"] for row in report["actions"] if row.get("action_id"))
        data["chains"] = {aid: self.request("/api/chain/" + quote(aid, safe="")) for aid in sorted(action_ids)}
        answers = {}
        for question in QUESTIONS:
            try:
                result = self.request("/api/ask?" + urlencode({"q": question, "run_id": run_ids[1]}))
            except CaptureError:
                continue
            if result.get("ok"):
                answers[question] = result
        if answers:
            data["ask"] = answers
        report_html = self.request("/report/" + quote(run_ids[1], safe=""), html=True)
        # Keep browsers from asking the static host's root for favicon.ico.
        report_html = report_html.replace("<head>", '<head><link rel="icon" href="data:,">', 1)
        return data, report_html


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="URL of a dedicated running console")
    parser.add_argument("--out", type=Path, default=Path("docs/recording.json"))
    args = parser.parse_args()
    base = urlsplit(args.base)
    if base.scheme not in {"http", "https"} or not base.hostname or base.username or base.password or base.query or base.fragment:
        parser.error("--base must be an HTTP URL without credentials, a query or a fragment")
    recorder = Recorder(args.base)
    try:
        data, report = recorder.capture()
        encoded = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        check_public(encoded)
        check_public(report)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(encoded, encoding="utf-8")
        args.out.with_name("report.html").write_text(report, encoding="utf-8")
        print("Saved the recording and its reviewer's report.")
    except CaptureError as error:
        parser.exit(1, str(error) + "\n")
    except Exception:
        parser.exit(1, "Capture failed; check the running console. No response details were logged.\n")
    finally:
        recorder.pool.shutdown(wait=True, cancel_futures=True)


if __name__ == "__main__":
    main()
