from __future__ import annotations

import asyncio
import json
import os
import platform

import anyio
from urllib.parse import parse_qs

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from .controllers import Controllers
from .models import ensure_metadata, status_object
from .runtime.docker import DockerRuntime
from .runtime.logs import LogOptions, limit_stream
from .runtime.raw_proot import RawProotRuntime
from .state import MemoryState
from .nodes import local_node, select_fields, node_table, patch_node_labels, NodePatchConflict
from .metrics import HostMetrics, METRICS_GROUP
from .portforward import PortForwardSession, PROTOCOLS


class LogResponse(StreamingResponse):
    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Close even when an ASGI send fails while the iterator is suspended
            # at yield. Shield cleanup from Starlette's disconnect cancel scope.
            with anyio.CancelScope(shield=True):
                await self.body_iterator.aclose()


def api_resource_list(group_version: str, resources: list[dict]) -> dict:
    return {"kind": "APIResourceList", "apiVersion": "v1", "groupVersion": group_version, "resources": resources}


CORE_RESOURCES = [
    {"name": "nodes", "singularName": "node", "namespaced": False, "kind": "Node", "shortNames": ["no"], "verbs": ["get", "list", "patch"]},
    {"name": "events", "singularName": "event", "namespaced": True, "kind": "Event", "verbs": ["list"]},
    {"name": "pods", "singularName": "", "namespaced": True, "kind": "Pod", "verbs": ["create", "delete", "get", "list"]},
    {"name": "pods/log", "singularName": "", "namespaced": True, "kind": "Pod", "verbs": ["get"]},
    {"name": "pods/portforward", "singularName": "", "namespaced": True, "kind": "PodPortForwardOptions", "verbs": ["get", "create"]},
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
    node = local_node(runtime_name)
    metrics = HostMetrics()
    ctl = Controllers(state, runtime, node["metadata"]["name"])

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
            }, METRICS_GROUP],
        })

    async def core_discovery(request: Request):
        return JSONResponse(api_resource_list("v1", CORE_RESOURCES))

    async def apps_discovery(request: Request):
        return JSONResponse(api_resource_list("apps/v1", APPS_RESOURCES))

    async def metrics_group(request: Request):
        return JSONResponse({"apiVersion": "v1", "kind": "APIGroup", **METRICS_GROUP})

    async def metrics_discovery(request: Request):
        return JSONResponse(api_resource_list("metrics.k8s.io/" + request.path_params["metrics_version"], [
            {"name": "nodes", "singularName": "node", "namespaced": False,
             "kind": "NodeMetrics", "verbs": ["get", "list"]},
        ]))

    async def node_metrics(request: Request):
        name = request.path_params.get("name")
        if name is not None and name != node["metadata"]["name"]:
            return JSONResponse(status_object("node not found", 404, "NotFound"), status_code=404)
        api_version = "metrics.k8s.io/" + request.path_params["metrics_version"]
        try:
            if request.query_params.get("watch", "false").lower() in ("true", "1"):
                raise ValueError("metrics watches are not supported")
            if request.query_params.get("labelSelector"):
                raise ValueError("node label selectors are not supported")
            selected = select_fields([node], request.query_params.get("fieldSelector", ""), {"metadata.name"})
        except ValueError as exc:
            return JSONResponse(status_object(str(exc), 400, "BadRequest"), status_code=400)
        response_headers = {}
        annotations = {}
        if metrics.scope == "visible-processes":
            annotations["pocketkube.io/metrics-scope"] = "visible-processes"
            response_headers["Warning"] = ('299 pocketkube "CPU and memory cover readable same-user processes, '
                                           'not the whole device; memory is summed RSS"')
        items = []
        if selected:
            try:
                sample = await metrics.sample()
            except RuntimeError as exc:
                return JSONResponse(status_object(str(exc), 503, "ServiceUnavailable"), status_code=503)
            items.append({"apiVersion": api_version, "kind": "NodeMetrics",
                          "metadata": {"name": node["metadata"]["name"], "labels": node["metadata"]["labels"], "annotations": annotations},
                          **sample})
        if name is not None:
            return JSONResponse(items[0] if items else status_object("node not found", 404, "NotFound"),
                                status_code=200 if items else 404, headers=response_headers)
        return JSONResponse(_list("NodeMetrics", api_version, items), headers=response_headers)

    async def nodes(request: Request):
        if request.query_params.get("watch", "false").lower() in ("true", "1"):
            return JSONResponse(status_object("node watches are not supported", 400, "BadRequest"), status_code=400)
        try:
            items = select_fields([node], request.query_params.get("fieldSelector", ""), {"metadata.name"})
            if request.query_params.get("labelSelector"):
                raise ValueError("node label selectors are not supported")
        except ValueError as exc:
            return JSONResponse(status_object(str(exc), 400, "BadRequest"), status_code=400)
        if "as=Table" in request.headers.get("accept", ""):
            return JSONResponse(node_table(items))
        return JSONResponse(_list("Node", "v1", items))

    async def node_item(request: Request):
        nonlocal node
        if request.path_params["name"] != node["metadata"]["name"]:
            return JSONResponse(status_object("node not found", 404, "NotFound"), status_code=404)
        if request.method == "PATCH":
            content_type = request.headers.get("content-type", "").split(";", 1)[0].strip()
            if content_type not in ("application/merge-patch+json", "application/strategic-merge-patch+json"):
                return JSONResponse(status_object("use JSON merge patch or strategic merge patch", 415,
                                                  "UnsupportedMediaType"), status_code=415)
            try:
                dry_run = request.query_params.get("dryRun")
                if dry_run not in (None, "All"):
                    raise ValueError("dryRun must be All")
                patch = await request.json()
                # No await between validation and replacement: concurrent writes
                # cannot interleave on the API event loop.
                updated = patch_node_labels(node, patch)
            except NodePatchConflict as exc:
                return JSONResponse(status_object(str(exc), 409, "Conflict"), status_code=409)
            except (ValueError, UnicodeError) as exc:
                return JSONResponse(status_object(str(exc), 422, "Invalid"), status_code=422)
            if dry_run != "All":
                node = updated
            return JSONResponse(updated)
        if "as=Table" in request.headers.get("accept", ""):
            return JSONResponse(node_table([node]))
        return JSONResponse(node)

    async def events(request: Request):
        # PocketKube does not record events yet. kubectl describe requests them.
        return JSONResponse(_list("Event", "v1", []))

    def pod_list(request, items):
        try:
            items = select_fields(items, request.query_params.get("fieldSelector", ""),
                                  {"metadata.name", "metadata.namespace", "spec.nodeName", "status.phase"})
        except ValueError as exc:
            return JSONResponse(status_object(str(exc), 400, "BadRequest"), status_code=400)
        return JSONResponse(_list("Pod", "v1", items))

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
            return pod_list(request, await state.list("pods", ns))
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
        return pod_list(request, await state.list("pods", None))

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

    async def pod_logs(request: Request):
        ns, name = request.path_params["namespace"], request.path_params["name"]
        pod = await state.get("pods", ns, name)
        if not pod:
            return JSONResponse(status_object("pod not found", 404, "NotFound"), status_code=404)
        try:
            options = LogOptions.parse(request.query_params)
            containers = pod.get("spec", {}).get("containers") or []
            container = request.query_params.get("container")
            if container and not any(c.get("name") == container for c in containers):
                raise ValueError("container not found: " + container)
            stream = await runtime.logs(ns, name, pod, options)
        except (ValueError, RuntimeError, OSError, OverflowError) as exc:
            return JSONResponse(status_object(str(exc), 400, "BadRequest"), status_code=400)
        return LogResponse(limit_stream(stream, options.limit), media_type="text/plain",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    async def portforward_http(request: Request):
        return JSONResponse(status_object(
            "port-forward requires kubectl WebSocket tunneling; set KUBECTL_PORT_FORWARD_WEBSOCKETS=true. "
            "Legacy HTTP SPDY upgrades are not supported.", 426, "UpgradeRequired"), status_code=426)

    async def portforward_ws(ws: WebSocket):
        offered = [p.strip() for p in ws.headers.get("sec-websocket-protocol", "").split(",")]
        selected = next((p for p in PROTOCOLS if p in offered), None)
        ns, name = ws.path_params["namespace"], ws.path_params["name"]
        pod = await state.get("pods", ns, name)
        async def reject(message, code):
            if "websocket.http.response" in ws.scope.get("extensions", {}):
                await ws.send_denial_response(JSONResponse(status_object(message, code, "Failure"), status_code=code))
            else:
                await ws.close(code=1008, reason=message)
        if pod is None:
            await reject("pod not found", 404)
            return
        if pod.get("status", {}).get("phase") != "Running":
            await reject("pod is not running", 400)
            return
        if selected is None:
            await reject("use kubectl WebSocket SPDY portforward tunneling", 400)
            return
        await ws.accept(subprotocol=selected)
        async def connect(port):
            return await runtime.open_port(ns, name, pod, port)
        session = PortForwardSession(ws, connect)
        async def monitor():
            while True:
                await asyncio.sleep(.5)
                current = await state.get("pods", ns, name)
                if current is None or current.get("metadata", {}).get("uid") != pod.get("metadata", {}).get("uid"):
                    return
                if current.get("status", {}).get("phase") != "Running":
                    return
        tasks = [asyncio.create_task(session.run()), asyncio.create_task(monitor())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (WebSocketDisconnect, OSError):
            pass
        finally:
            for task in tasks:
                task.cancel()
            with anyio.CancelScope(shield=True):
                await asyncio.gather(*tasks, return_exceptions=True)
                try:
                    await ws.close()
                except (WebSocketDisconnect, RuntimeError, OSError):
                    pass

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

        proc = None
        tasks = []
        disconnected = False
        send_lock = asyncio.Lock()

        async def send(data):
            async with send_lock:
                await ws.send_bytes(data)

        async def pump(reader, channel):
            if reader is None:
                return
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    return
                # Even disabled streams must be drained to avoid a full pipe.
                if channel is not None:
                    await send(bytes([channel]) + chunk)

        async def close_stdin():
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.close()
                if tty_enabled:
                    await proc.stdin.drain()

        async def read_client():
            nonlocal disconnected
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    disconnected = True
                    return
                data = msg.get("bytes")
                if data is None:
                    text = msg.get("text")
                    data = text.encode() if text is not None else None
                if not data:
                    continue
                channel, payload = data[0], data[1:]
                if channel == 0 and stdin_enabled and proc.stdin and not proc.stdin.is_closing():
                    proc.stdin.write(payload)
                    await proc.stdin.drain()
                elif channel == 255 and selected == "v5.channel.k8s.io" and payload == b"\x00":
                    await close_stdin()
                elif channel == 4 and tty_enabled:
                    size = json.loads(payload)
                    proc.resize(int(size["Width"]), int(size["Height"]))

        try:
            proc = await runtime.exec_stream(ns, name, pod, command, tty=tty_enabled)
            if not stdin_enabled:
                await close_stdin()
            stdout_task = asyncio.create_task(pump(proc.stdout, 1 if stdout_enabled else None))
            stderr_task = asyncio.create_task(pump(proc.stderr, 2 if stderr_enabled and not tty_enabled else None))
            stdin_task = asyncio.create_task(read_client())
            wait_task = asyncio.create_task(proc.wait())
            tasks = [stdout_task, stderr_task, stdin_task, wait_task]
            # A disconnect must stop exec even if the command never reads stdin.
            watched = set(tasks)
            while not wait_task.done():
                done, _ = await asyncio.wait(watched, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
                if stdin_task in done:
                    return
                watched.difference_update(done)
            rc = wait_task.result()
            await asyncio.wait_for(asyncio.gather(stdout_task, stderr_task), timeout=3)
            if rc != 0:
                result = {
                    "kind": "Status", "apiVersion": "v1", "metadata": {}, "status": "Failure",
                    "message": f"command terminated with exit code {rc}", "reason": "NonZeroExitCode",
                    "details": {"causes": [{"reason": "ExitCode", "message": str(rc)}]}, "code": 500,
                }
            else:
                result = status_object("")
            await send(b"\x03" + json.dumps(result).encode())
        except WebSocketDisconnect:
            disconnected = True
        except Exception as exc:
            if not disconnected:
                try:
                    await send(b"\x03" + json.dumps(status_object(str(exc), 500, "InternalError")).encode())
                except (WebSocketDisconnect, RuntimeError, OSError):
                    disconnected = True
        finally:
            # Disconnect cancellation must not interrupt process cleanup.
            with anyio.CancelScope(shield=True):
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if proc is not None:
                    try:
                        if proc.returncode is None:
                            proc.terminate()
                            try:
                                await asyncio.wait_for(proc.wait(), timeout=1)
                            except asyncio.TimeoutError:
                                proc.kill()
                                await asyncio.wait_for(proc.wait(), timeout=1)
                    except (ProcessLookupError, asyncio.TimeoutError):
                        pass
                    finally:
                        if tty_enabled:
                            proc.close()
                        elif proc.stdin:
                            proc.stdin.close()
            if not disconnected:
                try:
                    await ws.close()
                except (WebSocketDisconnect, RuntimeError, OSError):
                    pass

    routes = [
        Route("/api/v1/nodes", nodes, methods=["GET"]),
        Route("/api/v1/nodes/{name}", node_item, methods=["GET", "PATCH"]),
        Route("/api/v1/events", events, methods=["GET"]),
        Route("/api/v1/namespaces/{namespace}/events", events, methods=["GET"]),
        Route("/", root), Route("/version", version), Route("/api", api), Route("/apis", apis),
        Route("/api/v1", core_discovery), Route("/apis/apps/v1", apps_discovery),
        Route("/api/v1/namespaces", namespaces, methods=["GET", "POST"]),
        Route("/api/v1/namespaces/{name}", namespace_item, methods=["GET", "DELETE"]),
        Route("/api/v1/pods", all_pods, methods=["GET"]),
        Route("/api/v1/namespaces/{namespace}/pods", pods, methods=["GET", "POST"]),
        Route("/api/v1/namespaces/{namespace}/pods/{name}/log", pod_logs, methods=["GET"]),
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
        Route("/api/v1/namespaces/{namespace}/pods/{name}/portforward", portforward_http, methods=["GET", "POST"]),
        WebSocketRoute("/api/v1/namespaces/{namespace}/pods/{name}/portforward", portforward_ws),
    ]
    routes.append(Route("/apis/metrics.k8s.io", metrics_group, methods=["GET"]))
    for metrics_version in ("v1", "v1beta1"):
        # Use fixed version paths so unsupported versions remain ordinary 404s.
        async def discovery(request, version=metrics_version):
            request.path_params["metrics_version"] = version
            return await metrics_discovery(request)
        async def usage(request, version=metrics_version):
            request.path_params["metrics_version"] = version
            return await node_metrics(request)
        prefix = "/apis/metrics.k8s.io/" + metrics_version
        routes.extend([Route(prefix, discovery), Route(prefix + "/nodes", usage),
                       Route(prefix + "/nodes/{name}", usage)])
    app = Starlette(debug=False, routes=routes)
    app.state.pk_metrics = metrics
    app.state.pk_state = state
    app.state.pk_runtime = runtime
    return app
