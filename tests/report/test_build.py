import json

import pytest

from stopline.report import build_report, render_report

from .records import NOW, POLICY_HASH, RUN, moment


def check(report, name):
    return next(item for item in report["checks"] if item["id"] == name)


def test_reconstructs_demo_from_records_alone(demo):
    result = build_report(demo, RUN)
    assert result["run"]["mode"] == "stopline"
    assert all(item["passed"] for item in result["checks"])
    assert len(result["checks"]) == 7
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


def test_audit_order_keeps_whole_seconds_before_fractional_seconds(demo):
    report = build_report(demo, RUN)
    entries = [entry for entry in report["audit"] if entry["action_id"] == "rollback"]
    assert [entry["kind"] for entry in entries] == ["allow", "claim", "outcome"]


def test_missing_policy_fields_fail_without_breaking_reconstruction(demo):
    demo.audit.update_one({"kind": "deny"}, {"$unset": {"policy_version": "", "policy_hash": ""}})
    result = build_report(demo, RUN)
    assert check(result, "audit_policies_exist")["passed"] is False
    assert "shift-50" in check(result, "audit_policies_exist")["action_ids"]
    assert "Policy missing" in render_report(result)


def test_open_receipt_keeps_sent_action_unknown(demo):
    demo.effects.update_one({"_id": "shift-25"}, {"$set": {"state": "sent", "evidence": None}})
    demo.receipts.update_one({"_id": "receipt-1"}, {"$set": {"status": "open", "closed_at": None}})
    result = build_report(demo, RUN)
    assert all(item["passed"] for item in result["checks"])
    assert next(row for row in result["actions"] if row["action_id"] == "shift-25")["outcome"] == "unknown"


def test_stopped_ancestor_is_checked_even_if_the_frozen_chain_is_missing(demo):
    demo.effects.update_one({"_id": "restart"}, {"$set": {"admitted_seq": 9, "chain": []}})
    demo.mandates.update_one({"_id": "worker-1"}, {"$set": {"revoked_at_seq": None}})
    demo.receipts.update_one({"_id": "receipt-1"}, {"$set": {"revoked": ["repair"]}})
    assert "restart" in check(build_report(demo, RUN), "no_admission_after_stop")["action_ids"]


def test_stop_check_also_checks_the_audits_admission_counter(demo):
    demo.audit.update_one({"kind": "allow", "action_id": "restart"}, {"$set": {"seq": 9}})
    assert "restart" in check(build_report(demo, RUN), "no_admission_after_stop")["action_ids"]


def test_deleted_action_remains_visible_from_its_allow_entry(demo):
    demo.effects.delete_one({"_id": "rollback"})
    result = build_report(demo, RUN)
    row = next(row for row in result["actions"] if row["action_id"] == "rollback")
    assert row["decision"] == "allowed"
    assert row["outcome"] == "unknown"
    assert row["missing"] is True
    assert row["chain"] == []  # The frozen chain disappeared with the action record.
    assert check(result, "covered_actions_exist")["passed"] is False


def test_independent_roots_can_use_the_same_counter_value(demo):
    root = demo.mandates.find_one({"_id": "root"})
    root.update(_id="other-root", root_id="other-root", seq=1)
    demo.mandates.insert_one(root)
    action = demo.effects.find_one({"_id": "rollback"})
    action.update(_id="other-action", root_id="other-root", mandate_id="other-root", chain=["other-root"], admitted_seq=1)
    demo.effects.insert_one(action)
    entry = demo.audit.find_one({"kind": "allow", "action_id": "rollback"})
    del entry["_id"]
    entry.update(action_id="other-action", root_id="other-root", mandate_id="other-root", seq=1)
    demo.audit.insert_one(entry)
    assert all(item["passed"] for item in build_report(demo, RUN)["checks"])


@pytest.mark.parametrize("tamper,word", [("delete", "missing"), ("copy", "repeated")])
def test_decision_counters_name_missing_or_repeated_values(demo, tamper, word):
    entry = demo.audit.find_one({"kind": "allow", "action_id": "rollback"})
    if tamper == "delete":
        demo.audit.delete_one({"_id": entry["_id"]})
    else:
        del entry["_id"]
        demo.audit.insert_one(entry)
    result = check(build_report(demo, RUN), "decision_counters_complete")
    assert result["passed"] is False
    assert any(word in failure and "[4]" in failure and "root" in failure for failure in result["violations"])


def test_replay_consumes_a_counter_without_readmitting_or_duplicating_action(demo):
    entry = demo.audit.find_one({"kind": "allow", "action_id": "rollback"})
    del entry["_id"]
    entry.update(seq=9, at=moment(9), detail={**entry["detail"], "replayed": True})
    demo.audit.insert_one(entry)
    demo.mandates.update_one({"_id": "root"}, {"$set": {"seq": 9}})
    result = build_report(demo, RUN)
    assert len(result["checks"]) == 7
    assert all(item["passed"] for item in result["checks"])
    assert len(result["actions"]) == 5
    assert [row["counter"] for row in result["actions"] if row["action_id"] == "rollback"] == [4]


@pytest.mark.parametrize("counter,word,value", [(9, "missing", 9), (7, "outside", 8)])
def test_decision_counters_use_the_roots_stored_counter(demo, counter, word, value):
    demo.mandates.update_one({"_id": "root"}, {"$set": {"seq": counter}})
    result = check(build_report(demo, RUN), "decision_counters_complete")
    assert result["passed"] is False
    assert any(word in failure and f"[{value}]" in failure for failure in result["violations"])
