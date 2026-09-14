from __future__ import annotations

import asyncio
import json
import os
import platform
from urllib.parse import parse_qs

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from .controllers import Controllers
from .models import ensure_metadata, status_object
from .runtime.docker import DockerRuntime
from .runtime.raw_proot import RawProotRuntime
from .state import MemoryState


def api_resource_list(group_version: str, resources: list[dict]) -> dict:
    return {"kind": "APIResourceList", "apiVersion": "v1", "groupVersion": group_version, "resources": resources}


CORE_RESOURCES = [
    {"name": "pods", "singularName": "", "namespaced": True, "kind": "Pod", "verbs": ["create", "delete", "get", "list"]},
    {"name": "pods/exec", "singularName": "", "namespaced": True, "kind": "PodExecOptions", "verbs": ["create", "get"]},
    {"name": "namespaces", "singularName": "", "namespaced": False, "kind": "Namespace", "verbs": ["create", "delete", "get", "list"]},
]
APPS_RESOURCES = [
    {"name": "deployments", "singularName": "", "namespaced": True, "kind": "Deployment", "verbs": ["create", "delete", "get", "list"]},
]


def _list(kind: str, api_version: str, items: list[dict]) -> dict:
    return {"kind": f"{kind}List", "apiVersion": api_version, "metadata": {"resourceVersion": "1"}, "items": items}


