import json
import re
from urllib.parse import urlsplit

from pages_helpers import DOCS, FORBIDDEN, KEYS, ROOT, References, recording


def test_recording_contains_both_runs_and_every_key_in_order():
    data = recording()
    assert data["version"] == 1
    assert [step["key"] for step in data["keys"]] == KEYS
    assert {run["mode"] for run in data["runs"]} == {"flag", "stopline"}
    assert len(data["runs"]) == 2
    assert set(data["states"]) == {run["id"] for run in data["runs"]}
    for step in data["keys"]:
        assert "ok" in step["answer"]
        assert step["frames"]
        times = [frame["ms"] for frame in step["frames"]]
        assert times == sorted(times) and times[0] >= 0
        pairs = [json.dumps([frame["state"], frame["targets"]], sort_keys=True)
                 for frame in step["frames"]]
        assert all(left != right for left, right in zip(pairs, pairs[1:]))


def test_recorded_evidence_covers_the_demonstration():
    data = recording()
    baseline = data["keys"][1]["frames"][-1]["state"]
    assert any(row.get("ran_after_stop") for row in baseline["rows"])
    opened = data["keys"][6]["frames"][-1]["state"]
    assert opened["receipt"]["status"] == "open"
    assert any(row["kind"] == "refusal" and row["principal"] == "agent:worker-2"
               for row in opened["rows"])
    closed = data["keys"][8]["frames"][-1]["state"]
    assert closed["receipt"]["status"] == "closed"
    assert any(row.get("undo") == "undone" for row in closed["receipt"]["rows"])
    for step in data["keys"]:
        for frame in step["frames"]:
            for row in frame["state"]["rows"] + (frame["state"].get("receipt") or {}).get("rows", []):
                assert row["action_id"] in data["chains"]


def test_capture_contains_no_forbidden_strings():
    recording()
    for filename in ("recording.json", "report.html"):
        value = (DOCS / filename).read_text()
        for token in FORBIDDEN:
            assert token not in value, f"Unsafe captured content in {filename}"


def test_report_does_not_request_a_favicon_at_the_host_root():
    assert '<link rel="icon" href="data:,">' in (DOCS / "report.html").read_text()


def test_page_references_exist_and_are_relative():
    assert (DOCS / "index.html").is_file()
    html = (DOCS / "index.html").read_text()
    external = {"https://github.com/Aly700/stopline", "https://www.youtube.com/watch?v=2440YfUVDkE"}
    for tag, value in References(html).references:
        if tag == "a" and value in external:
            continue
        if value == "data:,":  # The original console's empty, embedded favicon.
            continue
        parsed = urlsplit(value)
        assert not parsed.scheme and not parsed.netloc and not value.startswith("/"), value
        assert (DOCS / (parsed.path or ".")).exists(), value
    assert html.index('src="replay.js"') < html.index('src="app.js"')
    assert "Recorded run from 14 November 2026." in html
    assert "Nothing on this page is live: it replays states captured from a real run." in html
    assert (DOCS / ".nojekyll").read_bytes() == b""
    for filename in ("app.js", "style.css", "pages.css", "replay.js"):
        text = (DOCS / filename).read_text()
        assert not re.search(r"['\"`](/(?:api|report)/)", text)
        assert not re.search(r"(?:https?:)?//[^\s]+\.(?:js|css|woff2?)", text)


def test_copied_assets_only_change_urls():
    assert (DOCS / "app.js").is_file()
    source = ROOT / "src" / "stopline" / "view"
    expected = (source / "app.js").read_text().replace("'/api/", "'api/").replace("'/report/", "'report/")
    assert (DOCS / "app.js").read_text() == expected
    assert (DOCS / "style.css").read_bytes() == (source / "style.css").read_bytes()
