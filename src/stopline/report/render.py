"""A standalone, printable page; all record content is escaped."""

import hashlib
import json
import re
from datetime import datetime, timezone
from html import escape


STYLE = """
*{box-sizing:border-box}html{color-scheme:light}body{margin:0;background:#fff;color:#18212b;
font:14px/1.3 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-variant-numeric:tabular-nums}
main{max-width:1280px;margin:auto;padding:16px 28px}h1{font-size:27px;line-height:1.15;margin:0 0 5px}
h2{font-size:16px;margin:0 0 7px}p{margin:4px 0}header{border-bottom:2px solid #18212b;padding-bottom:10px;margin-bottom:12px}
.meta,small{color:#43505d;font-size:12px}.top{display:grid;grid-template-columns:minmax(0,2.25fr) minmax(0,1fr);gap:24px}
section{min-width:0;margin-bottom:10px}ul{padding-left:18px;margin:4px 0}.tree{list-style:none;padding:0}
.tree ul{list-style:none;border-left:1px solid #c7cdd3;padding-left:14px;margin-left:7px}.tree li{margin:4px 0}
.scope{font-size:11px;color:#43505d}.permission{font-size:12px}.stop{border-left:3px solid #a43725;padding-left:14px}
.receipt-status{font-weight:750}.covered{list-style:none;padding:0;font-size:13px}.covered li{margin:3px 0}
table{border-collapse:collapse;width:100%;table-layout:fixed;font-size:12px}th{text-align:left;color:#43505d;background:#f3f5f7;font-size:11px}
th,td{padding:6px 7px;vertical-align:top;border-bottom:1px solid #d8dde2;overflow-wrap:anywhere}
th:nth-child(1){width:9%}th:nth-child(2){width:23%}th:nth-child(3){width:21%}th:nth-child(4){width:13%}
th:nth-child(5){width:14%}th:nth-child(6){width:20%}td small{font-size:10px;display:block;margin-top:2px}
.outcome{font-weight:750}.unknown{color:#8a4c08}.fail{color:#a22417}.pass{color:#205a36}
.checks{display:grid;grid-template-columns:1fr 1fr;gap:5px 24px;list-style:none;padding:0;font-size:12px}
.checks strong{display:inline-block;min-width:39px}.checks ul{grid-column:1/-1}a{color:inherit;text-decoration:underline;text-underline-offset:2px}
code{font-size:11px;overflow-wrap:anywhere}details{margin-top:3px}summary{cursor:pointer;font-size:11px;color:#43505d}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:11px;padding:10px;background:#f3f5f7;margin:6px 0}
.records{border-top:1px solid #d8dde2;padding-top:7px}.refusals{font-size:12px}.refusals h2{display:inline;font-size:12px}.refusals p{display:inline}.refusals p+p{display:block}.policy{font-size:11px}.action-id{font-size:10px;color:#43505d}
:focus-visible{outline:2px solid #18212b;outline-offset:3px}
@media(max-width:760px){main{padding:16px}.top{grid-template-columns:1fr;gap:0}.checks{grid-template-columns:1fr}th,td{padding:5px 3px;font-size:10px}}
@media print{@page{size:A4 landscape;margin:10mm}main{padding:0;max-width:none}body{font-size:11px}
header{margin-bottom:8px;padding-bottom:8px}section{margin-bottom:9px}th,td{padding:5px}h1{font-size:22px}
tr,li,.stop{break-inside:avoid}a{text-decoration:none}.top{grid-template-columns:minmax(0,2.25fr) minmax(0,1fr)}}
"""


REASONS = {
    "ok": "", "revoked": "permission stopped", "expired": "permission expired",
    "unknown_mandate": "permission not found", "tool_not_allowed": "tool not allowed",
    "target_not_allowed": "target not allowed", "limit_exceeded": "permission limit exceeded",
    "wider_than_parent": "permission exceeds the parent’s scope", "tree_too_large": "permission tree too large",
    "same_id_different_args": "action ID already used with different arguments",
    "cancelled_by_stop": "cancelled by the stop", "not_admitted": "action was not allowed",
    "not_claimed": "action was not claimed for sending", "not_authorised": "permission missing",
    "no_undo": "no undo available", "state_changed_since": "target state changed since the action",
    "fenced": "target blocks any late arrival", "target_unreachable": "target unreachable",
    "missing_record": "action record missing",
}


def _plain(value):
    # Internal storage vocabulary is translated only in the presentation.
    text = str(value)
    text = re.sub("no effect", "no change", text, flags=re.IGNORECASE)
    text = re.sub("mandate", "permission", text, flags=re.IGNORECASE)
    return re.sub("effect", "action", text, flags=re.IGNORECASE)


def _h(value):
    return escape(_plain("—" if value is None else value), quote=True)


