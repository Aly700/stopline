"""A standalone, printable page; all record content is escaped."""

import hashlib
import json
import re
from datetime import datetime, timezone
from html import escape


STYLE = """
:root{color-scheme:dark;--canvas:#090f19;--panel:#101925;--raised:#152131;
--line:#293649;--text:#e5ecf5;--muted:#9baac0;--blue:#83b5ff;
--green:#86d9b0;--green-bg:#142e29;--amber:#f0c781;--amber-bg:#352b1b;
--red:#f1a2a7;--red-bg:#352129;--mono:ui-monospace,SFMono-Regular,Consolas,"Liberation Mono",monospace}
*{box-sizing:border-box}body{margin:0;background:var(--canvas);color:var(--text);
font:14px/1.5 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-variant-numeric:tabular-nums}
main{max-width:1680px;margin:auto;padding:32px 40px 48px}h1{font-size:26px;line-height:1.25;letter-spacing:-.6px;margin:0 0 8px;font-weight:650}
h2{font-size:14px;line-height:1.5;margin:0;font-weight:650}p{margin:8px 0}
header{margin-bottom:24px}.heading{display:flex;align-items:flex-start;justify-content:space-between;gap:24px}
.heading p{color:var(--muted);font-size:13px;margin:0}.heading>.chip{margin-top:4px}
.run-meta{display:flex;align-items:center;flex-wrap:wrap;gap:8px 16px;margin:16px 0 8px}
.run-id{color:var(--blue);font-size:14px}.meta,small{color:var(--muted);font-size:12px}
.summary{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));margin:20px 0 0;
border:1px solid var(--line);border-radius:8px;background:var(--panel)}
.summary>div{padding:16px 20px;min-width:0}.summary>div+div{border-left:1px solid var(--line)}
.summary dt{font-size:12px;color:var(--muted);margin-bottom:8px}.summary dd{margin:0;font-size:24px;line-height:1.25;font-weight:600;letter-spacing:-.5px}
.summary dd small{font-size:14px;font-weight:400;letter-spacing:0}.summary .chip{font-size:13px;vertical-align:middle}
.summary-note{display:block;color:var(--muted);font-size:12px;font-weight:400;letter-spacing:0;line-height:1.5;margin-top:4px}
.top{display:grid;grid-template-columns:minmax(0,1.8fr) minmax(0,1fr);gap:20px;align-items:start}
section{min-width:0;margin-bottom:20px}.panel{border:1px solid var(--line);border-radius:8px;background:var(--panel)}
.section-heading{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:16px 20px;border-bottom:1px solid var(--line)}
.panel-body{padding:16px 20px}.panel-body>:first-child{margin-top:0}.panel-body>:last-child{margin-bottom:0}
ul{padding-left:20px;margin:8px 0}.tree{list-style:none;padding:0;margin:0}
.tree ul{margin:0 0 0 8px;padding-left:24px}.tree li{position:relative;margin:0}
.tree ul>li:before{content:"";position:absolute;left:-24px;top:0;bottom:0;border-left:1px solid var(--line)}
.tree ul>li:last-child:before{height:20px}.tree ul>li:after{content:"";position:absolute;left:-24px;top:20px;width:16px;border-top:1px solid var(--line)}
.permission-node{padding:8px 0}.tree>.tree-root>.permission-node{padding-top:0}
.permission-title{display:flex;align-items:baseline;flex-wrap:wrap;gap:4px 8px}
.permission-title strong{font-size:14px;font-weight:600}.permission{font-size:12px;color:var(--muted)}
.permission code{color:var(--text)}.permission-node details{margin-top:4px}.permission-node summary{font-size:13px;color:var(--text)}
.scope{font-size:12px;color:var(--muted);overflow-wrap:anywhere}
.permission-stop{font-size:12px;color:var(--muted);margin:4px 0}.permission-stop .chip{margin-right:4px}
.receipt+.receipt{border-top:1px solid var(--line);margin-top:16px;padding-top:16px}
.receipt-status{display:flex;align-items:center;flex-wrap:wrap;gap:8px;font-size:12px;margin:16px 0}
.covered{list-style:none;padding:0;margin:8px 0 16px}.covered li{display:flex;align-items:baseline;justify-content:space-between;
gap:12px;padding:8px 0;border-bottom:1px solid var(--line);font-size:13px}.covered a{min-width:0;overflow-wrap:anywhere}
.covered .chip{flex-shrink:0}.receipt .meta{margin:8px 0}
.chip,.outcome{display:inline-block;max-width:100%;padding:2px 8px;border:1px solid var(--line);border-radius:6px;
background:var(--raised);color:var(--muted);font-size:12px;line-height:1.5;font-weight:600;letter-spacing:0;overflow-wrap:anywhere}
.chip.mode{color:var(--blue);border-color:#35517a;background:#17273e}
.chip.pass,.outcome.pass{color:var(--green);background:var(--green-bg);border-color:#2b5146}
.chip.unknown,.outcome.unknown{color:var(--amber);background:var(--amber-bg);border-color:#604d2d}
.chip.fail,.outcome.fail{color:var(--red);background:var(--red-bg);border-color:#64404b}
.pass{color:var(--green)}.unknown{color:var(--amber)}.fail{color:var(--red)}
table{border-collapse:separate;border-spacing:0;width:100%;table-layout:fixed;font-size:13px}
th{text-align:left;color:var(--muted);background:var(--raised);font-size:11px;font-weight:600;position:sticky;top:0;z-index:2}
th,td{padding:12px;vertical-align:top;border-bottom:1px solid var(--line);overflow-wrap:anywhere}
th:first-child,td:first-child{padding-left:20px;text-align:right}th:last-child,td:last-child{padding-right:20px}
th:nth-child(1){width:10%}th:nth-child(2){width:21%}th:nth-child(3){width:22%}th:nth-child(4){width:13%}
th:nth-child(5){width:15%}th:nth-child(6){width:19%}tbody tr:last-child td{border-bottom:0}
tbody tr:hover{background:var(--raised)}tr:target{background:var(--raised);outline:2px solid var(--blue);outline-offset:-2px;scroll-margin-top:48px}
td small{font-size:11px;display:block;margin-top:6px;line-height:1.5}td small code{font-size:11px}td:first-child{font-family:var(--mono);font-size:13px}
td:first-child small{font-size:11px}.action-name{display:block;font-weight:600}.action-id{display:block;margin-top:4px;font-size:11px;color:var(--muted);font-family:var(--mono)}
.target{font-family:var(--mono);color:var(--muted)}.chain{display:flex;align-items:center;flex-wrap:wrap;gap:4px;margin-top:8px}
.chain code{padding:1px 5px;border:1px solid var(--line);border-radius:6px;background:var(--canvas);font-size:11px}
.chain-arrow{color:var(--muted);font-size:11px}.policy-ref{font-family:var(--mono)}
.checks{list-style:none;padding:0;margin:0}.checks>li{padding:12px 20px;border-bottom:1px solid var(--line)}
.checks>li:last-child{border-bottom:0}.check-line{display:flex;align-items:baseline;gap:12px;font-size:13px;overflow-wrap:anywhere}
.check-line>.chip{flex-shrink:0;min-width:48px;text-align:center}.checks ul{margin:8px 0 0 60px;font-size:12px;overflow-wrap:anywhere}
.checks-note{padding:12px 20px;border-top:1px solid var(--line);margin:0}
a{color:var(--blue);text-decoration:none;text-underline-offset:3px}a:hover{text-decoration:underline}
code,pre{font-family:var(--mono);font-size:12px;overflow-wrap:anywhere}
details{margin-top:8px;min-width:0}summary{cursor:pointer;font-size:12px;color:var(--muted);width:fit-content;max-width:100%}
summary:hover{color:var(--text)}summary::marker{color:var(--blue)}details[open]>summary{margin-bottom:8px;color:var(--text)}
pre{white-space:pre-wrap;word-break:break-word;font-size:11px;line-height:1.6;padding:12px;border:1px solid var(--line);border-radius:6px;background:var(--canvas);margin:8px 0 0}
.records{border:1px solid var(--line);border-radius:8px;padding:16px 20px;background:var(--panel);margin-top:16px}
.records>summary{font-size:13px}.refusals{padding:12px 20px;border-left:2px solid var(--red)}
.refusals h2{font-size:13px;color:var(--red)}.refusals p{font-size:12px;margin:4px 0;overflow-wrap:anywhere}
.policy{font-size:12px;color:var(--muted);margin:8px 0}.policy code{font-size:11px}
:focus-visible{outline:2px solid var(--blue);outline-offset:4px;border-radius:2px}
@media(max-width:1320px){main{padding:24px}.summary>div{padding:16px}.top{grid-template-columns:minmax(0,1.65fr) minmax(0,1fr)}}
@media(max-width:900px){main{padding:16px}.top{grid-template-columns:1fr;gap:0}.summary>div{padding:12px}.summary dd{font-size:20px}
th,td{padding:8px 6px}th:first-child,td:first-child{padding-left:8px}th:last-child,td:last-child{padding-right:8px}.chain{gap:2px}}
@media(max-width:600px){.heading{gap:12px}h1{font-size:22px}.summary{grid-template-columns:repeat(2,minmax(0,1fr))}
.summary>div+div{border-left:0}.summary>div{border-bottom:1px solid var(--line)}.summary>div:last-child{border-bottom:0}
.summary>div:nth-child(even){border-left:1px solid var(--line)}.panel-body,.section-heading{padding:12px}
table{font-size:11px}th{font-size:10px}.chip,.outcome{padding:2px 4px;font-size:11px}.checks>li{padding:12px}}
@media print{@page{size:A4 landscape;margin:10mm}
:root{color-scheme:light;--canvas:#fff;--panel:#fff;--raised:#f2f4f6;--line:#c4cbd3;--text:#18212b;--muted:#465360;
--blue:#174a7a;--green:#205a36;--green-bg:#edf6ef;--amber:#745013;--amber-bg:#faf4e8;--red:#8e2931;--red-bg:#f9edef}
main{padding:0;max-width:none}body{font-size:11px}header{margin-bottom:12px}h1{font-size:22px}
.heading p,.run-id{font-size:12px}.summary{margin-top:12px}.summary>div{padding:8px 12px}.summary dd{font-size:20px}
section{margin-bottom:12px}.top{grid-template-columns:minmax(0,1.8fr) minmax(0,1fr);gap:12px}
.panel-body,.section-heading{padding:12px}.permission-node{padding:8px 0}
th{position:static}th,td{padding:8px}thead{display:table-header-group}tr,.permission-node,.receipt,.checks>li{break-inside:avoid}
.chip.mode{background:var(--raised);color:var(--blue);border-color:var(--line)}
.chip,.outcome{border-color:var(--line)!important}a{color:inherit;text-decoration:none}.checks>li{padding:8px 12px}
.records{padding:12px}.policy code{font-size:10px}}
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


def _identifier(value, limit=24):
    text = str(value)
    short = text if len(text) <= limit else text[:limit - 9] + "…" + text[-8:]
    return f'<code title="{_h(text)}">{_h(short)}</code>'


def _tone(value):
    if value in {"completed", "allowed", "closed"}:
        return "pass"
    if value in {"unknown", "open", "unchecked"}:
        return "unknown"
    if value in {"blocked", "refused", "missing"}:
        return "fail"
    return ""


def _chip(value, tone=None):
    return f'<span class="chip {_tone(value) if tone is None else tone}">{_h(value)}</span>'


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
            f'<li class="{"tree-root" if node.get("parent_id") is None else "tree-child"}"><div class="permission-node">'
            f'<div class="permission-title"><strong>{_person(node["holder"])}</strong> <span class="permission">'
            f'← {_person(node["grantor"])} · permission <code>{_h(node["permission_id"])}</code></span></div>'
            '<details><summary>'
            f'{_h(limits["max_actions"])} actions · up to {_h(limits["max_shift_percent"])}% traffic</summary>'
            f'<div class="scope">{_h(scope)}</div>'
            f'<div class="scope">Until {_time(node["expires_at"])}</div></details>'
            '<p class="permission-stop">'
            + (_stopped(node["stopped"]) if node["stopped"] else _chip("active", "pass"))
            + '</p></div>'
            + (_permissions(node["children"]) if node["children"] else "") + '</li>'
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
    if action["state"] == "rejected" and evidence.get("fenced"):
        summary += " · fence confirmed"
    elif isinstance(evidence.get("weights"), dict):
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
    if action.get("state") == "rejected" and (action.get("evidence") or {}).get("fenced"):
        outcome = "stopped before it reached the target"
    policy = ("No check" if action["decision"] == "unchecked" else "Policy missing") if action["policy_version"] is None else (
        f'v{_h(action["policy_version"])} · <span title="{_h(action["policy_hash"] or "")}">{_h((action["policy_hash"] or "")[:12])}</span>'
    )
    chain = '<span class="chain-arrow">→</span>'.join(_identifier(item["permission_id"], 14) for item in action["chain"])
    who = _person(action["principal"])
    who += '<div class="chain">' + (chain or '<small>No permission chain</small>') + '</div>'
    undo = ""
    if action.get("reverses"):
        undo += f'<small>Undo of <a href="#{_anchor(action["reverses"])}">{_identifier(action["reverses"], 16)}</a></small>'
    for aid in action.get("undone_by", []):
        undo += f'<small>Undo: <a href="#{_anchor(aid)}">{_identifier(aid, 16)}</a></small>'
    if action.get("undo") == "no_undo":
        undo += '<small>No undo available</small>'
    if action.get("undo") == "undo_unknown":
        undo += '<small>Undo unknown</small>'
    counter = '—' if action["counter"] is None else '#' + str(action["counter"])
    at = _h(action["at"].split("T")[-1].replace("Z", "")).replace(".", "<wbr>.")
    reason = REASONS.get(action["reason"], action["reason"].replace("_", " "))
    description = '<span class="action-name">' + _label(action) + '</span><span class="action-id">· ' + _identifier(action["action_id"]) + '</span><small class="target">' + _h(action.get("target")) + '</small>'
    return (
        f'<tr id="{_anchor(action["action_id"])}"><td>{_h(counter)}<small>{at}</small></td>'
        f'<td>{description}</td><td>{who}</td><td>{_chip(action["decision"])}<small class="policy-ref">{policy}</small></td>'
        f'<td><span class="outcome {_tone(action["outcome"])}">{outcome}</span>'
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
    status_tone = "fail" if failed else "pass" if applicable else "unknown"
    allowed = sum(action["decision"] == "allowed" for action in report["actions"])
    refused = sum(action["decision"] == "refused" for action in report["actions"])
    unknown = sum(action["outcome"] == "unknown" for action in report["actions"])
    unchecked = sum(action["decision"] == "unchecked" for action in report["actions"])
    receipt_states = list(dict.fromkeys(
        stop["receipt"]["status"] if stop["receipt"] else "missing" for stop in report["stops"]
    ))
    receipt_summary = " ".join(_chip(state.upper(), _tone(state)) for state in receipt_states) or "—"
    passed = sum(check["passed"] for check in applicable)
    parts = ['<!doctype html><html lang="en"><head><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width,initial-scale=1">',
             '<title>StopLine · ' + _h(run["_id"]) + '</title><style>' + STYLE + '</style></head><body><main>',
             '<header><div class="heading"><div><h1>StopLine · Run review</h1>',
             "<p>Rebuilt from the stored records only. Nothing on this page comes from the running program's memory.</p>",
             '</div>' + _chip(run["mode"], "mode") + '</div>',
             f'<p class="run-meta"><code class="run-id">{_h(run["_id"])}</code><span class="meta">{_h(run["cluster"])} cluster</span>'
             + _chip(run["status"]) + _chip(status, status_tone) + '</p>',
             f'<p class="meta">{_h(report["notice"])} Created {_time(run["created_at"])}. Times are UTC.</p>',
             f'<dl class="summary"><div><dt>Actions allowed</dt><dd>{allowed}'
             + (f'<span class="summary-note">{unchecked} unchecked</span>' if unchecked else '') + '</dd></div>',
             f'<div><dt>Actions refused</dt><dd>{refused}</dd></div>',
             f'<div><dt>Actions unknown</dt><dd class="{"unknown" if unknown else ""}">{unknown}</dd></div>',
             f'<div><dt>Receipt</dt><dd>{receipt_summary}</dd></div>',
             f'<div><dt>Checks passed</dt><dd>{passed} <small>/ {len(checks)}</small></dd></div></dl></header>',
             '<div class="top"><section class="panel"><div class="section-heading"><h2>Permissions</h2></div><div class="panel-body">',
             _permissions(report["permissions"]) if report["permissions"] else '<p>No permissions were recorded.</p>',
             '</div></section><section class="panel stop"><div class="section-heading"><h2>Stops &amp; receipts</h2></div><div class="panel-body">']
    if not report["stops"]:
        parts.append('<p>No stop receipts were recorded.</p>')
        if run.get("stop_at"):
            parts.append('<p class="meta">Stop flag recorded at ' + _time(run["stop_at"]) + '.</p>')
    for stop in report["stops"]:
        parts.append(f'<div class="receipt"><p>Permission <code>{_h(stop["permission_id"])}</code></p><p class="meta">{_stopped(stop)}</p>')
        receipt = stop["receipt"]
        if receipt is None:
            parts.append('<p class="fail">Receipt missing.</p></div>')
            continue
        parts.append('<p class="receipt-status">' + _chip(receipt["status"].upper(), _tone(receipt["status"]))
                     + f' <span>· receipt <code>{_h(receipt["_id"])}</code></span></p><p class="meta">Fixed covered list:</p><ul class="covered">')
        for row in stop["covered"]:
            parts.append(f'<li><a href="#{_anchor(row["action_id"])}"><code>{_h(row["action_id"])}</code></a> '
                         + _chip(row["outcome"]) + (' · record missing' if row.get("missing") else '') + '</li>')
        parts.append(f'</ul><p class="meta">{len(stop["refusals"])} later refusal(s), outside the covered list.</p>')
        if receipt.get("closed_at"):
            parts.append('<p class="meta">Closed ' + _time(receipt["closed_at"]) + '</p>')
        parts.append('</div>')
    parts.append('</div></section></div><section class="panel"><div class="section-heading"><h2>Actions in order</h2></div>')
    parts.append('<table><thead><tr><th scope="col">Counter / time</th><th scope="col">Action / target</th><th scope="col">Who asked / chain</th><th scope="col">Decision / policy</th><th scope="col">Outcome / undo</th><th scope="col">Target evidence</th></tr></thead><tbody>')
    parts.extend(_action(action) for action in report["actions"])
    if not report["actions"]:
        parts.append('<tr><td colspan="6">No actions were recorded.</td></tr>')
    parts.append('</tbody></table></section>')
    if report["refusals"]:
        parts.append('<section class="panel refusals"><h2>Refused: </h2>')
        for refusal in report["refusals"]:
            reason = REASONS.get(refusal["reason"], refusal["reason"].replace("_", " "))
            parts.append(f'<p><strong>{_h(refusal["action_id"] or "Permission request")}</strong> · {_person(refusal["principal"])} · {_h(reason)} · {_stopped(refusal["stopped"]) or _time(refusal["at"])}</p>')
        parts.append('</section>')
    parts.append('<section class="panel"><div class="section-heading"><h2>Checks from the stored records</h2></div><ul class="checks">')
    for check in checks:
        label = "PASS" if check["passed"] else "FAIL"
        suffix = "" if check["applicable"] else " (not applicable: no check before actions)"
        tone = label.lower() if check["applicable"] else ""
        parts.append(f'<li><div class="check-line"><strong class="chip {tone}">{label}</strong> <span>{_h(check["name"])}{suffix}</span></div>')
        if check["violations"]:
            parts.append('<ul class="fail">' + ''.join('<li>' + _h(item) + '</li>' for item in check["violations"]) + '</ul>')
        parts.append('</li>')
    parts.append('</ul><p class="meta checks-note">Counters allocate hand-downs, admissions and stops; other audit entries may observe the same value. Checks establish consistency of these records.</p></section>')
    for policy in report["policies"]:
        parts.append(f'<p class="policy">Policy v{_h(policy["_id"])} · full hash <code>{_h(policy["hash"])}</code></p>')
    parts.append(f'<details class="records"><summary>Full action details, {len(report["audit"])} audit entries and {len(report["tasks"])} saved work records</summary>')
    parts.append(_record({"actions": report["actions"], "audit": report["audit"], "saved_work": report["tasks"]}))
    parts.append('</details></main></body></html>')
    return "".join(parts)
