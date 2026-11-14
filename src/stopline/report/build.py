"""Build an account from one read-only database snapshot."""

from collections import defaultdict
from datetime import datetime, timezone

from bson import ObjectId

from stopline.db import run_transaction

from .checks import check_records


def _json_value(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _time_key(row, field):
    at = row.get(field)
    timestamp = at.replace(tzinfo=at.tzinfo or timezone.utc).timestamp() if at else float("-inf")
    return (timestamp, row.get("seq") or row.get("admitted_seq") or row.get("counter") or 0,
            str(row.get("_id", row.get("action_id", ""))))


def build_report(db, run_id) -> dict:
    """Reconstruct a run without importing the gate or contacting a target.

    Raises LookupError for a missing run. All returned values are JSON-ready.
    The seven checks concern consistency of the stored records; they cannot
    establish that the records themselves have never been rewritten.
    """
    def read(session):
        run = db.runs.find_one({"_id": run_id}, session=session)
        if run is None:
            raise LookupError("Run not found")
        records = {name: list(db[name].find({"run_id": run_id}, session=session))
                   for name in ("mandates", "effects", "audit", "receipts", "tasks")}
        records["policies"] = list(db.policies.find({}, session=session))
        return run, records

    (run, records), _ = run_transaction(read)
    permissions = records["mandates"]
    permissions_by_id = {p["_id"]: p for p in permissions}
    effects = records["effects"]
    audit = sorted(records["audit"], key=lambda row: _time_key(row, "at"))
    receipts = sorted(records["receipts"], key=lambda row: _time_key(row, "stopped_at"))
    tasks = sorted(records["tasks"], key=lambda row: row["_id"])
    policies = sorted(records["policies"], key=lambda row: row["_id"])
    audit_by_action = defaultdict(list)
    for entry in audit:
        if entry.get("action_id"):
            audit_by_action[entry["action_id"]].append(entry)
    plans = {step["action_id"]: step for task in tasks for step in task["plan"]}

    def chain_ids(permission_id):
        chain = []
        while permission_id and permission_id not in chain:
            chain.append(permission_id)
            permission_id = permissions_by_id.get(permission_id, {}).get("parent_id")
        return list(reversed(chain))

    def describe_chain(ids):
        return [{"permission_id": pid, "holder": permissions_by_id.get(pid, {}).get("holder"),
                 "grantor": permissions_by_id.get(pid, {}).get("grantor"), "missing": pid not in permissions_by_id}
                for pid in ids]

    def stopped(permission_id, detail=None):
        detail = detail or {}
        if detail.get("revoked_by"):
            return {"by": detail["revoked_by"], "counter": detail.get("revoked_at_seq"), "at": detail.get("revoked_at")}
        for pid in reversed(chain_ids(permission_id)):
            permission = permissions_by_id.get(pid, {})
            if permission.get("revoked_at_seq") is not None:
                return {"by": permission.get("revoked_by"), "counter": permission["revoked_at_seq"], "at": permission.get("revoked_at")}
        for receipt in receipts:
            if permission_id in receipt["revoked"] or permission_id == receipt["mandate_id"]:
                return {"by": receipt["stopped_by"], "counter": receipt["stop_seq"], "at": receipt["stopped_at"]}
        return None

    children = defaultdict(list)
    for permission in sorted(permissions, key=lambda p: (p.get("depth", 0), p["_id"])):
        children[permission.get("parent_id")].append(permission)
    visited = set()

    def node(permission):
        visited.add(permission["_id"])
        return {**permission, "permission_id": permission["_id"], "stopped": stopped(permission["_id"]),
                "children": [node(child) for child in children[permission["_id"]] if child["_id"] not in visited]}

    forest = [node(p) for p in permissions if p.get("parent_id") not in permissions_by_id and p["_id"] not in visited]
    # Keep disconnected or cyclic records visible instead of recursing forever.
    forest.extend(node(p) for p in permissions if p["_id"] not in visited)

    actions = []
    for action in effects:
        aid = action["_id"]
        state = action["state"]
        actions.append({
            **action, "action_id": aid, "permission_id": action.get("mandate_id"),
            "chain_ids": action["chain"], "chain": describe_chain(action["chain"]),
            "decision": "allowed" if run["mode"] == "stopline" else "unchecked",
            "counter": action["admitted_seq"], "at": action["admitted_at"],
            "outcome": {"applied": "completed", "cancelled": "blocked", "rejected": "no effect"}.get(state, "unknown"),
            "reason": "cancelled_by_stop" if state == "cancelled" else "ok",
            "stopped": stopped(action.get("mandate_id")) if state == "cancelled" else None,
            "undone_by": [other["_id"] for other in effects if other.get("reverses") == aid],
            "audit": audit_by_action[aid],
        })
    existing = {action["action_id"] for action in actions}
    for entry in audit:
        aid = entry.get("action_id")
        if entry["kind"] != "allow" or not aid or aid in existing:
            continue
        detail = {**plans.get(aid, {}), **entry.get("detail", {})}
        # An allow entry still names an action if its main record was deleted.
        # Its frozen chain and target outcome are now missing, not inferred.
        actions.append({
            "action_id": aid, "principal": entry["principal"], "permission_id": entry.get("mandate_id"),
            "chain_ids": [], "chain": [], "decision": "allowed", "outcome": "unknown", "missing": True,
            "tool": detail.get("tool"), "target": detail.get("target"), "args": detail.get("args", {}),
            "policy_version": entry.get("policy_version"), "policy_hash": entry.get("policy_hash"),
            "counter": entry.get("seq"), "at": entry["at"], "reason": "missing_record", "stopped": None,
            "evidence": None, "state": None, "reverses": None, "undo": None,
            "undone_by": [other["_id"] for other in effects if other.get("reverses") == aid], "audit": audit_by_action[aid],
        })
        existing.add(aid)
    refusals = []
    for entry in audit:
        if entry["kind"] != "deny":
            continue
        aid = entry.get("action_id")
        detail = {**plans.get(aid, {}), **entry.get("detail", {})}
        ids = chain_ids(entry.get("mandate_id"))
        refusal = {
            "action_id": aid, "principal": entry["principal"], "permission_id": entry.get("mandate_id"),
            "chain_ids": ids, "chain": describe_chain(ids), "decision": "refused", "outcome": "blocked",
            "tool": detail.get("tool"), "target": detail.get("target"), "args": detail.get("args", {}),
            "policy_version": entry.get("policy_version"), "policy_hash": entry.get("policy_hash"),
            "counter": entry["seq"], "at": entry["at"], "reason": entry["reason"], "detail": entry.get("detail", {}),
            "stopped": stopped(entry.get("mandate_id"), detail) if entry["reason"] in {"revoked", "cancelled_by_stop"} else None,
            "evidence": None, "state": None, "reverses": None, "undo": None, "undone_by": [], "audit": audit_by_action[aid],
        }
        refusals.append(refusal)
        if aid and aid not in existing:
            actions.append(refusal)
            existing.add(aid)
    actions.sort(key=lambda row: _time_key(row, "at"))
    by_id = {row["action_id"]: row for row in actions if row["state"] is not None}
    stops = []
    for receipt in receipts:
        stops.append({
            "permission_id": receipt["mandate_id"], "by": receipt["stopped_by"], "counter": receipt["stop_seq"],
            "at": receipt["stopped_at"], "receipt": receipt,
            "covered": [by_id.get(aid, {"action_id": aid, "outcome": "unknown", "state": None, "missing": True}) for aid in receipt["covered"]],
            "refusals": [row for row in refusals if row["permission_id"] in receipt["revoked"] and row["at"] >= receipt["stopped_at"]],
        })
    for entry in audit:
        if entry["kind"] == "stop" and not any(r["mandate_id"] == entry.get("mandate_id") and r["stop_seq"] == entry["seq"] for r in receipts):
            stops.append({"permission_id": entry.get("mandate_id"), "by": entry["principal"], "counter": entry["seq"],
                          "at": entry["at"], "receipt": None, "covered": [], "refusals": []})
    stops.sort(key=lambda row: _time_key(row, "at"))
    return _json_value({
        "run": run, "permissions": forest, "actions": actions, "refusals": refusals, "stops": stops,
        "checks": check_records(run, permissions, effects, audit, receipts, policies),
        "policies": policies, "tasks": tasks, "audit": audit,
        "notice": "No check before actions: workers wrote actions directly." if run["mode"] != "stopline" else "Reconstructed from stored records in one database snapshot.",
    })
