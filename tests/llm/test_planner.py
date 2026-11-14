import asyncio
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import threading
import time
import traceback

import pytest

from stopline.contract import (
    TARGET_API, TARGET_REGION_B, TARGET_WORKER, Tool, WORKER_ONE, WORKER_TWO,
)


INCIDENT = "checkout-api version v2 is failing; the last good version is v1"
FAKE_KEY = "local-test-credential"
GOOD = {
    WORKER_ONE: [
        {"tool": "rollback", "target": TARGET_API,
         "args": {"to_version": "v1"}, "why": "Restore the last good version."},
        {"tool": "restart", "target": TARGET_WORKER,
         "args": {}, "why": "Reconnect workers after the rollback."},
    ],
    WORKER_TWO: [
        {"tool": "shift_traffic", "target": TARGET_REGION_B,
         "args": {"percent": 90}, "why": "Move traffic away from the bad release."},
    ],
}


def envelope(raw):
    return {"candidates": [{"content": {"parts": [{"text": raw}]}, "finishReason": "STOP"}]}


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)
    monkeypatch.delenv("STOPLINE_MODEL", raising=False)


@pytest.fixture
def server(isolated):
    class Fake:
        raw = json.dumps(GOOD)
        status = 200
        get_delay = 0
        post_delay = 0
        drip_delay = 0
        response = None
        requests = []
        pages = [{"models": [
            {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-99.0-flash-image", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-99.0-flash", "supportedGenerationMethods": ["embedContent"]},
        ]}]

    fake = Fake()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            fake.requests.append(("GET", self.path, dict(self.headers), None))
            time.sleep(fake.get_delay)
            page = fake.pages[min(sum(r[0] == "GET" for r in fake.requests) - 1, len(fake.pages) - 1)]
            self.reply(page)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            fake.requests.append(("POST", self.path, dict(self.headers), body))
            time.sleep(fake.post_delay)
            self.reply(fake.response if fake.response is not None else envelope(fake.raw))

        def reply(self, payload):
            data = json.dumps(payload).encode()
            try:
                self.send_response(fake.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if fake.drip_delay:
                    for byte in data:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(fake.drip_delay)
                else:
                    self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    fake.url = f"http://127.0.0.1:{httpd.server_port}/v1beta"
    yield fake
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=1)


def planner(server):
    from stopline.llm import ModelPlanner
    return ModelPlanner(base_url=server.url)


def test_good_answer_and_90_percent_pass_through_unchanged(server):
    result = planner(server).plan(INCIDENT)
    assert result.steps == GOOD
    assert result.raw_text == server.raw
    assert result.model == "gemini-3.8-flash"
    assert 0 < result.seconds < 10

    method, path, headers, body = server.requests[-1]
    assert method == "POST"
    assert path == "/v1beta/models/gemini-3.8-flash:generateContent"
    assert headers["x-goog-api-key"] == FAKE_KEY
    assert all(FAKE_KEY not in request[1] for request in server.requests)
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    prompt = body["contents"][0]["parts"][0]["text"]
    for required in [INCIDENT, WORKER_ONE, WORKER_TWO, TARGET_API, TARGET_WORKER,
                     TARGET_REGION_B, *[tool.value for tool in Tool], "to_version", "percent", "why"]:
        assert required in prompt
    assert FAKE_KEY not in prompt


def test_caches_discovered_model_in_memory(server):
    subject = planner(server)
    subject.plan(INCIDENT)
    subject.plan(INCIDENT)
    assert [request[0] for request in server.requests] == ["GET", "POST", "POST"]


def test_explicit_model_skips_discovery(server, monkeypatch):
    monkeypatch.setenv("STOPLINE_MODEL", "models/gemini-2.5-flash")
    assert planner(server).plan(INCIDENT).model == "gemini-2.5-flash"
    assert [request[0] for request in server.requests] == ["POST"]


def test_model_discovery_follows_pages(server):
    server.pages = [{"models": [], "nextPageToken": "second"}, server.pages[0]]
    assert planner(server).plan(INCIDENT).model == "gemini-3.8-flash"
    assert "pageToken=second" in server.requests[1][1]


@pytest.mark.parametrize("raw", [
    "```json\n" + json.dumps(GOOD) + "\n```",
    "Here is your plan: " + json.dumps(GOOD),
    "{bad json}",
    json.dumps(GOOD) + " trailing text",
    '{"agent:worker-1": [], "agent:worker-1": [], "agent:worker-2": []}',
])
def test_bad_json_has_a_clean_fallback(server, raw):
    from stopline.llm import PlannerUnavailable
    server.raw = raw
    with pytest.raises(PlannerUnavailable, match="JSON"):
        planner(server).plan(INCIDENT)


