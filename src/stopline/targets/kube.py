"""The two demo Deployments, with action evidence kept by Kubernetes itself.

The sender is the only writer during a run. A patch changes the pod template
and its action record together. ReplicaSets retain that record after a later
action changes the Deployment. No rollout wait is needed to acknowledge send.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import threading
import time
from typing import Any

from stopline.contract import (
    Action, CLUSTER, Inverse, KUBE_CONTEXT, KUBE_NAMESPACE, Lookup, Reason,
    SendResult, TargetTimeout, TargetUnreachable, Tool, args_hash,
)


ACTION_ID = "stopline.dev/action-id"
ARGS_HASH = "stopline.dev/args-hash"
RECORD = "stopline.dev/action"
RESTARTED_AT = "kubectl.kubernetes.io/restartedAt"
REVISION = "deployment.kubernetes.io/revision"
DEPLOYMENTS = ("checkout-api", "checkout-worker")
LABEL = "app.kubernetes.io/part-of=stopline"


class KubeCluster:
    name = CLUSTER
    # Multiple request threads and adapter instances share the single sender.
    _lock = threading.RLock()

    def __init__(self) -> None:
        self._cut = False

    def _run(self, *args: str, deadline: float, patch: bool = False) -> dict[str, Any]:
        if self._cut:
            raise TargetUnreachable("cluster connection is cut")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TargetUnreachable("cluster request deadline elapsed before sending")
        command = [
            "kubectl", "--context", KUBE_CONTEXT, "-n", KUBE_NAMESPACE,
            f"--request-timeout={max(1, int(remaining * 1000))}ms",
            *args, "-o", "json",
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=remaining)
        except subprocess.TimeoutExpired as error:
            exception = TargetTimeout if patch else TargetUnreachable
            raise exception("cluster patch outcome is unknown" if patch else "cluster read timed out") from error
        except OSError as error:
            raise TargetUnreachable("kubectl could not start") from error
        if result.returncode:
            message = result.stderr.strip()
            if patch and any(word in message.lower() for word in ("timeout", "timed out", "deadline exceeded")):
                raise TargetTimeout("cluster patch outcome is unknown")
            raise TargetUnreachable(f"kubectl failed: {message}")
        try:
            return json.loads(result.stdout)
        except (ValueError, TypeError) as error:
            exception = TargetTimeout if patch else TargetUnreachable
            raise exception("kubectl returned an unreadable response") from error

    def _inventory(self, deadline: float) -> tuple[dict[str, dict], list[dict]]:
        items = self._run("get", "deployments,replicasets", "-l", LABEL, deadline=deadline)["items"]
        deployments = {item["metadata"]["name"]: item for item in items
                       if item["kind"] == "Deployment" and item["metadata"]["name"] in DEPLOYMENTS}
        uids = {item["metadata"].get("uid") for item in deployments.values()}
        replicasets = [item for item in items if item["kind"] == "ReplicaSet" and any(
            owner.get("uid") in uids and owner.get("kind") == "Deployment"
            for owner in item["metadata"].get("ownerReferences", [])
        )]
        return deployments, replicasets

    @staticmethod
    def _annotations(item: dict) -> dict:
        return item["spec"]["template"]["metadata"].get("annotations", {})

    def _find(self, action_id: str, items: list[dict]) -> dict | None:
        for item in items:
            annotations = self._annotations(item)
            if annotations.get(ACTION_ID) == action_id:
                return json.loads(annotations[RECORD])
        return None

    @staticmethod
    def _evidence(action_id: str, **fields: Any) -> dict[str, Any]:
        return {"action_id": action_id, "system": CLUSTER, "at_epoch": time.time(), **fields}

    def send(self, action: Action) -> SendResult:
        with self._lock:
            deadline = time.monotonic() + 5
            deployments, replicasets = self._inventory(deadline)
            record = self._find(action.action_id, [*deployments.values(), *replicasets])
            digest = args_hash(action.args)
            if record:
                same = (record["args_hash"] == digest and record["tool"] == action.tool
                        and record["target"] == action.target)
                return SendResult(same, record["evidence"], Reason.OK if same else Reason.SAME_ID_DIFFERENT_ARGS)
            name = action.target.removeprefix("cluster/")
            if action.target != f"cluster/{name}" or name not in DEPLOYMENTS:
                return SendResult(False, self._evidence(action.action_id), Reason.TARGET_NOT_ALLOWED)
            if action.tool not in (Tool.ROLLBACK, Tool.RESTART):
                return SendResult(False, self._evidence(action.action_id), Reason.TOOL_NOT_ALLOWED)
            if (action.tool == Tool.ROLLBACK and action.args != {"to_version": "v1"}) or (
                action.tool == Tool.RESTART and action.args
            ):
                return SendResult(False, self._evidence(action.action_id), Reason.TOOL_NOT_ALLOWED)
            if name not in deployments:
                raise TargetUnreachable(f"Deployment {name} is missing; run k8s/up.sh")
            deployment = deployments[name]
            annotations = dict(self._annotations(deployment))
            image = deployment["spec"]["template"]["spec"]["containers"][0]["image"]
            if action.tool == Tool.ROLLBACK:
                image = "stopline/checkout:v1"
            else:
                annotations[RESTARTED_AT] = datetime.now(timezone.utc).isoformat(timespec="microseconds")
            evidence = self._evidence(action.action_id, deployment=name,
                                      generation=deployment["metadata"]["generation"] + 1, image=image)
            record = {"tool": action.tool, "target": action.target, "args_hash": digest, "evidence": evidence}
            annotations.update({ACTION_ID: action.action_id, ARGS_HASH: digest, RECORD: json.dumps(record)})
            patch = [
                {"op": "test", "path": "/metadata/resourceVersion", "value": deployment["metadata"]["resourceVersion"]},
                {"op": "add", "path": "/spec/template/metadata/annotations", "value": annotations},
                {"op": "replace", "path": "/spec/template/spec/containers/0/image", "value": image},
            ]
            updated = self._run("patch", "deployment", name, "--type=json", "-p", json.dumps(patch),
                                deadline=deadline, patch=True)
            confirmed = self._find(action.action_id, [updated])
            if not confirmed:
                raise TargetTimeout("patch returned without the expected action evidence")
            return SendResult(True, confirmed["evidence"])

    def lookup(self, action_id: str) -> Lookup:
        deployments, replicasets = self._inventory(time.monotonic() + 5)
        record = self._find(action_id, [*deployments.values(), *replicasets])
        if record:
            return Lookup(True, True, record["evidence"])
        return Lookup(False, False, self._evidence(action_id))

    def state(self) -> dict[str, Any]:
        try:
            deployments, _ = self._inventory(time.monotonic() + 5)
            state = {}
            for name, deployment in deployments.items():
                annotations = self._annotations(deployment)
                image = deployment["spec"]["template"]["spec"]["containers"][0]["image"]
                status = deployment.get("status", {})
                replicas = deployment["spec"].get("replicas", 1)
                state[name] = {
                    "image": image, "version": image.rsplit(":", 1)[-1],
                    "revision": deployment["metadata"].get("annotations", {}).get(REVISION),
                    "restarted_at": annotations.get(RESTARTED_AT), "action_id": annotations.get(ACTION_ID),
                    "ready": (status.get("observedGeneration", 0) >= deployment["metadata"]["generation"]
                              and status.get("updatedReplicas", 0) == replicas
                              and status.get("readyReplicas", 0) == replicas),
                }
            return {"system": CLUSTER, "reachable": True, "deployments": state}
        except Exception:
            return {"system": CLUSTER, "reachable": False, "deployments": {}}

    def inverse_of(self, action: Action) -> Inverse | None:
        return None

    def restore(self) -> None:
        self._cut = False

    def reset(self) -> None:
        with self._lock:
            self.restore()
            script = Path(__file__).resolve().parents[3] / "k8s" / "reset.sh"
            try:
                subprocess.run([str(script)], capture_output=True, text=True, check=True, timeout=9.5)
            except (OSError, subprocess.SubprocessError) as error:
                raise TargetUnreachable("cluster reset did not finish; run k8s/reset.sh for details") from error