def create_app(runtime_name: str | None = None) -> Starlette:
    runtime_name = runtime_name or os.environ.get("POCKETKUBE_RUNTIME", "proot")
    if runtime_name == "docker":
        runtime = DockerRuntime()
    elif runtime_name == "proot":
        runtime = RawProotRuntime()
    else:
        raise RuntimeError(f"unknown PocketKube runtime: {runtime_name}")
    state = MemoryState()
    ctl = Controllers(state, runtime)

    async def version(request: Request):
        machine = platform.machine().lower()
        arch = "arm" if machine.startswith("armv7") or machine.startswith("armv6") else ("arm64" if machine in {"aarch64", "arm64"} else machine)
        return JSONResponse({"major": "1", "minor": "31", "gitVersion": "v1.31.0-pocketkube", "platform": f"linux/{arch}"})

    async def root(request: Request):
        return JSONResponse({"paths": ["/api", "/api/v1", "/apis", "/apis/apps/v1", "/version"]})

    async def api(request: Request):
        return JSONResponse({"kind": "APIVersions", "apiVersion": "v1", "versions": ["v1"], "serverAddressByClientCIDRs": []})

    async def apis(request: Request):
        return JSONResponse({
            "kind": "APIGroupList", "apiVersion": "v1", "groups": [{
                "name": "apps",
                "versions": [{"groupVersion": "apps/v1", "version": "v1"}],
                "preferredVersion": {"groupVersion": "apps/v1", "version": "v1"},
            }],
        })

    async def core_discovery(request: Request):
        return JSONResponse(api_resource_list("v1", CORE_RESOURCES))

    async def apps_discovery(request: Request):
        return JSONResponse(api_resource_list("apps/v1", APPS_RESOURCES))

    async def namespaces(request: Request):
        if request.method == "GET":
            items = await state.list("namespaces", "")
            if not items:
                items = [ensure_metadata({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "default"}})]
            return JSONResponse(_list("Namespace", "v1", items))
        body = await request.json()
        obj = ensure_metadata(body)
        name = obj.get("metadata", {}).get("name")
        if not name:
            return JSONResponse(status_object("metadata.name is required", 422, "Invalid"), status_code=422)
        await state.put("namespaces", "", name, obj)
        return JSONResponse(obj, status_code=201)

    async def namespace_item(request: Request):
        name = request.path_params["name"]
        if name == "default":
            default = ensure_metadata({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "default"}})
            if request.method == "GET": return JSONResponse(default)
            return JSONResponse(status_object("deleted default namespace"))
        if request.method == "GET":
            obj = await state.get("namespaces", "", name)
            if not obj: return JSONResponse(status_object("namespace not found", 404, "NotFound"), status_code=404)
            return JSONResponse(obj)
        obj = await state.delete("namespaces", "", name)
        if not obj: return JSONResponse(status_object("namespace not found", 404, "NotFound"), status_code=404)
        return JSONResponse(status_object(f"namespace {name} deleted"))

    async def pods(request: Request):
        ns = request.path_params["namespace"]
        if request.method == "GET":
            return JSONResponse(_list("Pod", "v1", await state.list("pods", ns)))
        body = await request.json()
        if body.get("kind") != "Pod":
            return JSONResponse(status_object("kind must be Pod", 422, "Invalid"), status_code=422)
        try:
            pod = await ctl.create_pod(ns, body)
        except Exception as exc:
            return JSONResponse(status_object(str(exc), 422, "Invalid"), status_code=422)
        return JSONResponse(pod, status_code=201)

    async def pod_item(request: Request):
        ns, name = request.path_params["namespace"], request.path_params["name"]
        if request.method == "GET":
            pod = await state.get("pods", ns, name)
            if not pod: return JSONResponse(status_object("pod not found", 404, "NotFound"), status_code=404)
            return JSONResponse(pod)
        pod = await ctl.delete_pod(ns, name)
        if not pod: return JSONResponse(status_object("pod not found", 404, "NotFound"), status_code=404)
        return JSONResponse(status_object(f"pod {name} deleted"))

    async def all_pods(request: Request):
        return JSONResponse(_list("Pod", "v1", await state.list("pods", None)))

    async def deployments(request: Request):
        ns = request.path_params["namespace"]
        if request.method == "GET":
            return JSONResponse(_list("Deployment", "apps/v1", await state.list("deployments", ns)))
        body = await request.json()
        if body.get("kind") != "Deployment":
            return JSONResponse(status_object("kind must be Deployment", 422, "Invalid"), status_code=422)
        dep = await ctl.create_deployment(ns, body)
        return JSONResponse(dep, status_code=201)

    async def deployment_item(request: Request):
        ns, name = request.path_params["namespace"], request.path_params["name"]
        if request.method == "GET":
            obj = await state.get("deployments", ns, name)
            if not obj: return JSONResponse(status_object("deployment not found", 404, "NotFound"), status_code=404)
            return JSONResponse(obj)
        obj = await ctl.delete_deployment(ns, name)
        if not obj: return JSONResponse(status_object("deployment not found", 404, "NotFound"), status_code=404)
        return JSONResponse(status_object(f"deployment {name} deleted"))

    async def all_deployments(request: Request):
        return JSONResponse(_list("Deployment", "apps/v1", await state.list("deployments", None)))

    async def exec_http_unsupported(request: Request):
        # Older kubectl clients use SPDY with an HTTP POST to this endpoint.
        # PocketKube intentionally implements only the lighter WebSocket remote-command
        # transport. Returning 426 makes the incompatibility explicit instead of a 404.
        return JSONResponse(
            status_object(
                "PocketKube exec requires the Kubernetes WebSocket remote-command transport. "
                "Use kubectl >= 1.29 and set KUBECTL_REMOTE_COMMAND_WEBSOCKETS=true "
                "when your kubectl does not enable it by default.",
                426,
                "UpgradeRequired",
            ),
            status_code=426,
        )

    async def exec_ws(ws: WebSocket):
        ns, name = ws.path_params["namespace"], ws.path_params["name"]
        offered = ws.headers.get("sec-websocket-protocol", "")
        protocols = [p.strip() for p in offered.split(",") if p.strip()]
        selected = "v5.channel.k8s.io" if "v5.channel.k8s.io" in protocols else ("v4.channel.k8s.io" if "v4.channel.k8s.io" in protocols else None)
        await ws.accept(subprotocol=selected)
        pod = await state.get("pods", ns, name)
        if not pod:
            await ws.send_bytes(b"\x03" + json.dumps(status_object("pod not found", 404, "NotFound")).encode())
            await ws.close(code=1008)
            return

        params = ws.query_params
        command = params.getlist("command")
        if not command:
            await ws.send_bytes(b"\x03" + json.dumps(status_object("command is required", 422, "Invalid")).encode())
            await ws.close(code=1008)
            return
        def qbool(key: str, default: bool = False) -> bool:
            value = params.get(key)
            if value is None:
                return default
            return value.lower() in {"1", "true", "yes", "on"}

        stdin_enabled = qbool("stdin")
        stdout_enabled = qbool("stdout", True)
        stderr_enabled = qbool("stderr")
        tty_enabled = qbool("tty")

        proc = await runtime.exec_stream(ns, name, pod, command)

        async def pump(reader, channel: int):
            if reader is None: return
            while True:
                chunk = await reader.read(4096)
                if not chunk: break
                await ws.send_bytes(bytes([channel]) + chunk)

        stdout_task = (
            asyncio.create_task(pump(proc.stdout, 1))
            if stdout_enabled
            else asyncio.create_task(asyncio.sleep(0))
        )

        # Kubernetes does not create a separate stderr stream when tty=true.
        # PocketKube does not emulate a real PTY yet, but merging stderr into
        # stdout preserves the remote-command channel contract and avoids
        # kubectl discarding channel 2 as an unknown stream.
        if tty_enabled:
            stderr_task = asyncio.create_task(pump(proc.stderr, 1))
        elif stderr_enabled:
            stderr_task = asyncio.create_task(pump(proc.stderr, 2))
        else:
            stderr_task = asyncio.create_task(asyncio.sleep(0))

        async def read_client():
            try:
                while True:
                    msg = await ws.receive()
                    data = msg.get("bytes")
                    if data is None:
                        text = msg.get("text")
                        data = text.encode() if text is not None else None
                    if not data:
                        continue
                    channel, payload = data[0], data[1:]
                    if channel == 0 and stdin_enabled and proc.stdin:
                        proc.stdin.write(payload)
                        await proc.stdin.drain()
                    elif channel == 255 and proc.stdin:
                        proc.stdin.close()
                    # resize channel 4 is intentionally ignored in 0.1
            except (WebSocketDisconnect, RuntimeError):
                if proc.stdin and not proc.stdin.is_closing(): proc.stdin.close()

        stdin_task = asyncio.create_task(read_client())
        rc = await proc.wait()
        await stdout_task
        await stderr_task
        if rc != 0:
            err = {
                "kind": "Status", "apiVersion": "v1", "metadata": {}, "status": "Failure",
                "message": f"command terminated with exit code {rc}", "reason": "NonZeroExitCode",
                "details": {"causes": [{"reason": "ExitCode", "message": str(rc)}]}, "code": 500,
            }
            await ws.send_bytes(b"\x03" + json.dumps(err).encode())
        stdin_task.cancel()
        try: await stdin_task
        except BaseException: pass
        await ws.close()

    routes = [
        Route("/", root), Route("/version", version), Route("/api", api), Route("/apis", apis),
        Route("/api/v1", core_discovery), Route("/apis/apps/v1", apps_discovery),
        Route("/api/v1/namespaces", namespaces, methods=["GET", "POST"]),
        Route("/api/v1/namespaces/{name}", namespace_item, methods=["GET", "DELETE"]),
        Route("/api/v1/pods", all_pods, methods=["GET"]),
        Route("/api/v1/namespaces/{namespace}/pods", pods, methods=["GET", "POST"]),
        Route("/api/v1/namespaces/{namespace}/pods/{name}", pod_item, methods=["GET", "DELETE"]),
        Route("/apis/apps/v1/deployments", all_deployments, methods=["GET"]),
        Route("/apis/apps/v1/namespaces/{namespace}/deployments", deployments, methods=["GET", "POST"]),
        Route("/apis/apps/v1/namespaces/{namespace}/deployments/{name}", deployment_item, methods=["GET", "DELETE"]),
        Route(
            "/api/v1/namespaces/{namespace}/pods/{name}/exec",
            exec_http_unsupported,
            methods=["POST"],
        ),
        WebSocketRoute("/api/v1/namespaces/{namespace}/pods/{name}/exec", exec_ws),
    ]
    app = Starlette(debug=False, routes=routes)
    app.state.pk_state = state
    app.state.pk_runtime = runtime
    return app
