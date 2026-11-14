"""Transport checks run without a cluster; live behavior is in test_live.py."""

import importlib
import json
import subprocess

import pytest

from stopline.contract import TargetUnreachable


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


def test_state_does_not_call_old_replicas_ready(kube, monkeypatch):
    deployment = {
        "kind": "Deployment",
        "metadata": {"name": "checkout-api", "generation": 2, "annotations": {}},
        "spec": {"replicas": 1, "template": {
            "metadata": {"annotations": {}},
            "spec": {"containers": [{"image": "stopline/checkout:v1"}]},
        }},
        "status": {"observedGeneration": 1, "readyReplicas": 1, "updatedReplicas": 1},
    }

    def response(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, json.dumps({"items": [deployment]}), "")

    monkeypatch.setattr(subprocess, "run", response)
    assert kube.state()["deployments"]["checkout-api"]["ready"] is False
