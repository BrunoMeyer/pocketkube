from __future__ import annotations

import asyncio
import copy
import random
import string
from typing import Any

from .models import ensure_metadata, now_rfc3339


def _suffix(n: int = 5) -> str:
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(n))


class Controllers:
    def __init__(self, state, runtime, node_name=None) -> None:
        self.state = state
        self.runtime = runtime
        self.node_name = node_name

    async def create_pod(self, namespace: str, pod: dict[str, Any], owner: dict[str, Any] | None = None) -> dict[str, Any]:
        pod = ensure_metadata(pod, namespace)
        if self.node_name:
            spec = pod.setdefault("spec", {})
            if spec.get("nodeName") not in (None, "", self.node_name):
                raise ValueError("PocketKube only supports node " + self.node_name)
            spec["nodeName"] = self.node_name
        meta = pod.setdefault("metadata", {})
        name = meta["name"]
        if owner:
            meta.setdefault("ownerReferences", []).append(owner)
        pod.setdefault("status", {})
        pod["status"].update({
            "phase": "Pending",
            "startTime": now_rfc3339(),
            "conditions": [],
            "containerStatuses": [],
        })
        await self.state.put("pods", namespace, name, pod)
        asyncio.create_task(self._start_pod(namespace, name))
        return pod

    async def _start_pod(self, namespace: str, name: str) -> None:
        pod = await self.state.get("pods", namespace, name)
        if not pod:
            return
        try:
            await self.runtime.start_pod(namespace, name, pod)
            container = (pod.get("spec", {}).get("containers") or [{}])[0]
            pod["status"].update({
                "phase": "Running",
                "conditions": [{"type": "Ready", "status": "True", "lastTransitionTime": now_rfc3339()}],
                "containerStatuses": [{
                    "name": container.get("name", "container"),
                    "image": container.get("image") or "",
                    # Required by typed Kubernetes clients. The runtime interface
                    # does not yet expose the resolved image ID.
                    "imageID": "",
                    "ready": True,
                    "restartCount": 0,
                    "state": {"running": {"startedAt": now_rfc3339()}},
                }],
            })
        except Exception as exc:
            pod["status"].update({
                "phase": "Failed",
                "message": str(exc),
                "reason": "RuntimeError",
            })
        await self.state.put("pods", namespace, name, pod)

    async def delete_pod(self, namespace: str, name: str) -> dict[str, Any] | None:
        pod = await self.state.get("pods", namespace, name)
        if not pod:
            return None
        await self.runtime.stop_pod(namespace, name, pod)
        return await self.state.delete("pods", namespace, name)

    async def create_deployment(self, namespace: str, deployment: dict[str, Any]) -> dict[str, Any]:
        deployment = ensure_metadata(deployment, namespace)
        meta = deployment.setdefault("metadata", {})
        spec = deployment.setdefault("spec", {})
        replicas = int(spec.get("replicas", 1))
        deployment["status"] = {
            "replicas": replicas,
            "readyReplicas": 0,
            "availableReplicas": 0,
            "updatedReplicas": replicas,
        }
        await self.state.put("deployments", namespace, meta["name"], deployment)
        owner = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "name": meta["name"],
            "uid": meta["uid"],
            "controller": True,
        }
        template = spec.get("template", {})
        for _ in range(replicas):
            pod = copy.deepcopy(template)
            pod["apiVersion"] = "v1"
            pod["kind"] = "Pod"
            pm = pod.setdefault("metadata", {})
            pm["name"] = f"{meta['name']}-{_suffix()}"
            pm.setdefault("labels", {}).update(spec.get("selector", {}).get("matchLabels", {}))
            await self.create_pod(namespace, pod, owner)
        asyncio.create_task(self._refresh_deployment(namespace, meta["name"]))
        return deployment

    async def _refresh_deployment(self, namespace: str, name: str) -> None:
        await asyncio.sleep(0.1)
        dep = await self.state.get("deployments", namespace, name)
        if not dep:
            return
        uid = dep.get("metadata", {}).get("uid")
        pods = await self.state.list("pods", namespace)
        owned = [p for p in pods if any(r.get("uid") == uid for r in p.get("metadata", {}).get("ownerReferences", []))]
        ready = sum(1 for p in owned if p.get("status", {}).get("phase") == "Running")
        dep.setdefault("status", {}).update({"replicas": len(owned), "readyReplicas": ready, "availableReplicas": ready})
        await self.state.put("deployments", namespace, name, dep)

    async def delete_deployment(self, namespace: str, name: str) -> dict[str, Any] | None:
        dep = await self.state.get("deployments", namespace, name)
        if not dep:
            return None
        uid = dep.get("metadata", {}).get("uid")
        pods = await self.state.list("pods", namespace)
        for p in pods:
            if any(r.get("uid") == uid for r in p.get("metadata", {}).get("ownerReferences", [])):
                await self.delete_pod(namespace, p["metadata"]["name"])
        return await self.state.delete("deployments", namespace, name)
