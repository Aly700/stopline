import asyncio
import json
from html import escape
from html.parser import HTMLParser

import httpx
import pytest
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


def summary_figures(html):
    summary = html.split('<dl class="summary">', 1)[1].split('</dl>', 1)[0]
    figures = {}
    for item in summary.split('<dt>')[1:]:
        label, value = item.split('</dt>', 1)
        value = value.split('<dd', 1)[1].split('>', 1)[1].split('</dd>', 1)[0]
        figures[label] = " ".join(" ".join(Text(value).parts).split())
    return figures


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
    assert '</h1><p>Rebuilt from the stored records only. Nothing on this page comes from the running program\'s memory.</p>' in html
    assert summary_figures(html) == {
        "Actions allowed": "4", "Actions refused": "1", "Actions unknown": "0",
        "Receipt": "CLOSED", "Checks passed": "7 / 7",
    }


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
    assert "4 unchecked" in html
    assert "No permission checks apply" in html
    assert summary_figures(html) == {
        "Actions allowed": "0 4 unchecked", "Actions refused": "0", "Actions unknown": "0",
        "Receipt": "—", "Checks passed": "0 / 7",
    }


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
            assert len(data.json()["checks"]) == 7
            assert (await client.get("/report/missing")).status_code == 404
            assert (await client.get("/api/report/missing")).status_code == 404

    asyncio.run(requests())


@pytest.mark.parametrize("state,fenced,expected", [
    ("rejected", True, "stopped before it reached the target"),
    ("rejected", False, "no change"),
    ("applied", True, "completed"),
])
def test_fenced_action_outcome_keeps_its_target_evidence(demo, state, fenced, expected):
    demo.effects.update_one({"_id": "shift-25"}, {"$set": {
        "state": state, "evidence": {"action_id": "shift-25", "system": "router", "at_epoch": 42, "fenced": fenced},
    }})
    html = render_report(build_report(demo, RUN))
    table = html.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    row = next(row for row in table.split("</tr>") if 'class="action-id">· <code title="shift-25"' in row)
    assert expected in Text(row).parts
    assert f'&quot;fenced&quot;: {str(fenced).lower()}' in row
    if state == "rejected" and fenced:
        assert "fence confirmed" in row
    else:
        assert "stopped before it reached the target" not in row


def test_summary_keeps_unknown_receipts_and_failed_checks_visible(demo):
    demo.effects.update_one({"_id": "shift-25"}, {"$set": {"state": "sent", "evidence": None}})
    result = build_report(demo, RUN)
    html = render_report(result)
    figures = summary_figures(html)
    assert figures["Actions unknown"] == "1"
    assert figures["Receipt"] == "CLOSED"
    assert figures["Checks passed"] == "6 / 7"
    assert "1 checks failed" in html
    assert "still unknown or unfinished" in html

    demo.receipts.update_one({"_id": "receipt-1"}, {"$set": {"status": "open", "closed_at": None}})
    figures = summary_figures(render_report(build_report(demo, RUN)))
    assert figures["Actions unknown"] == "1"
    assert figures["Receipt"] == "OPEN"
    assert figures["Checks passed"] == "7 / 7"


def test_shortened_identifiers_keep_escaped_full_values_and_details(demo):
    result = build_report(demo, RUN)
    action_id = 'run-1234567890-abcdefghijklmnopqrstuvwxyz-"<unsafe>'
    permission_id = "permission-1234567890-abcdefghijklmnopqrstuvwxyz"
    result["actions"][0]["action_id"] = action_id
    result["actions"][0]["chain"][0]["permission_id"] = permission_id
    html = render_report(result)
    table = html.split('<tbody>', 1)[1].split('</tbody>', 1)[0]
    assert f'title="{escape(action_id, quote=True)}"' in table
    assert f'title="{permission_id}"' in table
    assert '…' in table
    assert '<unsafe>' not in html
    details = html.split('<details class="records">', 1)[1].split('<pre>', 1)[1].split('</pre>', 1)[0]
    saved = json.loads(''.join(Text(details).parts))
    assert saved["actions"][0]["action_id"] == action_id
    assert saved["actions"][0]["chain"][0]["permission_id"] == permission_id