@pytest.mark.parametrize("change", [
    lambda p: [],
    lambda p: {"steps": p},
    lambda p: {WORKER_ONE: p[WORKER_ONE]},
    lambda p: {**p, "agent:other": []},
    lambda p: {**p, WORKER_ONE: {}},
    lambda p: {**p, WORKER_ONE: p[WORKER_ONE] * 2},
    lambda p: {**p, WORKER_ONE: [None]},
    lambda p: {**p, WORKER_ONE: [{**p[WORKER_ONE][0], "tool": "delete_everything"}]},
    lambda p: {**p, WORKER_ONE: [{**p[WORKER_ONE][0], "tool": []}]},
    lambda p: {**p, WORKER_ONE: [{**p[WORKER_ONE][0], "target": "unknown/service"}]},
    lambda p: {**p, WORKER_ONE: [{**p[WORKER_ONE][0], "target": "cluster/"}]},
    lambda p: {**p, WORKER_ONE: [{**p[WORKER_ONE][0], "target": "cluster"}]},
    lambda p: {**p, WORKER_ONE: [{**p[WORKER_ONE][0], "args": []}]},
    lambda p: {**p, WORKER_ONE: [{**p[WORKER_ONE][0], "why": "two\nlines"}]},
    lambda p: {**p, WORKER_ONE: [{**p[WORKER_ONE][0], "why": "trailing newline\n"}]},
    lambda p: {**p, WORKER_ONE: [{**p[WORKER_ONE][0], "why": ""}]},
    lambda p: {**p, WORKER_ONE: [{**p[WORKER_ONE][0], "extra": "no"}]},
    lambda p: {**p, WORKER_ONE: [{k: v for k, v in p[WORKER_ONE][0].items() if k != "why"}]},
])
def test_wrong_shape_is_unavailable(server, change):
    from stopline.llm import PlannerUnavailable
    server.raw = json.dumps(change(deepcopy(GOOD)))
    with pytest.raises(PlannerUnavailable):
        planner(server).plan(INCIDENT)


@pytest.mark.parametrize("percent", [-1, 101, 25.0, True, "25", None])
def test_shift_percent_must_be_an_integer_in_range(server, percent):
    from stopline.llm import PlannerUnavailable
    value = deepcopy(GOOD)
    value[WORKER_TWO][0]["args"]["percent"] = percent
    server.raw = json.dumps(value)
    with pytest.raises(PlannerUnavailable, match="percent"):
        planner(server).plan(INCIDENT)


def test_missing_shift_percent_is_unavailable(server):
    from stopline.llm import PlannerUnavailable
    value = deepcopy(GOOD)
    value[WORKER_TWO][0]["args"] = {}
    server.raw = json.dumps(value)
    with pytest.raises(PlannerUnavailable, match="percent"):
        planner(server).plan(INCIDENT)


def test_permissions_are_left_to_the_gate(server):
    value = deepcopy(GOOD)
    value[WORKER_TWO] = value[WORKER_ONE]
    value[WORKER_TWO][0]["target"] = "cluster/another-deployment"
    server.raw = json.dumps(value)
    assert planner(server).plan(INCIDENT).steps == value


def test_missing_key_fails_without_network(server, monkeypatch):
    from stopline.llm import PlannerUnavailable
    monkeypatch.delenv("GEMINI_API_KEY")
    started = time.monotonic()
    with pytest.raises(PlannerUnavailable, match="key"):
        planner(server).plan(INCIDENT)
    assert time.monotonic() - started < 1
    assert server.requests == []


def test_reads_only_key_assignment_from_file(server, monkeypatch, tmp_path):
    monkeypatch.delenv("GEMINI_API_KEY")
    (tmp_path / ".stopline_env").write_text(
        "MONGODB_URI='unused-test-value'\nGEMINI_API_KEY='" + FAKE_KEY + "'\n"
    )
    assert planner(server).plan(INCIDENT).steps == GOOD


