from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from .models import deep_copy


class MemoryState:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._objects: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)

    async def put(self, resource: str, namespace: str, name: str, obj: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            self._objects[(resource, namespace)][name] = deep_copy(obj)
            return deep_copy(obj)

    async def get(self, resource: str, namespace: str, name: str) -> dict[str, Any] | None:
        async with self._lock:
            value = self._objects[(resource, namespace)].get(name)
            return deep_copy(value) if value else None

    async def delete(self, resource: str, namespace: str, name: str) -> dict[str, Any] | None:
        async with self._lock:
            value = self._objects[(resource, namespace)].pop(name, None)
            return deep_copy(value) if value else None

    async def list(self, resource: str, namespace: str | None = None) -> list[dict[str, Any]]:
        async with self._lock:
            out: list[dict[str, Any]] = []
            for (r, ns), values in self._objects.items():
                if r == resource and (namespace is None or namespace == ns):
                    out.extend(deep_copy(v) for v in values.values())
            return out
