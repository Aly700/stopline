"""The repair agent saves its decision before delegating narrower authority."""

from stopline.contract import DEMO_PLAN, REPAIR_AGENT, TaskStatus, WORKER_TWO
from .planner import ScriptedPlanner, WORKERS, prepare_steps
from .worker import utcnow


class RepairAgent:
    def __init__(self, db, gate, planner):
        self.db, self.gate, self.planner = db, gate, planner

    def prepare(self, run_id, root):
        handed = self.gate.hand_down(
            parent_id=root["_id"], holder=REPAIR_AGENT, tools=root["tools"], targets=root["targets"],
            limits={"max_actions": 4, "max_shift_percent": 50}, ttl_seconds=1800,
        )
        if not handed.allowed:
            raise RuntimeError("repair permission was refused")
        self.db.runs.update_one({"_id": run_id}, {"$set": {
            "root_mandate_id": root["_id"], "agent_mandate_id": handed.mandate_id,
        }})
        mandate = self.db.mandates.find_one({"_id": handed.mandate_id})
        saved = ScriptedPlanner().plan(run_id, mandate)
        tasks = [{
            "_id": f"{run_id}:{principal}", "run_id": run_id, "principal": principal,
            "mandate_id": None, "plan": saved.steps[principal], "position": 0,
            "planner": "scripted", "status": TaskStatus.RUNNING, "held_at": None,
            "pid": None, "note": "Saved a backup repair. Waiting for permission.", "updated_at": utcnow(),
        } for principal in WORKERS]
        tasks.append({
            "_id": f"{run_id}:{REPAIR_AGENT}", "run_id": run_id, "principal": REPAIR_AGENT,
            "mandate_id": handed.mandate_id, "plan": [s for t in tasks for s in t["plan"]],
            "position": 0, "planner": "scripted", "status": TaskStatus.RUNNING,
            "held_at": None, "pid": None, "note": "Planning the checkout repair.", "updated_at": utcnow(),
        })
        self.db.tasks.insert_many(tasks)
        chosen = self.planner.plan(run_id, mandate)
        steps = prepare_steps(run_id, chosen.steps)
        for principal in WORKERS:
            allowed = DEMO_PLAN[principal]
            scope = {
                "tools": sorted({str(s["tool"]) for s in allowed}),
                "targets": sorted({s["target"] for s in allowed}),
                "limits": {"max_actions": 2, "max_shift_percent": 50 if principal == WORKER_TWO else 0},
            }
            child = self.gate.hand_down(parent_id=handed.mandate_id, holder=principal,
                                        ttl_seconds=900, **scope)
            if not child.allowed:
                raise RuntimeError("worker permission was refused")
            self.db.tasks.update_one({"_id": f"{run_id}:{principal}"}, {"$set": {
                "mandate_id": child.mandate_id, "plan": steps[principal], "planner": chosen.source,
                "fixed_rules": scope, "note": "Saved the repair steps and received a narrower permission.",
                "updated_at": utcnow(),
            }})
        self.db.tasks.update_one({"_id": f"{run_id}:{REPAIR_AGENT}"}, {"$set": {
            "plan": [s for plan in steps.values() for s in plan], "position": 4,
            "planner": chosen.source, "status": TaskStatus.DONE,
            "note": f"{chosen.note} Handed down narrower permissions to both workers.", "updated_at": utcnow(),
        }})
        self.db.runs.update_one({"_id": run_id}, {"$set": {
            "planner": chosen.source, "planner_note": chosen.note,
        }})
