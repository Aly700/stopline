import asyncio
from html.parser import HTMLParser

import httpx
from fastapi import FastAPI

from stopline.report import build_report, render_report, report_routes

from .records import RUN


class Text(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.parts = []
        self.feed(html)

    def handle_data(self, data):
        self.parts.append(data)


def test_page_is_self_contained_and_uses_plain_words(demo):
    html = render_report(build_report(demo, RUN))
    text = " ".join(Text(html).parts).lower()
    assert html.startswith("<!doctype html>")
    assert "<script" not in html.lower()
    assert "<link" not in html.lower()
    assert "@import" not in html.lower()
    assert "@media print" in html
    for word in ("permission", "stop", "refused", "receipt", "completed", "blocked", "pass"):
        assert word in text
    assert "mandate" not in text
    assert "effect" not in text
    assert "25%" in text and "50%" in text
    assert "closed" in text
    assert 'href="#action-' in html  # undo links resolve inside the standalone page.


def test_page_escapes_record_content(demo):
    demo.effects.update_one({"_id": "rollback"}, {"$set": {"evidence": {"system": "<script>alert(1)</script>", "action_id": "rollback", "at_epoch": 42}}})
    html = render_report(build_report(demo, RUN))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_baseline_has_no_permission_checks(demo):
    demo.runs.update_one({"_id": RUN}, {"$set": {"mode": "flag", "root_mandate_id": None, "agent_mandate_id": None, "receipt_id": None}})
    for name in ("mandates", "audit", "receipts", "policies"):
        demo[name].delete_many({})
    demo.effects.update_many({}, {"$set": {"mode": "flag", "root_id": None, "mandate_id": None, "chain": [], "admitted_seq": None, "policy_version": None, "policy_hash": None}})
    report = build_report(demo, RUN)
    assert report["permissions"] == []
    assert all(not check["applicable"] for check in report["checks"])
    assert all(action["decision"] == "unchecked" for action in report["actions"])
    html = render_report(report)
    assert "no check before actions" in html.lower()
    assert "No permissions were recorded" in html
    assert "unchecked" in html


def test_routes_return_html_json_and_404(demo):
    app = FastAPI()
    app.include_router(report_routes(demo))

    async def requests():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            page = await client.get(f"/report/{RUN}")
            assert page.status_code == 200
            assert page.headers["content-type"].startswith("text/html")
            assert "StopLine" in page.text
            data = await client.get(f"/api/report/{RUN}")
            assert data.status_code == 200
            assert data.json()["run"]["_id"] == RUN
            assert len(data.json()["checks"]) == 6
            assert (await client.get("/report/missing")).status_code == 404
            assert (await client.get("/api/report/missing")).status_code == 404

    asyncio.run(requests())
