import json

import pytest

from stopline.report import build_report

from .records import NOW, POLICY_HASH, RUN


def check(report, name):
    return next(item for item in report["checks"] if item["id"] == name)


def test_reconstructs_demo_from_records_alone(demo):
    result = build_report(demo, RUN)
    assert result["run"]["mode"] == "stopline"
    assert all(item["passed"] for item in result["checks"])
    assert len(result["checks"]) == 6
    root = result["permissions"][0]
    assert root["holder"] == "person:operator"
    repair = root["children"][0]
    assert repair["holder"] == "agent:repair"
    assert {child["holder"] for child in repair["children"]} == {"agent:worker-1", "agent:worker-2"}
    assert repair["stopped"]["by"] == "person:operator"
    assert repair["stopped"]["counter"] == 7
    assert repair["stopped"]["at"].endswith("Z")
    assert repair["limits"]["max_shift_percent"] == 25
    rows = {row["action_id"]: row for row in result["actions"]}
    assert list(rows) == ["rollback", "shift-25", "restart", "shift-50", "undo-shift"]
    assert rows["rollback"]["outcome"] == "completed"
    assert rows["restart"]["outcome"] == "blocked"
    assert rows["shift-25"]["args"] == {"percent": 25}
    assert rows["shift-25"]["evidence"]["weights"]["region-b"] == 25
    assert rows["shift-25"]["policy_hash"] == POLICY_HASH
    assert [node["holder"] for node in rows["shift-25"]["chain"]] == ["person:operator", "agent:repair", "agent:worker-2"]
    assert rows["shift-50"]["decision"] == "refused"
    assert rows["shift-50"]["args"]["percent"] == 50
    assert rows["shift-50"]["stopped"]["by"] == "person:operator"
    assert rows["undo-shift"]["reverses"] == "shift-25"
    assert rows["shift-25"]["undone_by"] == ["undo-shift"]
    assert result["stops"][0]["receipt"]["covered"] == ["rollback", "shift-25", "restart"]
    assert result["stops"][0]["receipt"]["status"] == "closed"
    assert [row["outcome"] for row in result["stops"][0]["covered"]] == ["completed", "completed", "blocked"]
    assert len(result["refusals"]) == 1
    assert len(result["tasks"]) == 2
    json.dumps(result)  # datetime and ObjectId values must be usable by the JSON endpoint.


@pytest.mark.parametrize("tamper,check_id,action_id", [
    ("missing_allow", "allowed_actions_audited", "rollback"),
    ("after_stop", "no_admission_after_stop", "restart"),
    ("closed_unknown", "closed_receipts_settled", "shift-25"),
    ("missing_covered", "covered_actions_exist", "rollback"),
    ("repeated_counter", "unique_root_counters", "restart"),
    ("unknown_policy", "audit_policies_exist", "rollback"),
])
def test_tampering_fails_and_names_the_action(demo, tamper, check_id, action_id):
    if tamper == "missing_allow":
        demo.audit.delete_one({"kind": "allow", "action_id": action_id})
    elif tamper == "after_stop":
        demo.effects.update_one({"_id": action_id}, {"$set": {"admitted_seq": 9}})
    elif tamper == "closed_unknown":
        demo.effects.update_one({"_id": action_id}, {"$set": {"state": "sent", "evidence": None}})
    elif tamper == "missing_covered":
        demo.effects.delete_one({"_id": action_id})
    elif tamper == "repeated_counter":
        demo.effects.update_one({"_id": action_id}, {"$set": {"admitted_seq": 4}})
    else:
        demo.audit.update_one({"kind": "allow", "action_id": action_id}, {"$set": {"policy_hash": "not-the-stored-hash"}})
    failure = check(build_report(demo, RUN), check_id)
    assert failure["passed"] is False
    assert action_id in failure["action_ids"]
    assert action_id in " ".join(failure["violations"])


def test_unknown_run_is_explicit(db):
    with pytest.raises(LookupError, match="Run not found"):
        build_report(db, "missing")


def test_other_runs_do_not_contaminate_checks(demo):
    demo.effects.insert_one({"_id": "foreign-action", "run_id": "other-run"})
    demo.audit.insert_one({"run_id": "other-run", "kind": "allow", "at": NOW})
    assert all(item["passed"] for item in build_report(demo, RUN)["checks"])


def test_read_only_report_leaves_all_records_unchanged(demo):
    names = ("runs", "mandates", "policies", "effects", "audit", "receipts", "tasks")
    before = {name: list(demo[name].find({})) for name in names}
    build_report(demo, RUN)
    assert before == {name: list(demo[name].find({})) for name in names}