def _person(value):
    return _h((value or "unknown").removeprefix("person:").removeprefix("agent:"))


def _time(value):
    return _h(value.replace("T", " ").replace("Z", " UTC") if value else "time not recorded")


def _anchor(action_id):
    return "action-" + hashlib.sha256(str(action_id).encode()).hexdigest()[:16]


def _record(value):
    return "<pre>" + _h(json.dumps(value, indent=2, ensure_ascii=False)) + "</pre>"


def _label(action):
    tool, args = action.get("tool"), action.get("args", {})
    if tool == "shift_traffic":
        return f'Shift traffic to {_h(args.get("percent"))}%'
    if tool == "rollback":
        return f'Roll back to {_h(args.get("to_version"))}'
    return _h({"restart": "Restart"}.get(tool, tool or action["action_id"]))


def _stopped(stop):
    if not stop:
        return ""
    return f'Stopped by {_person(stop["by"])} · #{_h(stop["counter"])} · {_time(stop["at"])}'


def _permissions(nodes):
    rows = []
    for node in nodes:
        limits = node["limits"]
        scope = f'Tools: {", ".join(node["tools"])} · Targets: {", ".join(node["targets"])}'
        rows.append(
            f'<li><strong>{_person(node["holder"])}</strong> <span class="permission">'
            f'← {_person(node["grantor"])} · permission {_h(node["permission_id"])} · '
            f'{_h(limits["max_actions"])} actions · up to {_h(limits["max_shift_percent"])}% traffic</span>'
            f'<div class="scope">{_h(scope)}</div>'
            f'<div class="scope">Until {_time(node["expires_at"])}'
            + (f' · {_stopped(node["stopped"])}' if node["stopped"] else " · active")
            + '</div>' + (_permissions(node["children"]) if node["children"] else "") + '</li>'
        )
    return '<ul class="tree">' + "".join(rows) + '</ul>'


def _evidence(action):
    if action.get("missing"):
        return "Action record missing; outcome unknown."
    evidence = action.get("evidence")
    if not evidence:
        return {"cancelled": "Never sent; cancelled by stop.", None: "No request was sent.",
                "admitted": "Allowed; not sent yet."}.get(action.get("state"), "Unknown; no target answer recorded.")
    summary = _h(evidence.get("system", "Target"))
    if isinstance(evidence.get("weights"), dict):
        summary += " · " + ", ".join(f"{_h(k)} {_h(v)}%" for k, v in evidence["weights"].items())
    elif action["state"] == "applied":
        summary += " · confirmed"
    else:
        summary += " · no change"
    target_time = ""
    epoch = evidence.get("at_epoch")
    if isinstance(epoch, (int, float)):
        try:
            target_time = ' · target ' + datetime.fromtimestamp(epoch, timezone.utc).strftime("%H:%M:%S UTC")
        except (ValueError, OverflowError, OSError):
            target_time = ' · target clock ' + _h(epoch)
    return summary + '<details><summary>Evidence' + target_time + '</summary>' + _record(evidence) + '</details>'


def _action(action):
    outcome = _h(action["outcome"])
    policy = ("No check" if action["decision"] == "unchecked" else "Policy missing") if action["policy_version"] is None else (
        f'v{_h(action["policy_version"])} · {_h((action["policy_hash"] or "")[:12])}'
    )
    chain = " → ".join(_h(item["permission_id"]) for item in action["chain"])
    who = _person(action["principal"])
    who += '<small>' + (chain or "No permission chain") + '</small>'
    undo = ""
    if action.get("reverses"):
        undo += f'<small>Undo of <a href="#{_anchor(action["reverses"])}">{_h(action["reverses"])}</a></small>'
    for aid in action.get("undone_by", []):
        undo += f'<small>Undo: <a href="#{_anchor(aid)}">{_h(aid)}</a></small>'
    if action.get("undo") == "no_undo":
        undo += '<small>No undo available</small>'
    if action.get("undo") == "undo_unknown":
        undo += '<small>Undo unknown</small>'
    counter = '—' if action["counter"] is None else '#' + str(action["counter"])
    at = action["at"].split("T")[-1].replace("Z", "")
    reason = REASONS.get(action["reason"], action["reason"].replace("_", " "))
    description = _label(action) + ' <span class="action-id">· ' + _h(action["action_id"]) + '</span><small>' + _h(action.get("target")) + '</small>'
    return (
        f'<tr id="{_anchor(action["action_id"])}"><td>{_h(counter)}<small>{_h(at)}</small></td>'
        f'<td>{description}</td><td>{who}</td><td><strong>{_h(action["decision"])}</strong><small>{policy}</small></td>'
        f'<td><span class="outcome {"unknown" if action["outcome"] == "unknown" else ""}">{outcome}</span>'
        f'<small>{_h(reason)}</small>{undo}</td><td>{_evidence(action)}</td></tr>'
    )


