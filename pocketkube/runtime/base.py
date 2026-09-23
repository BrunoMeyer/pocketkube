from __future__ import annotations

from .logs import LogOptions

from dataclasses import dataclass
from typing import AsyncIterator, Protocol


@dataclass
class ExecResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class Runtime(Protocol):
    async def start_pod(self, namespace: str, pod_name: str, pod: dict) -> None: ...
    async def stop_pod(self, namespace: str, pod_name: str, pod: dict) -> None: ...
    async def exec(self, namespace: str, pod_name: str, pod: dict, command: list[str]) -> ExecResult: ...
    async def exec_stream(self, namespace: str, pod_name: str, pod: dict, command: list[str], tty: bool = False): ...

    async def logs(self, namespace: str, pod_name: str, pod: dict, options: LogOptions) -> AsyncIterator[bytes]: ...

    async def open_port(self, namespace: str, pod_name: str, pod: dict, port: int): ...
