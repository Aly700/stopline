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

import pytest

from stopline.contract import Action, TARGET_API, TARGET_WORKER, Tool


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
