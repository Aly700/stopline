"""Contract-shaped records, independent of the gate and target implementations."""

import hashlib
from datetime import datetime, timedelta, timezone

from stopline.contract import DEMO_PLAN, OPERATOR, REPAIR_AGENT, WORKER_ONE, WORKER_TWO, args_hash

RUN = "demo-run"
NOW = datetime(2026, 11, 14, 19, 0, tzinfo=timezone.utc)
POLICY_TEXT = "Check identity, permission scope, expiry, shared limits and stops before admission."
POLICY_HASH = hashlib.sha256(POLICY_TEXT.encode()).hexdigest()


def moment(seq):
    return NOW + timedelta(seconds=seq)


def insert_demo(db, run_id=RUN):
    db.runs.insert_one({
        "_id": run_id, "mode": "stopline", "cluster": "mock", "status": "done",
        "root_mandate_id": "root", "agent_mandate_id": "repair", "stop_flag": True,
        "stop_at": moment(7), "stop_at_local": moment(7).timestamp(), "holds": [],
        "receipt_id": "receipt-1", "planner": "scripted", "created_at": NOW,
    })
    db.policies.insert_one({"_id": 1, "text": POLICY_TEXT, "hash": POLICY_HASH, "created_at": NOW})
    for permission_id, parent, holder, grantor, depth, budget in [
        ("root", None, OPERATOR, OPERATOR, 0, 12),
        ("repair", "root", REPAIR_AGENT, OPERATOR, 1, 6),
        ("worker-1", "repair", WORKER_ONE, REPAIR_AGENT, 2, 3),
        ("worker-2", "repair", WORKER_TWO, REPAIR_AGENT, 2, 3),
    ]:
        stopped = parent is not None
        row = {
            "_id": permission_id, "run_id": run_id, "root_id": "root", "parent_id": parent,
            "depth": depth, "grantor": grantor, "holder": holder,
            "tools": ["rollback", "restart", "shift_traffic"],
            "targets": ["cluster/checkout-api", "cluster/checkout-worker", "router/region-b"],
            "limits": {"max_actions": budget, "max_shift_percent": 25},
            "expires_at": moment(3600), "status": "revoked" if stopped else "active",
            "revoked_by": OPERATOR if stopped else None,
            "revoked_at_seq": 7 if stopped else None, "revoked_at": moment(7) if stopped else None,
            "created_at": NOW,
        }
        if parent is None:
            row.update(seq=8, clock=moment(8))
        db.mandates.insert_one(row)

    def audit(kind, seq, permission, principal, action_id=None, detail=None, reason="ok", delay=0):
        db.audit.insert_one({
            "run_id": run_id, "kind": kind, "principal": principal, "mandate_id": permission,
            "root_id": "root", "action_id": action_id, "policy_version": 1,
            "policy_hash": POLICY_HASH, "reason": reason, "seq": seq,
            "at": moment(seq) + timedelta(milliseconds=delay), "detail": detail or {},
        })

    for seq, permission, principal in [(1, "repair", OPERATOR), (2, "worker-1", REPAIR_AGENT), (3, "worker-2", REPAIR_AGENT)]:
        audit("hand_down", seq, permission, principal)

    for action_id, principal, permission, step, seq, state, reverses in [
        ("rollback", WORKER_ONE, "worker-1", DEMO_PLAN[WORKER_ONE][0], 4, "applied", None),
        ("shift-25", WORKER_TWO, "worker-2", DEMO_PLAN[WORKER_TWO][0], 5, "applied", None),
        ("restart", WORKER_ONE, "worker-1", DEMO_PLAN[WORKER_ONE][1], 6, "cancelled", None),
        ("undo-shift", OPERATOR, "root", {"tool": "shift_traffic", "target": "router/region-b", "args": {"percent": 0}}, 8, "applied", "shift-25"),
    ]:
        evidence = None if state == "cancelled" else {
            "action_id": action_id, "system": step["target"].split("/")[0],
            "at_epoch": moment(seq + 2).timestamp(), "applied": True,
            **({"weights": {"region-a": 75, "region-b": 25}} if action_id == "shift-25" else {}),
        }
        db.effects.insert_one({
            "_id": action_id, "run_id": run_id, "mode": "stopline", "root_id": "root",
            "mandate_id": permission, "chain": ["root"] if permission == "root" else ["root", "repair", permission],
            "principal": principal, **step, "args_hash": args_hash(step["args"]), "admitted_seq": seq,
            "state": state, "evidence": evidence, "reverses": reverses,
            "undo": "undone" if action_id == "shift-25" else None,
            "policy_version": 1, "policy_hash": POLICY_HASH,
            "claimed_by": None if state == "cancelled" else "sender",
            "admitted_at": moment(seq), "sent_at": None if state == "cancelled" else moment(seq),
            "finished_at": moment(7) if state == "cancelled" else moment(seq + 2),
        })
        audit("allow", seq, permission, principal, action_id, dict(step))
        if state != "cancelled":
            audit("claim", seq, permission, "sender", action_id, delay=100)
            audit("outcome", seq, permission, "sender", action_id, {"state": state, "evidence": evidence}, delay=200)
    audit("stop", 7, "repair", OPERATOR, detail={"receipt_id": "receipt-1"})
    audit("deny", 7, "worker-2", WORKER_TWO, "shift-50", dict(DEMO_PLAN[WORKER_TWO][1]), "revoked", delay=500)
    db.receipts.insert_one({
        "_id": "receipt-1", "run_id": run_id, "root_id": "root", "mandate_id": "repair",
        "stopped_by": OPERATOR, "stop_seq": 7, "stopped_at": moment(7),
        "revoked": ["repair", "worker-1", "worker-2"], "covered": ["rollback", "shift-25", "restart"],
        "status": "closed", "closed_at": moment(9),
    })
    for principal, permission, ids in [(WORKER_ONE, "worker-1", ["rollback", "restart"]), (WORKER_TWO, "worker-2", ["shift-25", "shift-50"])]:
        db.tasks.insert_one({
            "_id": f"{run_id}:{principal}", "run_id": run_id, "principal": principal,
            "mandate_id": permission, "plan": [{"action_id": aid, **step} for aid, step in zip(ids, DEMO_PLAN[principal])],
            "position": 2, "planner": "scripted", "status": "stopped", "held_at": None,
            "pid": None, "note": "Stopped by the operator", "updated_at": moment(9),
        })
