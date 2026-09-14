from __future__ import annotations

import copy
import time
import uuid
from typing import Any


def now_rfc3339() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def uid() -> str:
    return str(uuid.uuid4())


def deep_copy(obj: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(obj)


def ensure_metadata(obj: dict[str, Any], namespace: str | None = None) -> dict[str, Any]:
    obj = deep_copy(obj)
    meta = obj.setdefault("metadata", {})
    meta.setdefault("uid", uid())
    meta.setdefault("creationTimestamp", now_rfc3339())
    if namespace is not None:
        meta.setdefault("namespace", namespace)
    return obj


def status_object(message: str, code: int = 200, reason: str = "Success") -> dict[str, Any]:
    return {
        "kind": "Status",
        "apiVersion": "v1",
        "metadata": {},
        "status": "Success" if code < 400 else "Failure",
        "message": message,
        "reason": reason,
        "code": code,
    }
