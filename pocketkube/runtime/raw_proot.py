from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from .base import ExecResult


class RawProotRuntime:
    """Minimal rootless runtime backed directly by PRoot.

    This backend is designed for old/limited Termux installations where
    ``proot`` is available but ``proot-distro`` is not.

    Current intentionally-small scope:
      * exactly one container per Pod
      * Alpine images only
      * a pre-extracted Alpine rootfs, shared by Pods
      * no OCI image puller yet

    The rootfs defaults to ``~/rootfs/alpine`` and can be overridden with
    ``POCKETKUBE_PROOT_ROOTFS`` or the CLI ``--rootfs`` option.
    """

    def __init__(self, binary: str = "proot", rootfs: str | Path | None = None) -> None:
        self.binary = binary
        configured = rootfs or os.environ.get("POCKETKUBE_PROOT_ROOTFS") or (Path.home() / "rootfs" / "alpine")
        self.rootfs = Path(configured).expanduser()
        self._processes: dict[tuple[str, str], asyncio.subprocess.Process] = {}

    def _first_container(self, pod: dict[str, Any]) -> dict[str, Any]:
        containers = pod.get("spec", {}).get("containers") or []
        if not containers:
            raise RuntimeError("pod has no containers")
        if len(containers) != 1:
            raise RuntimeError("PocketKube currently supports exactly one container per pod")
        return containers[0]

    def _rootfs_for_image(self, image: str) -> Path:
        # Until the OCI puller exists, all Alpine tags use the configured
        # pre-extracted Alpine rootfs. Reject other images instead of silently
        # pretending they are supported.
        if image != "alpine" and not image.startswith("alpine:"):
            raise RuntimeError(
                f"image {image!r} is not supported by the raw PRoot backend yet; "
                "currently only alpine and alpine:* are supported"
            )

        rootfs = self.rootfs
        if not rootfs.is_dir():
            raise RuntimeError(
                f"Alpine rootfs not found at {rootfs}. "
                "Run scripts/setup-alpine-rootfs.sh or pass --rootfs PATH."
            )

        shell = rootfs / "bin" / "sh"
        if not shell.exists() and not shell.is_symlink():
            raise RuntimeError(f"invalid Alpine rootfs: {shell} is missing")

        return rootfs

    def _host_environment(self) -> dict[str, str]:
        env = os.environ.copy()

        # Legacy Android 5/6 Termux executables depend on $PREFIX/lib through
        # LD_LIBRARY_PATH. PRoot itself must see this host-side value in order
        # to locate libraries such as libtalloc and libandroid-support.
        prefix = env.get("PREFIX")
        if prefix:
            env["LD_LIBRARY_PATH"] = f"{prefix}/lib"

        # A Termux preload library must not leak into guest programs.
        env.pop("LD_PRELOAD", None)
        return env

    @staticmethod
    def _container_environment(container: dict[str, Any]) -> list[str]:
        result: list[str] = []
        for item in container.get("env", []) or []:
            name = item.get("name")
            if not name or "value" not in item:
                continue
            result.append(f"{name}={item['value']}")
        return result

    def _proot_command(
        self,
        rootfs: Path,
        command: list[str],
        container_env: list[str] | None = None,
    ) -> list[str]:
        if not command:
            raise RuntimeError("container command is empty")

        # LD_LIBRARY_PATH is required while Android starts the *host* PRoot
        # executable, but must be removed before executing guest binaries.
        # Passing the actual command through "$@" avoids shell quoting bugs.
        wrapper = (
            "unset LD_LIBRARY_PATH LD_PRELOAD; "
            "export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin; "
            "export HOME=/root USER=root LOGNAME=root; "
            "exec \"$@\""
        )

        guest_command = [str(x) for x in command]
        if container_env:
            guest_command = ["/usr/bin/env", *container_env, *guest_command]

        return [
            self.binary,
            "--link2symlink",
            "-0",
            "-r",
            str(rootfs),
            "-w",
            "/",
            "/bin/sh",
            "-c",
            wrapper,
            "pocketkube",
            *guest_command,
        ]

    async def start_pod(self, namespace: str, pod_name: str, pod: dict) -> None:
        container = self._first_container(pod)
        image = container.get("image")
        if not image:
            raise RuntimeError("container image is required")

        rootfs = self._rootfs_for_image(str(image))
        command = [
            *map(str, container.get("command") or []),
            *map(str, container.get("args") or []),
        ]
        if not command:
            raise RuntimeError(
                "raw PRoot currently requires spec.containers[].command; "
                "OCI ENTRYPOINT/CMD discovery is not implemented yet"
            )

        key = (namespace, pod_name)
        previous = self._processes.get(key)
        if previous is not None and previous.returncode is None:
            raise RuntimeError(f"pod {namespace}/{pod_name} is already running")

        cmd = self._proot_command(
            rootfs,
            command,
            self._container_environment(container),
        )
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            env=self._host_environment(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

        # Most setup/ABI errors happen immediately. Give PRoot a short chance
        # to report them before marking the Pod Running.
        try:
            await asyncio.wait_for(proc.wait(), timeout=0.25)
        except asyncio.TimeoutError:
            self._processes[key] = proc
            return

        stderr = await proc.stderr.read() if proc.stderr else b""
        raise RuntimeError(
            stderr.decode(errors="replace").strip()
            or f"proot exited immediately with status {proc.returncode}"
        )

    async def stop_pod(self, namespace: str, pod_name: str, pod: dict) -> None:
        proc = self._processes.pop((namespace, pod_name), None)
        if proc is None or proc.returncode is not None:
            return

        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()

    async def exec(self, namespace: str, pod_name: str, pod: dict, command: list[str]) -> ExecResult:
        container = self._first_container(pod)
        image = container.get("image")
        if not image:
            raise RuntimeError("container image is required")

        rootfs = self._rootfs_for_image(str(image))
        cmd = self._proot_command(
            rootfs,
            command,
            self._container_environment(container),
        )
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            env=self._host_environment(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return ExecResult(proc.returncode or 0, stdout, stderr)

    async def exec_stream(self, namespace: str, pod_name: str, pod: dict, command: list[str]):
        container = self._first_container(pod)
        image = container.get("image")
        if not image:
            raise RuntimeError("container image is required")

        rootfs = self._rootfs_for_image(str(image))
        cmd = self._proot_command(
            rootfs,
            command,
            self._container_environment(container),
        )
        return await asyncio.create_subprocess_exec(
            *cmd,
            env=self._host_environment(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
