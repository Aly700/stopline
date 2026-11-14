"""Uses only the dedicated checkout namespace on kind-stopline.

Run k8s/up.sh first. Each test resets the two demo Deployments; never run these
tests during a live demonstration.
"""

import importlib
import json
from pathlib import Path
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from stopline.contract import Action, Reason, TARGET_API, TARGET_WORKER, TargetTimeout, TargetUnreachable, Tool, args_hash


ROOT = Path(__file__).resolve().parents[2]
ACTION_ID = "stopline.dev/action-id"


def kubectl(*args):
    result = subprocess.run(
        ["kubectl", "--context", "kind-stopline", "-n", "checkout",
         "--request-timeout=3s", *args, "-o", "json"],
        capture_output=True, text=True, timeout=5, check=True,
    )
    return json.loads(result.stdout)


@pytest.fixture(scope="module", autouse=True)
def reachable_cluster():
    try:
        kubectl("get", "nodes")
    except (OSError, subprocess.SubprocessError):
        pytest.skip("kind-stopline is not reachable; start the dedicated cluster and run k8s/up.sh")


@pytest.fixture
def kube():
    adapter = importlib.import_module("stopline.targets.kube").KubeCluster()
    adapter.reset()
    yield adapter
    adapter.restore()
    adapter.reset()


def action(tool, target, args=None, action_id=None):
    return Action(
        action_id=action_id or f"kube-test-{uuid.uuid4().hex}", run_id="kube-test",
        mandate_id="test-mandate", principal="agent:worker-1", tool=tool,
        target=target, args=args or {},
    )


def eventually(check, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.1)
    pytest.fail("cluster did not reach the expected state within the deadline")


def assert_initial_state(kube):
    state = kube.state()
    assert state["system"] == "cluster"
    assert state["reachable"] is True
    assert set(state["deployments"]) == {"checkout-api", "checkout-worker"}
    for name, version in (("checkout-api", "v2"), ("checkout-worker", "v1")):
        deployment = state["deployments"][name]
        assert deployment["image"] == f"stopline/checkout:{version}"
        assert deployment["version"] == version
        assert deployment["action_id"] is None
        assert deployment["restarted_at"] is None
        assert deployment["ready"] is True


def test_up_is_repeatable_and_serves_versions(kube):
    subprocess.run([str(ROOT / "k8s/up.sh")], check=True, timeout=30, capture_output=True)
    assert_initial_state(kube)
    pods = kubectl("get", "pods", "-l", "app.kubernetes.io/part-of=stopline")["items"]
    assert len(pods) == 2
    for pod in pods:
        version = "v2" if pod["metadata"]["labels"]["app"] == "checkout-api" else "v1"
        result = subprocess.run(
            ["kubectl", "--context", "kind-stopline", "-n", "checkout", "--request-timeout=3s",
             "exec", pod["metadata"]["name"], "--", "wget", "-qO-", "http://127.0.0.1:8080"],
            capture_output=True, text=True, check=True, timeout=5,
        )
        assert result.stdout == f"checkout {version}\n"


@pytest.mark.parametrize("tool,target,args", [
    (Tool.ROLLBACK, TARGET_API, {"to_version": "v1"}),
    (Tool.RESTART, TARGET_WORKER, {}),
])
def test_send_has_deployment_and_replicaset_evidence(kube, tool, target, args):
    request = action(tool, target, args)
    started = time.monotonic()
    result = kube.send(request)
    assert time.monotonic() - started < 5.5
    assert result.applied is True
    assert result.evidence["action_id"] == request.action_id
    assert result.evidence["system"] == "cluster"
    assert abs(result.evidence["at_epoch"] - time.time()) < 10
    assert result.evidence["deployment"] == target.split("/")[1]
    assert result.evidence["generation"] >= 2
    assert result.evidence["image"] == "stopline/checkout:v1"
    assert kube.lookup(request.action_id).evidence == result.evidence
    deployment = kubectl("get", "deployment", target.split("/")[1])
    annotations = deployment["spec"]["template"]["metadata"]["annotations"]
    assert annotations[ACTION_ID] == request.action_id
    assert annotations["stopline.dev/args-hash"] == args_hash(request.args)

    def history_contains_id():
        return any(rs["spec"]["template"]["metadata"].get("annotations", {}).get(ACTION_ID)
                   == request.action_id for rs in kubectl("get", "replicasets")["items"])

    eventually(history_contains_id)
    state = kube.state()["deployments"][target.split("/")[1]]
    assert state["version"] == "v1"
    if tool == Tool.RESTART:
        assert state["restarted_at"]


def test_lookup_keeps_old_result_in_replicaset_history(kube):
    first = action(Tool.RESTART, TARGET_WORKER)
    original = kube.send(first)
    eventually(lambda: any(
        rs["spec"]["template"]["metadata"].get("annotations", {}).get(ACTION_ID) == first.action_id
        for rs in kubectl("get", "replicasets")["items"]
    ))
    kube.send(action(Tool.RESTART, TARGET_WORKER))
    fresh_adapter = type(kube)()
    lookup = fresh_adapter.lookup(first.action_id)
    assert lookup.found and lookup.applied
    assert lookup.evidence == original.evidence


def test_reset_clears_history_under_ten_seconds(kube):
    request = action(Tool.ROLLBACK, TARGET_API, {"to_version": "v1"})
    kube.send(request)
    started = time.monotonic()
    kube.reset()
    duration = time.monotonic() - started
    assert duration < 10
    print(f"reset completed in {duration:.2f}s")
    assert_initial_state(kube)
    assert kube.lookup(request.action_id).found is False


