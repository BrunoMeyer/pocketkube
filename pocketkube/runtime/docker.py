from __future__ import annotations

import asyncio
import hashlib
import re
from typing import Any

from .base import ExecResult


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

    async def exec_stream(self, namespace: str, pod_name: str, pod: dict, command: list[str]):
        return await asyncio.create_subprocess_exec(
            self.binary, "exec", "-i", _name(namespace, pod_name), *command,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
