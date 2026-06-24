"""Track in-flight chat generations (for cancel on disconnect + resume UI)."""

from __future__ import annotations

import asyncio
import threading

_lock = threading.Lock()
_active: dict[str, dict] = {}


def _key(space_id: str, chat_id: str) -> str:
    return f"{space_id}:{chat_id}"


async def begin(space_id: str, chat_id: str) -> asyncio.Event:
    cancel = asyncio.Event()
    with _lock:
        _active[_key(space_id, chat_id)] = {
            "cancel": cancel,
            "partial": "",
            "query": "",
        }
    return cancel


async def end(space_id: str, chat_id: str) -> None:
    with _lock:
        _active.pop(_key(space_id, chat_id), None)


async def set_query(space_id: str, chat_id: str, query: str) -> None:
    with _lock:
        row = _active.get(_key(space_id, chat_id))
        if row is not None:
            row["query"] = query


async def set_partial(space_id: str, chat_id: str, partial: str) -> None:
    with _lock:
        row = _active.get(_key(space_id, chat_id))
        if row is not None:
            row["partial"] = partial


def snapshot(space_id: str, chat_id: str) -> dict | None:
    row = _active.get(_key(space_id, chat_id))
    if not row:
        return None
    return {
        "generating": True,
        "partial": row.get("partial") or "",
        "query": row.get("query") or "",
    }
