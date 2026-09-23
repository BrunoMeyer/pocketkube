from __future__ import annotations

import asyncio
import hashlib
import os
import re
from pathlib import Path
from typing import Any

from .base import ExecResult
from .terminal import spawn_terminal


def _safe_name(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
    return (value or "pod")[:48]


class ProotDistroRuntime:
    """Rootless runtime backed by Termux proot-distro.

    One proot-distro container filesystem is created per Kubernetes Pod.
    This is intentionally simple rather than storage-optimal.
    """

    def __init__(self, binary: str = "proot-distro") -> None:
        self.binary = binary
        self._pod_names: dict[tuple[str, str], str] = {}

    def container_name(self, namespace: str, pod_name: str) -> str:
        key = (namespace, pod_name)
        if key not in self._pod_names:
            digest = hashlib.sha1(f"{namespace}/{pod_name}".encode()).hexdigest()[:8]
            self._pod_names[key] = _safe_name(f"pk-{namespace}-{pod_name}-{digest}")
        return self._pod_names[key]

    async def _run(self, *args: str, check: bool = True, capture: bool = True) -> asyncio.subprocess.Process:
        proc = await asyncio.create_subprocess_exec(
            self.binary,
            *args,
            stdout=asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL,
        )
        stdout, stderr = await proc.communicate()
        if check and proc.returncode != 0:
            raise RuntimeError((stderr or stdout or b"proot-distro failed").decode(errors="replace"))
        proc._pk_stdout = stdout  # type: ignore[attr-defined]
        proc._pk_stderr = stderr  # type: ignore[attr-defined]
        return proc

    async def _installed(self, name: str) -> bool:
        proc = await self._run("list", "--quiet", check=False)
        output = getattr(proc, "_pk_stdout", b"").decode(errors="replace")
        return name in {line.strip() for line in output.splitlines()}

    def _first_container(self, pod: dict[str, Any]) -> dict[str, Any]:
        containers = pod.get("spec", {}).get("containers") or []
        if not containers:
            raise RuntimeError("pod has no containers")
        if len(containers) > 1:
            raise RuntimeError("PocketKube 0.1 supports one container per pod")
        return containers[0]

    async def start_pod(self, namespace: str, pod_name: str, pod: dict) -> None:
        c = self._first_container(pod)
        image = c.get("image")
        if not image:
            raise RuntimeError("container image is required")
        name = self.container_name(namespace, pod_name)

        if not await self._installed(name):
            await self._run("install", str(image), "--name", name)

        env_args: list[str] = []
        for env in c.get("env", []) or []:
            if "name" in env and "value" in env:
                env_args += ["--env", f"{env['name']}={env['value']}"]

        command = list(c.get("command") or []) + list(c.get("args") or [])
        if c.get("command"):
            args = ["login", "--detach", *env_args, name, "--", *command]
        else:
            # proot-distro run uses the image ENTRYPOINT/CMD. If only args are
            # supplied Kubernetes-style, they replace the image CMD.
            args = ["run", "--detach", *env_args, name]
            if c.get("args"):
                args += ["--", *map(str, c.get("args") or [])]
        await self._run(*map(str, args))

    async def stop_pod(self, namespace: str, pod_name: str, pod: dict) -> None:
        name = self.container_name(namespace, pod_name)
        await self._run("kill", name, check=False)
        # Keep the extracted rootfs for quick restart; explicit cleanup can be
        # added later without affecting Kubernetes semantics.

    async def exec(self, namespace: str, pod_name: str, pod: dict, command: list[str]) -> ExecResult:
        name = self.container_name(namespace, pod_name)
        proc = await asyncio.create_subprocess_exec(
            self.binary, "login", name, "--", *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return ExecResult(proc.returncode or 0, stdout, stderr)

    async def exec_stream(self, namespace: str, pod_name: str, pod: dict, command: list[str], tty: bool = False):
        name = self.container_name(namespace, pod_name)
        if tty:
            return await spawn_terminal([self.binary, "login", name, "--", *command])
        return await asyncio.create_subprocess_exec(
            self.binary, "login", name, "--", *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
