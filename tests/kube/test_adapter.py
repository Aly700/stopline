"""Transport checks run without a cluster; live behavior is in test_live.py."""

import importlib
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from stopline.contract import Action, TargetTimeout, TargetUnreachable, Tool


@pytest.fixture
def kube():
    return importlib.import_module("stopline.targets.kube").KubeCluster()


def test_state_handles_missing_kubectl(kube, monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("kubectl")

    monkeypatch.setattr(subprocess, "run", missing)
    assert kube.state()["reachable"] is False


def test_lookup_connection_failure_is_unreachable(kube, monkeypatch):
    def refused(command, **kwargs):
        assert command[:5] == ["kubectl", "--context", "kind-stopline", "-n", "checkout"]
        assert "-o" in command and "json" in command
        assert 0 < kwargs["timeout"] <= 5
        return subprocess.CompletedProcess(command, 1, "", "connection refused")

    monkeypatch.setattr(subprocess, "run", refused)
    with pytest.raises(TargetUnreachable):
        kube.lookup("missing")


@pytest.mark.parametrize("status", [
    {"observedGeneration": 1, "readyReplicas": 1, "updatedReplicas": 1, "replicas": 1},
    {"observedGeneration": 2, "readyReplicas": 1, "updatedReplicas": 1, "replicas": 2},
])
def test_state_does_not_call_old_replicas_ready(kube, monkeypatch, status):
    deployment = {
        "kind": "Deployment",
        "metadata": {"name": "checkout-api", "generation": 2, "annotations": {}},
        "spec": {"replicas": 1, "template": {
            "metadata": {"annotations": {}},
            "spec": {"containers": [{"image": "stopline/checkout:v1"}]},
        }},
        "status": status,
    }

    def response(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, json.dumps({"items": [deployment]}), "")

    monkeypatch.setattr(subprocess, "run", response)
    assert kube.state()["deployments"]["checkout-api"]["ready"] is False


def test_cut_blocks_calls_without_starting_kubectl(kube, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("cut adapter started a subprocess")

    monkeypatch.setattr(subprocess, "run", unexpected)
    request = Action("cut-id", "test", "test", "test", Tool.RESTART, "cluster/checkout-worker", {})
    kube.cut()
    for call in (lambda: kube.send(request), lambda: kube.lookup("cut-id"), lambda: kube.fence("cut-id")):
        with pytest.raises(TargetUnreachable):
            call()
    assert kube.state() == {"system": "cluster", "reachable": False, "deployments": {}}


@pytest.mark.parametrize("stderr,exception", [
    ("Unable to connect to the server: dial tcp: i/o timeout", TargetUnreachable),
    ("The connection to the server 127.0.0.1:6443 was refused", TargetUnreachable),
    ("Error from server (Timeout): request timed out", TargetTimeout),
    ("error: unexpected EOF", TargetTimeout),
    ("Unable to connect to the server: EOF", TargetTimeout),
    ("Unable to connect to the server: read tcp: connection reset by peer", TargetTimeout),
])
def test_patch_failures_distinguish_connection_from_unknown_outcome(kube, monkeypatch, stderr, exception):
    import time

    def failed(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, "", stderr)

    monkeypatch.setattr(subprocess, "run", failed)
    with pytest.raises(exception):
        kube._run("patch", "deployment", "checkout-api", deadline=time.monotonic() + 5, patch=True)


@pytest.mark.parametrize("tool", [Tool.ROLLBACK, Tool.RESTART])
def test_cluster_actions_have_no_inverse(kube, tool):
    request = Action("no-undo", "test", "test", "test", tool, "cluster/checkout-api", {})
    assert kube.inverse_of(request) is None


@pytest.mark.parametrize("cut", [False, True])
def test_send_deadline_includes_waiting_for_another_mutation(kube, monkeypatch, cut):
    def unexpected(*args, **kwargs):
        pytest.fail("a cut or expired send must not start a subprocess")

    monkeypatch.setattr(subprocess, "run", unexpected)
    request = Action("busy-id", "test", "test", "test", Tool.RESTART, "cluster/checkout-worker", {})
    if cut:
        kube.cut()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with kube._lock:
            started = time.monotonic()
            pending = pool.submit(kube.send, request)
            with pytest.raises(TargetUnreachable):
                pending.result(timeout=0.5 if cut else 5.75)
            assert time.monotonic() - started < (0.5 if cut else 5.75)
