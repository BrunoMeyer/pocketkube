from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .base import ExecResult
from .logs import LogBuffer, LogOptions
from .terminal import spawn_terminal, terminal_host_command
from .images import Image, ImageStore, guest_path, remove


class RawProotRuntime:
    """Rootless PRoot runtime with OCI images and an optional legacy Alpine rootfs."""

    def __init__(self, binary: str = "proot", rootfs: str | Path | None = None) -> None:
        self.binary = binary
        self._legacy_library_path: str | None = None
        self._link2symlink: bool | None = None
        configured = rootfs or os.environ.get("POCKETKUBE_PROOT_ROOTFS")
        self.rootfs = Path(configured).expanduser() if configured else None
        self.images = ImageStore()
        self._pod_images = {}
        self._output_tasks = {}
        self._logs = {}
        self._processes: dict[tuple[str, str], asyncio.subprocess.Process] = {}

    def _first_container(self, pod: dict[str, Any]) -> dict[str, Any]:
        containers = pod.get("spec", {}).get("containers") or []
        if not containers:
            raise RuntimeError("pod has no containers")
        if len(containers) != 1:
            raise RuntimeError("PocketKube currently supports exactly one container per pod")
        return containers[0]

    def _rootfs_for_image(self, image: str) -> Path:
        # Explicit legacy rootfs mode remains available for offline Alpine use.
        if image != "alpine" and not image.startswith("alpine:"):
            raise RuntimeError(
                f"image {image!r} is not supported by the raw PRoot backend yet; "
                "currently only alpine and alpine:* are supported"
            )

        rootfs = self.rootfs
        if rootfs is None or not rootfs.is_dir():
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

        # Modern Termux binaries use their embedded library search paths.
        # Forcing $PREFIX/lib can shadow Android's own liblzma and break
        # libunwindstack (for example, missing Xzs_Construct).
        override = env.get("POCKETKUBE_PROOT_LD_LIBRARY_PATH")
        if override is not None:
            if override:
                env["LD_LIBRARY_PATH"] = override
            else:
                env.pop("LD_LIBRARY_PATH", None)
        elif self._legacy_library_path is not None:
            env["LD_LIBRARY_PATH"] = self._legacy_library_path
        elif env.get("PREFIX") or env.get("TERMUX__PREFIX"):
            env.pop("LD_LIBRARY_PATH", None)

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

    def _supports_link2symlink(self) -> bool:
        # This extension is not present in every PRoot build. Probe once using
        # the host environment (and Android linker) also used for execution.
        if self._link2symlink is None:
            self._link2symlink = False
            try:
                env = self._host_environment()
                result = subprocess.run(
                    terminal_host_command([self.binary, "--help"], env),
                    env=env, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    timeout=2, check=False,
                )
                prefix = env.get("PREFIX") or env.get("TERMUX__PREFIX")
                missing_library = b"not found" in result.stdout and any(
                    name in result.stdout for name in (b"libtalloc.so", b"libandroid-support.so")
                )
                if (result.returncode != 0 and missing_library and prefix
                        and "POCKETKUBE_PROOT_LD_LIBRARY_PATH" not in env):
                    candidate = dict(env, LD_LIBRARY_PATH=str(Path(prefix) / "lib"))
                    retry = subprocess.run(
                        terminal_host_command([self.binary, "--help"], candidate),
                        env=candidate, stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        timeout=2, check=False,
                    )
                    # Keep the fallback only if it actually fixes host startup.
                    if retry.returncode == 0:
                        self._legacy_library_path = candidate["LD_LIBRARY_PATH"]
                        result = retry
                self._link2symlink = result.returncode == 0 and b"--link2symlink" in result.stdout
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                # Let the actual launch report any executable/linker failure.
                pass
        return self._link2symlink

    def _proot_command(
        self,
        rootfs: Path,
        command: list[str],
        container_env: list[str] | None = None,
    ) -> list[str]:
        if not command:
            raise RuntimeError("container command is empty")

        guest_command = [str(x) for x in command]
        base = [
            self.binary,
            *(["--link2symlink"] if self._supports_link2symlink() else []),
            "-0",
            "-r",
            str(rootfs),
            "-b", "/proc:/proc!",
            "-b", "/dev/null:/dev/null!",
            "-b", "/dev/zero:/dev/zero!",
            "-b", "/dev/random:/dev/random!",
            "-b", "/dev/urandom:/dev/urandom!",
            "-w",
            "/",
        ]
        shell = rootfs / "bin/sh"
        if not shell.exists() and not shell.is_symlink():
            return [*base, *guest_command]

        if container_env:
            guest_command = ["/usr/bin/env", *container_env, *guest_command]

        # Any host library path selected for legacy PRoot must be removed
        # before executing guest binaries.
        # Passing the actual command through "$@" avoids shell quoting bugs.
        wrapper = (
            "unset LD_LIBRARY_PATH LD_PRELOAD; "
            "export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin; "
            "export HOME=/root USER=root LOGNAME=root; "
            "exec \"$@\""
        )
        return [*base, "/bin/sh", "-c", wrapper, "pocketkube", *guest_command]

    async def start_pod(self, namespace: str, pod_name: str, pod: dict) -> None:
        container = self._first_container(pod)
        image = container.get("image")
        if not image:
            raise RuntimeError("container image is required")

        key = (namespace, pod_name)
        previous = self._processes.get(key)
        if previous is not None and previous.returncode is None:
            raise RuntimeError(f"pod {namespace}/{pod_name} is already running")

        if self.rootfs is not None and (image == "alpine" or image.startswith("alpine:")):
            resolved = Image(self._rootfs_for_image(image), {"Cmd": ["/bin/sh"]})
        else:
            default_policy = "Always" if ":" not in image.rsplit("/", 1)[-1] or image.endswith(":latest") else "IfNotPresent"
            resolved = await asyncio.get_running_loop().run_in_executor(
                None, self.images.pull, image, container.get("imagePullPolicy", default_policy)
            )
        command = self._image_command(container, resolved.config)
        if not command:
            raise RuntimeError("image and container specify no command")
        pods = self.images.directory.parent / "pods"
        pods.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix="pod-", dir=str(pods)))
        rootfs = directory / "rootfs"
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, lambda: shutil.copytree(resolved.rootfs, rootfs, symlinks=True)
            )
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        old = self._pod_images.pop(key, None)
        if old:
            shutil.rmtree(old.rootfs.parent, ignore_errors=True)
        self._pod_images[key] = Image(rootfs, resolved.config)
        try:
            self._prepare_devices(rootfs)
            cmd = self._pod_command(key, container, command)
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                env=self._host_environment(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except BaseException:
            self._pod_images.pop(key, None)
            shutil.rmtree(directory, ignore_errors=True)
            raise

        output = bytearray()
        logs = LogBuffer()
        old_logs = self._logs.get(key)
        if old_logs:
            old_logs.close()
        self._logs[key] = logs
        output_task = asyncio.create_task(self._capture_output(proc.stdout, output, logs))
        self._output_tasks[key] = output_task

        # Most setup/ABI errors happen immediately. Give PRoot a short chance
        # to report them before marking the Pod Running.
        try:
            await asyncio.wait_for(proc.wait(), timeout=0.25)
        except asyncio.TimeoutError:
            self._processes[key] = proc
            return

        await self._finish_output(key, proc)
        self._pod_images.pop(key, None)
        shutil.rmtree(directory, ignore_errors=True)
        raise RuntimeError(
            f"proot exited immediately with status {proc.returncode}"
            + (":\n" + output.decode(errors="replace").strip() if output else "")
        )

    async def stop_pod(self, namespace: str, pod_name: str, pod: dict) -> None:
        key = (namespace, pod_name)
        proc = self._processes.pop(key, None)
        if proc is not None and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        await self._finish_output(key, proc)
        logs = self._logs.pop(key, None)
        if logs:
            logs.close()
        image = self._pod_images.pop(key, None)
        if image:
            await asyncio.get_running_loop().run_in_executor(
                None, lambda: shutil.rmtree(image.rootfs.parent, ignore_errors=True)
            )

    @staticmethod
    def _prepare_devices(rootfs):
        # OCI layers do not supply a working /dev. Keep device bindings narrow,
        # and provide stdio links even on Android hosts without /dev/stdout.
        dev = guest_path(rootfs, "dev", True)
        dev.mkdir(parents=True, exist_ok=True)
        for name, target in {"stdin": "/proc/self/fd/0", "stdout": "/proc/self/fd/1",
                             "stderr": "/proc/self/fd/2", "fd": "/proc/self/fd"}.items():
            path = dev / name
            remove(path)
            path.symlink_to(target)

    async def logs(self, namespace, pod_name, pod, options: LogOptions):
        logs = self._logs.get((namespace, pod_name))
        if logs is None:
            raise RuntimeError("container logs are not available; the container has not started")
        return logs.stream(options)

    @staticmethod
    async def _capture_output(stream, output, logs=None):
        # A single reader feeds both startup diagnostics and independent log
        # followers, so a slow HTTP client never blocks the container's pipe.
        try:
            if stream is None:
                return
            while True:
                chunk = await stream.read(8192)
                if not chunk:
                    return
                output.extend(chunk)
                del output[:-65536]
                if logs is not None:
                    logs.append(chunk)
        finally:
            if logs is not None:
                logs.close()

    async def _finish_output(self, key, proc):
        task = self._output_tasks.pop(key, None)
        if task is None:
            return
        try:
            await asyncio.wait_for(task, timeout=3)
        except asyncio.TimeoutError:
            # A descendant may retain the write end after PRoot exits. asyncio
            # Process has no public close method; close its transport so the
            # abandoned pipe does not survive until event-loop shutdown.
            transport = getattr(proc, "_transport", None)
            if transport is not None:
                transport.close()

    @staticmethod
    def _image_command(container, config):
        return [*map(str, container.get("command") or config.get("Entrypoint") or []),
                *map(str, container.get("args") or config.get("Cmd") or [])]

    def _pod_command(self, key, container, command):
        image = self._pod_images.get(key)
        if image is None:
            raise RuntimeError("pod rootfs is unavailable; start the pod first")
        env = dict(item.split("=", 1) for item in image.config.get("Env", []) if "=" in item)
        env.update(item.split("=", 1) for item in self._container_environment(container))
        cmd = self._proot_command(image.rootfs, command, [k + "=" + v for k, v in env.items()])
        cmd[cmd.index("-w") + 1] = container.get("workingDir") or image.config.get("WorkingDir") or "/"
        user = image.config.get("User")
        if user and user not in ("0", "root", "0:0", "root:root"):
            # PRoot accepts numeric IDs; resolve named image users from guest passwd.
            name, _, group = user.partition(":")
            uid, gid = name, group or name
            if not name.isdigit():
                passwd = guest_path(image.rootfs, "etc/passwd", True)
                rows = [line.split(":") for line in passwd.read_text().splitlines()]
                row = next((r for r in rows if len(r) >= 4 and r[0] == name), None)
                if row is None:
                    raise RuntimeError("image user not found: " + name)
                uid, gid = row[2], group or row[3]
            if not gid.isdigit():
                groups = guest_path(image.rootfs, "etc/group", True).read_text().splitlines()
                gid = next((r.split(":")[2] for r in groups if r.split(":")[0] == gid), "")
            if not uid.isdigit() or not gid.isdigit():
                raise RuntimeError("cannot resolve image user: " + user)
            index = cmd.index("-0")
            cmd[index:index + 1] = ["-i", uid + ":" + gid]
        return cmd

    async def exec(self, namespace: str, pod_name: str, pod: dict, command: list[str]) -> ExecResult:
        container = self._first_container(pod)
        image = container.get("image")
        if not image:
            raise RuntimeError("container image is required")

        cmd = self._pod_command((namespace, pod_name), container, command)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            env=self._host_environment(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return ExecResult(proc.returncode or 0, stdout, stderr)

    async def exec_stream(self, namespace: str, pod_name: str, pod: dict, command: list[str], tty: bool = False):
        container = self._first_container(pod)
        image = container.get("image")
        if not image:
            raise RuntimeError("container image is required")

        cmd = self._pod_command((namespace, pod_name), container, command)
        if tty:
            return await spawn_terminal(cmd, env=self._host_environment())
        return await asyncio.create_subprocess_exec(
            *cmd,
            env=self._host_environment(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    async def open_port(self, namespace, pod_name, pod, port):
        proc = self._processes.get((namespace, pod_name))
        if proc is None or proc.returncode is not None:
            raise RuntimeError("pod is not running")
        # PRoot shares host networking; containerPort does not create a listener.
        return await asyncio.open_connection("127.0.0.1", port)