def test_repeat_applies_once_even_from_new_instances_and_threads(kube):
    request = action(Tool.RESTART, TARGET_WORKER)
    before = kubectl("get", "deployment", "checkout-worker")["metadata"]["generation"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: type(kube)().send(request), range(4)))
    assert all(result == results[0] and result.applied for result in results)
    assert kubectl("get", "deployment", "checkout-worker")["metadata"]["generation"] == before + 1
    kube.send(action(Tool.RESTART, TARGET_WORKER))
    generation = kubectl("get", "deployment", "checkout-worker")["metadata"]["generation"]
    assert type(kube)().send(request) == results[0]
    assert kubectl("get", "deployment", "checkout-worker")["metadata"]["generation"] == generation


def test_same_id_with_changed_arguments_tool_or_target_is_refused(kube):
    request = action(Tool.ROLLBACK, TARGET_API, {"to_version": "v1"})
    original = kube.send(request)
    for changed in (
        replace(request, args={"to_version": "v2"}),
        replace(request, tool=Tool.RESTART),
        replace(request, target=TARGET_WORKER),
    ):
        result = type(kube)().send(changed)
        assert not result.applied
        assert result.reason == Reason.SAME_ID_DIFFERENT_ARGS
    assert kube.lookup(request.action_id).evidence == original.evidence


def test_fence_survives_adapter_recreation_and_reset_clears_it(kube):
    request = action(Tool.RESTART, TARGET_WORKER)
    evidence = kube.fence(request.action_id)
    assert evidence["action_id"] == request.action_id
    assert evidence["system"] == "cluster"
    assert evidence["fenced"] is True
    assert abs(evidence["at_epoch"] - time.time()) < 10
    assert type(kube)().fence(request.action_id) == evidence
    for target in (TARGET_API, TARGET_WORKER):
        result = type(kube)().send(replace(request, target=target))
        assert not result.applied and result.reason == Reason.FENCED
    lookup = kube.lookup(request.action_id)
    assert lookup.found and not lookup.applied
    assert lookup.evidence == evidence
    started = time.monotonic()
    kube.reset()
    assert time.monotonic() - started < 10
    assert not kube.lookup(request.action_id).found
    assert kube.send(request).applied


def test_fence_does_not_hide_an_already_applied_action(kube):
    request = action(Tool.RESTART, TARGET_WORKER)
    result = kube.send(request)
    kube.fence(request.action_id)
    lookup = kube.lookup(request.action_id)
    assert lookup.applied and lookup.evidence == result.evidence
    replay = kube.send(request)
    assert not replay.applied and replay.reason == Reason.FENCED


def test_cut_and_restore(kube):
    request = action(Tool.ROLLBACK, TARGET_API, {"to_version": "v1"})
    kube.cut()
    with pytest.raises(TargetUnreachable):
        kube.send(request)
    assert kube.state()["reachable"] is False
    kube.restore()
    assert_initial_state(kube)
    assert kube.send(request).applied


def test_lost_patch_response_is_unknown_then_found_without_reapplying(kube, monkeypatch):
    request = action(Tool.ROLLBACK, TARGET_API, {"to_version": "v1"})
    run = subprocess.run
    patches = []

    def lose_response(command, **kwargs):
        result = run(command, **kwargs)
        if "patch" in command:
            assert result.returncode == 0, result.stderr
            patches.append(command)
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return result

    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "run", lose_response)
        with pytest.raises(TargetTimeout):
            kube.send(request)
    assert len(patches) == 1
    lookup = type(kube)().lookup(request.action_id)
    assert lookup.found and lookup.applied
    generation = kubectl("get", "deployment", "checkout-api")["metadata"]["generation"]
    result = type(kube)().send(request)
    assert result.applied and result.evidence == lookup.evidence
    assert kubectl("get", "deployment", "checkout-api")["metadata"]["generation"] == generation


def test_back_to_back_actions_leave_both_replicasets(kube):
    requests = [action(Tool.RESTART, TARGET_WORKER) for _ in range(3)]
    for request in requests:
        assert kube.send(request).applied
    expected = {request.action_id for request in requests}
    eventually(lambda: expected <= {
        rs["spec"]["template"]["metadata"].get("annotations", {}).get(ACTION_ID)
        for rs in kubectl("get", "replicasets")["items"]
    })
    assert all(type(kube)().lookup(request.action_id).applied for request in requests)


def test_send_accepts_without_rollout_but_keeps_unarchived_evidence(kube):
    kubectl("patch", "deployment", "checkout-worker", "--type=merge", "-p", '{"spec":{"paused":true}}')
    first = action(Tool.RESTART, TARGET_WORKER)
    started = time.monotonic()
    assert kube.send(first).applied
    assert time.monotonic() - started < 5
    # A paused controller cannot copy the first template into a ReplicaSet.
    # A second action must not erase that first action's only target record.
    with pytest.raises(TargetUnreachable):
        kube.send(action(Tool.RESTART, TARGET_WORKER))
    assert kube.lookup(first.action_id).applied


def test_send_retries_a_resource_version_conflict(kube, monkeypatch):
    request = action(Tool.RESTART, TARGET_WORKER)
    run = subprocess.run
    injected = False

    def change_resource_version(command, **kwargs):
        nonlocal injected
        if "patch" in command and not injected:
            injected = True
            kubectl("annotate", "deployment", "checkout-worker", "test-edit=conflict", "--overwrite")
        return run(command, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "run", change_resource_version)
        assert kube.send(request).applied
    assert injected
    assert kube.lookup(request.action_id).applied
