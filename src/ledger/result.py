"""统一 Result 返回结构（t2 §3：全部函数返回结构化 JSON）。"""
from __future__ import annotations

from typing import Any


def ok(data: Any = None) -> dict:
    return {"ok": True, "data": data}


def err(reason: str, **extra: Any) -> dict:
    return {"ok": False, "reason": reason, **extra}
