from __future__ import annotations

import asyncio
import hashlib
import json
import ipaddress
import re
from typing import Any

from .base import ExecResult
from .logs import timestamp
from .terminal import spawn_terminal


def _name(ns: str, pod: str) -> str:
    base = re.sub(r"[^A-Za-z0-9_.-]+", "-", f"pk-{ns}-{pod}")[:48]
    return f"{base}-{hashlib.sha1(f'{ns}/{pod}'.encode()).hexdigest()[:8]}"


class DockerRuntime:
    """Testing runtime for desktop Linux/macOS; not intended for Android."""

    def __init__(self, binary: str = "docker") -> None:
        self.binary = binary

    def _first(self, pod: dict[str, Any]) -> dict[str, Any]:
        containers = pod.get("spec", {}).get("containers") or []
        if len(containers) != 1:
            raise RuntimeError("PocketKube 0.1 supports exactly one container per pod")
        return containers[0]

    async def _call(self, *args: str, check: bool = True):
        p = await asyncio.create_subprocess_exec(self.binary, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await p.communicate()
        if check and p.returncode:
            raise RuntimeError((err or out).decode(errors="replace"))
        return p.returncode or 0, out, err

    async def start_pod(self, namespace: str, pod_name: str, pod: dict) -> None:
        c = self._first(pod)
        args = ["run", "-d", "--name", _name(namespace, pod_name)]
        for env in c.get("env", []) or []:
            if "name" in env and "value" in env:
                args += ["-e", f"{env['name']}={env['value']}"]
        args.append(c["image"])
        args += [*map(str, c.get("command") or []), *map(str, c.get("args") or [])]
        await self._call(*args)

    async def stop_pod(self, namespace: str, pod_name: str, pod: dict) -> None:
        await self._call("rm", "-f", _name(namespace, pod_name), check=False)

    async def exec(self, namespace: str, pod_name: str, pod: dict, command: list[str]) -> ExecResult:
        rc, out, err = await self._call("exec", _name(namespace, pod_name), *command, check=False)
        return ExecResult(rc, out, err)

    async def exec_stream(self, namespace: str, pod_name: str, pod: dict, command: list[str], tty: bool = False):
        if tty:
            return await spawn_terminal([
                self.binary, "exec", "-it", _name(namespace, pod_name), *command,
            ])
        return await asyncio.create_subprocess_exec(
            self.binary, "exec", "-i", _name(namespace, pod_name), *command,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )

    async def logs(self, namespace, pod_name, pod, options):
        name = _name(namespace, pod_name)
        # Fail before HTTP headers are sent when the container is unavailable.
        await self._call("inspect", "--format", "{{.Id}}", name)
        args = [self.binary, "logs", "--tail", str(options.tail) if options.tail >= 0 else "all"]
        if options.follow:
            args.append("--follow")
        if options.timestamps:
            args.append("--timestamps")
        if options.since is not None:
            args += ["--since", timestamp(options.since)]
        args.append(name)

        async def stream():
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            try:
                while True:
                    data = await proc.stdout.read(8192)
                    if not data:
                        break
                    yield data
                await proc.wait()
            finally:
                if proc.returncode is None:
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), 1)
                    except asyncio.TimeoutError:
                        proc.kill()
                        await proc.wait()
        return stream()

    async def open_port(self, namespace, pod_name, pod, port):
        _, output, _ = await self._call("inspect", _name(namespace, pod_name))
        info = json.loads(output)[0]
        if not info.get("State", {}).get("Running"):
            raise RuntimeError("pod container is not running")
        if info.get("HostConfig", {}).get("NetworkMode") == "host":
            address = "127.0.0.1"
        else:
            networks = info.get("NetworkSettings", {}).get("Networks", {}).values()
            address = next((network.get("IPAddress") or network.get("GlobalIPv6Address")
                            for network in networks if network.get("IPAddress") or network.get("GlobalIPv6Address")), None)
            if address is None:
                raise RuntimeError("Docker container has no reachable network address")
        return await asyncio.open_connection(str(ipaddress.ip_address(address)), port)