def test_env_key_wins_without_opening_file(server, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("should not read the file")
    monkeypatch.setattr(Path, "open", forbidden)
    assert planner(server).plan(INCIDENT).steps == GOOD


@pytest.mark.parametrize("status", [302, 400, 401, 429, 500])
def test_http_failure_never_discloses_key(server, status, caplog, capsys):
    from stopline.llm import PlannerUnavailable
    caplog.set_level(logging.DEBUG)
    server.status = status
    server.response = {"error": {"message": FAKE_KEY}}
    with pytest.raises(PlannerUnavailable) as caught:
        planner(server).plan(INCIDENT)
    rendered = "".join(traceback.format_exception(caught.value))
    captured = capsys.readouterr()
    assert FAKE_KEY not in rendered + caplog.text + captured.out + captured.err
    assert len(server.requests) == 1  # no retries


@pytest.mark.parametrize("response", [
    {}, {"candidates": []}, {"candidates": [{}]},
    {"candidates": [{"content": {"parts": [{"text": "{}"}]}, "finishReason": "MAX_TOKENS"}]},
    {"candidates": [{"content": {"parts": [{"text": 5}]}, "finishReason": "STOP"}]},
])
def test_malformed_api_response_is_unavailable(server, response):
    from stopline.llm import PlannerUnavailable
    server.response = response
    with pytest.raises(PlannerUnavailable):
        planner(server).plan(INCIDENT)


def test_no_usable_model_is_unavailable(server):
    from stopline.llm import PlannerUnavailable
    server.pages = [{"models": [{"name": "models/embedding", "supportedGenerationMethods": ["embedContent"]}]}]
    with pytest.raises(PlannerUnavailable, match="model"):
        planner(server).plan(INCIDENT)


def test_network_failure_is_unavailable(isolated):
    from stopline.llm import ModelPlanner, PlannerUnavailable
    # Reserve then close a local port, so connection refusal needs no remote server.
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = httpd.server_port
    httpd.server_close()
    started = time.monotonic()
    with pytest.raises(PlannerUnavailable):
        ModelPlanner(base_url=f"http://127.0.0.1:{port}/v1beta").plan(INCIDENT)
    assert time.monotonic() - started < 10


def test_slow_server_obeys_real_ten_second_deadline(server):
    from stopline.llm import PlannerUnavailable
    server.post_delay = 11
    started = time.monotonic()
    with pytest.raises(PlannerUnavailable, match="deadline"):
        planner(server).plan(INCIDENT)
    assert 9.5 < time.monotonic() - started < 10.3


def test_discovery_and_generation_share_one_deadline(server, monkeypatch):
    from stopline import llm
    monkeypatch.setattr(llm, "DEADLINE_SECONDS", 0.5)
    server.get_delay = 0.3
    server.post_delay = 0.3
    started = time.monotonic()
    with pytest.raises(llm.PlannerUnavailable, match="deadline"):
        planner(server).plan(INCIDENT)
    assert time.monotonic() - started < 0.7
    assert [request[0] for request in server.requests] == ["GET", "POST"]


def test_trickled_bytes_cannot_reset_deadline(server, monkeypatch):
    from stopline import llm
    monkeypatch.setattr(llm, "DEADLINE_SECONDS", 0.3)
    server.drip_delay = 0.05
    started = time.monotonic()
    with pytest.raises(llm.PlannerUnavailable, match="deadline"):
        planner(server).plan(INCIDENT)
    assert time.monotonic() - started < 0.5
    assert len(server.requests) == 1


def test_sync_entry_point_works_inside_an_event_loop(server):
    async def call():
        return planner(server).plan(INCIDENT)
    assert asyncio.run(call()).steps == GOOD


@pytest.mark.parametrize("percent", [0, 100])
def test_percent_boundaries_are_valid(server, percent):
    value = deepcopy(GOOD)
    value[WORKER_TWO][0]["args"]["percent"] = percent
    server.raw = json.dumps(value)
    assert planner(server).plan(INCIDENT).steps == value


@pytest.mark.parametrize("value", ["NaN", "Infinity", "1e999"])
def test_nonfinite_numbers_are_not_json_arguments(server, value):
    from stopline.llm import PlannerUnavailable
    server.raw = json.dumps(GOOD).replace('"v1"', value)
    with pytest.raises(PlannerUnavailable, match="JSON"):
        planner(server).plan(INCIDENT)


def test_model_cannot_echo_key_into_cli_output(server):
    from stopline.llm import PlannerUnavailable
    server.raw = json.dumps(GOOD).replace("Restore the last good version.", FAKE_KEY)
    with pytest.raises(PlannerUnavailable, match="credential") as caught:
        planner(server).plan(INCIDENT)
    assert FAKE_KEY not in str(caught.value)


def test_changed_key_rediscovers_models(server, monkeypatch):
    subject = planner(server)
    subject.plan(INCIDENT)
    monkeypatch.setenv("GEMINI_API_KEY", "another-local-credential")
    subject.plan(INCIDENT)
    assert [request[0] for request in server.requests] == ["GET", "POST", "GET", "POST"]


def test_cli_prints_result_json(server, monkeypatch, capsys):
    from stopline import llm
    subject = planner(server)
    monkeypatch.setattr(llm, "ModelPlanner", lambda: subject)
    assert llm.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert output["steps"] == GOOD
    assert output["model"] == "gemini-3.8-flash"
    assert output["raw_text"] == server.raw


def test_module_cli_reports_missing_key(isolated, monkeypatch):
    import subprocess
    import sys

    monkeypatch.delenv("GEMINI_API_KEY")
    result = subprocess.run([sys.executable, "-m", "stopline.llm"],
                            capture_output=True, text=True, timeout=2)
    assert result.returncode == 1
    assert "key" in json.loads(result.stdout)["unavailable"]
    assert not result.stderr
