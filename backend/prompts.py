"""Reusable system-prompt library, persisted on disk.

Each saved prompt (a.k.a. "preset" / "instruction template") is one JSON file:

    prompts/<prompt_id>.json   # {id, name, content, created_at, updated_at}

These are independent of spaces: any space can load a preset into its own
system prompt. Override the root with the PROMPTS_DIR env var.
"""

import os
import json
import uuid
import threading
from datetime import datetime, timezone

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_BACKEND_DIR)
PROMPTS_DIR = os.environ.get("PROMPTS_DIR", os.path.join(_PROJECT_ROOT, "prompts"))

_lock = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path(prompt_id: str) -> str:
    return os.path.join(PROMPTS_DIR, f"{prompt_id}.json")


def _read(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write(path: str, data: dict) -> None:
    os.makedirs(PROMPTS_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def create_prompt(name: str, content: str) -> dict:
    prompt_id = uuid.uuid4().hex
    data = {
        "id": prompt_id,
        "name": (name or "").strip() or "Untitled prompt",
        "content": content or "",
        "created_at": _now(),
        "updated_at": _now(),
    }
    with _lock:
        _write(_path(prompt_id), data)
    return data


def list_prompts() -> list:
    if not os.path.isdir(PROMPTS_DIR):
        return []
    out = []
    for fn in os.listdir(PROMPTS_DIR):
        if not fn.endswith(".json"):
            continue
        try:
            out.append(_read(os.path.join(PROMPTS_DIR, fn)))
        except (OSError, json.JSONDecodeError):
            continue
    out.sort(key=lambda p: p.get("name", "").lower())
    return out


def get_prompt(prompt_id: str) -> dict:
    path = _path(prompt_id)
    if not os.path.isfile(path):
        raise KeyError(f"Prompt '{prompt_id}' not found")
    return _read(path)


def update_prompt(prompt_id: str, name: str | None = None,
                  content: str | None = None) -> dict:
    with _lock:
        data = get_prompt(prompt_id)
        if name is not None and name.strip():
            data["name"] = name.strip()
        if content is not None:
            data["content"] = content
        data["updated_at"] = _now()
        _write(_path(prompt_id), data)
    return data


def delete_prompt(prompt_id: str) -> None:
    with _lock:
        try:
            os.remove(_path(prompt_id))
        except OSError:
            pass
