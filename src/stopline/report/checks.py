"""Re-derive guarantees; never use a stored 'passed' flag."""

from collections import defaultdict

from stopline.contract import TERMINAL_STATES


def check_records(run, permissions, actions, audit, receipts, policies):
    results = []
    gated = run["mode"] == "stopline"
    by_id = {row["_id"]: row for row in actions}

    def result(key, name, failures, applicable=True):
        results.append({
            "id": key, "name": name, "passed": not failures, "applicable": applicable,
            "violations": [message for _, message in failures],
            "action_ids": sorted({aid for ids, _ in failures for aid in ids if aid}),
        })

    allows = defaultdict(list)
    for entry in audit:
        if entry["kind"] == "allow":
            allows[entry.get("action_id")].append(entry)
    failures = []
    for action in actions if gated else []:
        matching = any(
            all(entry.get(audit_key) == action.get(action_key) for audit_key, action_key in (
                ("principal", "principal"), ("mandate_id", "mandate_id"), ("root_id", "root_id"),
                ("seq", "admitted_seq"), ("policy_version", "policy_version"), ("policy_hash", "policy_hash"),
            )) for entry in allows[action["_id"]]
        )
        if not matching:
            failures.append(([action["_id"]], f'{action["_id"]}: no matching allow entry in the audit.'))
    result("allowed_actions_audited", "Every allowed action has a matching allow entry", failures, gated)

    failures = []
    for receipt in receipts:
        for aid in receipt["covered"]:
            if aid not in by_id:
                failures.append(([aid], f'{aid}: missing from receipt {receipt["_id"]}.'))
    result("covered_actions_exist", "Every action covered by a receipt exists", failures, gated or bool(receipts))

    # Copies of one operation in an action, audit and receipt share a counter.
    # Claims, outcomes and refusals only observe it; they do not allocate it.
    counters = defaultdict(dict)
    failures = []

    def register(root, seq, operation, label, action_id=None):
        if root is None or not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            failures.append(([action_id], f"{label}: missing or invalid root counter."))
            return
        counters[(root, seq)][operation] = (label, action_id)

    for action in actions if gated else []:
        register(action.get("root_id"), action.get("admitted_seq"), ("allow", action["_id"]), action["_id"], action["_id"])
    for receipt in receipts:
        register(receipt["root_id"], receipt["stop_seq"], ("stop", receipt["mandate_id"]), f'stop {receipt["_id"]}')
    for entry in audit:
        kind = entry["kind"]
        if kind not in {"allow", "hand_down", "stop"}:
            continue
        identity = entry.get("action_id") if kind == "allow" else entry.get("mandate_id")
        if kind == "hand_down":
            identity = str(entry["_id"])
        register(entry.get("root_id"), entry.get("seq"), (kind, identity),
                 entry.get("action_id") or f'{kind} {entry.get("mandate_id")}', entry.get("action_id"))
    for (root, seq), operations in counters.items():
        if len(operations) > 1:
            labels, ids = zip(*operations.values())
            failures.append((ids, f'Root {root}, counter {seq} repeats: {", ".join(labels)}.'))
    result("unique_root_counters", "Counter values never repeat for separate operations under one root", failures, gated or bool(counters))

    # Use both the frozen admission chain and the saved tree/stop records.
    # A missing chain must not conceal an admission below a stopped ancestor.
    scopes = []
    for permission in permissions:
        if permission.get("revoked_at_seq") is not None:
            scopes.append((permission["root_id"], {permission["_id"]}, permission["revoked_at_seq"]))
    for receipt in receipts:
        scopes.append((receipt["root_id"], set(receipt["revoked"]) | {receipt["mandate_id"]}, receipt["stop_seq"]))
    for entry in audit:
        if entry["kind"] == "stop" and entry.get("seq") is not None:
            scopes.append((entry.get("root_id"), {entry.get("mandate_id")}, entry["seq"]))
    failures = []
    failed_ids = set()
    admissions = actions + [
        {"_id": entry["action_id"], "root_id": entry.get("root_id"), "mandate_id": entry.get("mandate_id"),
         "admitted_seq": entry.get("seq"), "chain": []}
        for entry in audit if entry["kind"] == "allow" and entry.get("action_id")
    ]
    for root, scope, stop_seq in scopes:
        while True:
            descendants = {p["_id"] for p in permissions if p.get("parent_id") in scope}
            if descendants <= scope:
                break
            scope |= descendants
        for action in admissions:
            chain = set(action.get("chain", [])) | {action.get("mandate_id")}
            if action.get("root_id") == root and chain & scope and action.get("admitted_seq") is not None:
                if action["admitted_seq"] > stop_seq and action["_id"] not in failed_ids:
                    failed_ids.add(action["_id"])
                    failures.append(([action["_id"]], f'{action["_id"]}: admitted at counter {action["admitted_seq"]}, above stop {stop_seq}.'))
    result("no_admission_after_stop", "No action was admitted above its permission’s stop counter", failures, gated or bool(scopes))

    failures = []
    for receipt in receipts:
        if receipt["status"] != "closed":
            continue
        for aid in receipt["covered"]:
            if aid not in by_id or by_id[aid].get("state") not in TERMINAL_STATES:
                failures.append(([aid], f'{aid}: receipt {receipt["_id"]} is closed but this action is still unknown or unfinished.'))
    result("closed_receipts_settled", "No closed receipt contains an unknown or unfinished action", failures, gated or bool(receipts))

    known_policies = {(policy["_id"], policy["hash"]) for policy in policies}
    failures = []
    for entry in audit:
        if (entry.get("policy_version"), entry.get("policy_hash")) not in known_policies:
            aid = entry.get("action_id")
            label = aid or f'audit {entry["_id"]}'
            failures.append(([aid], f'{label}: policy version and hash do not match a stored policy.'))
    result("audit_policies_exist", "Every audit entry names a stored policy version and hash", failures, gated or bool(audit))
    return results