def render_report(report) -> str:
    """Render build_report's JSON-ready result without scripts or external assets."""
    run = report["run"]
    checks = report["checks"]
    applicable = [check for check in checks if check["applicable"]]
    failed = [check for check in applicable if not check["passed"]]
    status = f'{len(failed)} checks failed' if failed else f'{len(applicable)} checks passed'
    if not applicable:
        status = "No permission checks apply"
    parts = ['<!doctype html><html lang="en"><head><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width,initial-scale=1">',
             '<title>StopLine · ' + _h(run["_id"]) + '</title><style>' + STYLE + '</style></head><body><main>',
             '<header><h1>StopLine · Run review</h1>',
             f'<p><strong>{_h(run["mode"])}</strong> · {_h(run["_id"])} · {_h(run["cluster"])} cluster · {_h(run["status"])} · <strong>{_h(status)}</strong></p>',
             f'<p class="meta">{_h(report["notice"])} Created {_time(run["created_at"])}. Times are UTC.</p></header>',
             '<div class="top"><section><h2>Permissions</h2>',
             _permissions(report["permissions"]) if report["permissions"] else '<p>No permissions were recorded.</p>',
             '</section><section class="stop"><h2>Stops &amp; receipts</h2>']
    if not report["stops"]:
        parts.append('<p>No stop receipts were recorded.</p>')
        if run.get("stop_at"):
            parts.append('<p class="meta">Stop flag recorded at ' + _time(run["stop_at"]) + '.</p>')
    for stop in report["stops"]:
        parts.append(f'<p>Permission <strong>{_h(stop["permission_id"])}</strong></p><p class="meta">{_stopped(stop)}</p>')
        receipt = stop["receipt"]
        if receipt is None:
            parts.append('<p class="fail">Receipt missing.</p>')
            continue
        parts.append(f'<p class="receipt-status">{_h(receipt["status"].upper())} · receipt {_h(receipt["_id"])}</p><p class="meta">Fixed covered list:</p><ul class="covered">')
        for row in stop["covered"]:
            parts.append(f'<li><a href="#{_anchor(row["action_id"])}">{_h(row["action_id"])}</a> · <strong>{_h(row["outcome"])}</strong>' + (' · record missing' if row.get("missing") else '') + '</li>')
        parts.append(f'</ul><p class="meta">{len(stop["refusals"])} later refusal(s), outside the covered list.</p>')
        if receipt.get("closed_at"):
            parts.append('<p class="meta">Closed ' + _time(receipt["closed_at"]) + '</p>')
    parts.append('</section></div><section><h2>Actions in order</h2>')
    parts.append('<table><thead><tr><th scope="col">Counter / time</th><th scope="col">Action / target</th><th scope="col">Who asked / chain</th><th scope="col">Decision / policy</th><th scope="col">Outcome / undo</th><th scope="col">Target evidence</th></tr></thead><tbody>')
    parts.extend(_action(action) for action in report["actions"])
    if not report["actions"]:
        parts.append('<tr><td colspan="6">No actions were recorded.</td></tr>')
    parts.append('</tbody></table></section>')
    if report["refusals"]:
        parts.append('<section class="refusals"><h2>Refused: </h2>')
        for refusal in report["refusals"]:
            reason = REASONS.get(refusal["reason"], refusal["reason"].replace("_", " "))
            parts.append(f'<p><strong>{_h(refusal["action_id"] or "Permission request")}</strong> · {_person(refusal["principal"])} · {_h(reason)} · {_stopped(refusal["stopped"]) or _time(refusal["at"])}</p>')
        parts.append('</section>')
    parts.append('<section><h2>Checks from the stored records</h2><ul class="checks">')
    for check in checks:
        label = "PASS" if check["passed"] else "FAIL"
        suffix = "" if check["applicable"] else " (not applicable: no check before actions)"
        parts.append(f'<li><strong class="{label.lower()}">{label}</strong> {_h(check["name"])}{suffix}')
        if check["violations"]:
            parts.append('<ul class="fail">' + ''.join('<li>' + _h(item) + '</li>' for item in check["violations"]) + '</ul>')
        parts.append('</li>')
    parts.append('</ul><p class="meta">Counters allocate hand-downs, admissions and stops; other audit entries may observe the same value. Checks establish consistency of these records.</p></section>')
    for policy in report["policies"]:
        parts.append(f'<p class="policy">Policy v{_h(policy["_id"])} · full hash <code>{_h(policy["hash"])}</code></p>')
    parts.append(f'<details class="records"><summary>Full action details, {len(report["audit"])} audit entries and {len(report["tasks"])} saved work records</summary>')
    parts.append(_record({"actions": report["actions"], "audit": report["audit"], "saved_work": report["tasks"]}))
    parts.append('</details></main></body></html>')
    return "".join(parts)
