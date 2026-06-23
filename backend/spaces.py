"""Filesystem persistence for Spaces, their files, and their chats.

A "Space" is a self-contained project. On disk:

    spaces/<space_id>/
        space.json                      # metadata + file list
        media/<file_id>__<name>         # original uploaded files
        chats/<chat_id>.json            # one conversation each

Vectors live in Qdrant (see qdrant_store.py); this module only owns the
files + JSON metadata. Override the root with the SPACES_DIR env var.
"""

import os
import re
import json
import uuid
import shutil
import threading
from datetime import datetime, timezone

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_BACKEND_DIR)
SPACES_DIR = os.environ.get("SPACES_DIR", os.path.join(_PROJECT_ROOT, "spaces"))

# Serialises metadata read-modify-write so concurrent requests can't corrupt it.
_lock = threading.RLock()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex


def _safe_name(name: str) -> str:
    """Strip directory parts and unsafe characters from an uploaded filename."""
    base = os.path.basename(name.replace("\\", "/"))
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base).strip("._") or "file"
    return base[:120]


def _space_dir(space_id: str) -> str:
    return os.path.join(SPACES_DIR, space_id)


def _media_dir(space_id: str) -> str:
    return os.path.join(_space_dir(space_id), "media")


def _chats_dir(space_id: str) -> str:
    return os.path.join(_space_dir(space_id), "chats")


def _space_json(space_id: str) -> str:
    return os.path.join(_space_dir(space_id), "space.json")


def _read_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: str, data: dict) -> None:
    # Atomic-ish write: tmp then replace.
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def kind_for(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    if ext == ".pdf":
        return "pdf"
    if ext in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff"}:
        return "image"
    if ext in {".mp4", ".avi", ".mov", ".mkv"}:
        return "video"
    return "other"


# --------------------------------------------------------------------------- #
# Spaces
# --------------------------------------------------------------------------- #
def create_space(name: str) -> dict:
    name = (name or "").strip() or "Untitled space"
    space_id = _new_id()
    with _lock:
        os.makedirs(_media_dir(space_id), exist_ok=True)
        os.makedirs(_chats_dir(space_id), exist_ok=True)
        data = {
            "id": space_id,
            "name": name,
            "created_at": _now(),
            "files": [],
        }
        _write_json(_space_json(space_id), data)
    return data


def list_spaces() -> list:
    if not os.path.isdir(SPACES_DIR):
        return []
    out = []
    for entry in os.listdir(SPACES_DIR):
        meta = _space_json(entry)
        if not os.path.isfile(meta):
            continue
        try:
            data = _read_json(meta)
        except (OSError, json.JSONDecodeError):
            continue
        out.append({
            "id": data.get("id", entry),
            "name": data.get("name", entry),
            "created_at": data.get("created_at", ""),
            "n_files": len(data.get("files", [])),
        })
    out.sort(key=lambda s: s.get("created_at", ""))
    return out


def get_space(space_id: str) -> dict:
    meta = _space_json(space_id)
    if not os.path.isfile(meta):
        raise KeyError(f"Space '{space_id}' not found")
    return _read_json(meta)


def delete_space(space_id: str) -> None:
    with _lock:
        d = _space_dir(space_id)
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Files within a space
# --------------------------------------------------------------------------- #
def store_file_bytes(space_id: str, file_bytes: bytes, original_name: str) -> dict:
    """Persist raw bytes to the space's media dir. Returns a partial file record.

    n_chunks is filled in later via register_file once ingestion is done.
    """
    if not os.path.isfile(_space_json(space_id)):
        raise KeyError(f"Space '{space_id}' not found")
    file_id = _new_id()
    safe = _safe_name(original_name)
    stored_name = f"{file_id}__{safe}"
    with _lock:
        os.makedirs(_media_dir(space_id), exist_ok=True)
        with open(os.path.join(_media_dir(space_id), stored_name), "wb") as f:
            f.write(file_bytes)
    return {
        "file_id": file_id,
        "original_name": original_name,
        "stored_name": stored_name,
        "kind": kind_for(original_name),
    }


def register_file(space_id: str, record: dict, n_chunks: int) -> dict:
    """Add a stored file (with its chunk count) to space.json."""
    with _lock:
        data = get_space(space_id)
        entry = {
            "file_id": record["file_id"],
            "original_name": record["original_name"],
            "stored_name": record["stored_name"],
            "kind": record.get("kind", kind_for(record["original_name"])),
            "n_chunks": n_chunks,
            "added_at": _now(),
        }
        data["files"].append(entry)
        _write_json(_space_json(space_id), data)
    return entry


def remove_file(space_id: str, file_id: str) -> dict:
    """Delete the media file and its space.json record. Returns the record."""
    with _lock:
        data = get_space(space_id)
        match = next((f for f in data["files"] if f["file_id"] == file_id), None)
        if match is None:
            raise KeyError(f"File '{file_id}' not found in space '{space_id}'")
        data["files"] = [f for f in data["files"] if f["file_id"] != file_id]
        _write_json(_space_json(space_id), data)
        media_path = os.path.join(_media_dir(space_id), match["stored_name"])
        try:
            os.remove(media_path)
        except OSError:
            pass
    return match


def get_file_path(space_id: str, file_id: str) -> str:
    data = get_space(space_id)
    match = next((f for f in data["files"] if f["file_id"] == file_id), None)
    if match is None:
        raise KeyError(f"File '{file_id}' not found in space '{space_id}'")
    return os.path.join(_media_dir(space_id), match["stored_name"])


# --------------------------------------------------------------------------- #
# Chats within a space
# --------------------------------------------------------------------------- #
def _chat_path(space_id: str, chat_id: str) -> str:
    return os.path.join(_chats_dir(space_id), f"{chat_id}.json")


def create_chat(space_id: str, title: str = "") -> dict:
    if not os.path.isfile(_space_json(space_id)):
        raise KeyError(f"Space '{space_id}' not found")
    chat_id = _new_id()
    with _lock:
        os.makedirs(_chats_dir(space_id), exist_ok=True)
        data = {
            "id": chat_id,
            "space_id": space_id,
            "title": (title or "").strip() or "New chat",
            "created_at": _now(),
            "updated_at": _now(),
            "messages": [],
        }
        _write_json(_chat_path(space_id, chat_id), data)
    return data


def list_chats(space_id: str) -> list:
    cdir = _chats_dir(space_id)
    if not os.path.isdir(cdir):
        return []
    out = []
    for fn in os.listdir(cdir):
        if not fn.endswith(".json"):
            continue
        try:
            data = _read_json(os.path.join(cdir, fn))
        except (OSError, json.JSONDecodeError):
            continue
        out.append({
            "id": data.get("id"),
            "title": data.get("title", "Chat"),
            "created_at": data.get("created_at", ""),
            "updated_at": data.get("updated_at", ""),
        })
    out.sort(key=lambda c: c.get("updated_at", ""), reverse=True)
    return out


def get_chat(space_id: str, chat_id: str) -> dict:
    path = _chat_path(space_id, chat_id)
    if not os.path.isfile(path):
        raise KeyError(f"Chat '{chat_id}' not found in space '{space_id}'")
    return _read_json(path)


def delete_chat(space_id: str, chat_id: str) -> None:
    with _lock:
        try:
            os.remove(_chat_path(space_id, chat_id))
        except OSError:
            pass


def append_message(space_id: str, chat_id: str, role: str, content: str,
                   sources: list | None = None) -> dict:
    """Append a message; auto-title the chat from the first user message."""
    with _lock:
        data = get_chat(space_id, chat_id)
        msg = {"role": role, "content": content}
        if sources:
            msg["sources"] = sources
        data["messages"].append(msg)
        data["updated_at"] = _now()
        if role == "user" and data.get("title", "New chat") == "New chat":
            data["title"] = (content[:48] + "...") if len(content) > 48 else content
        _write_json(_chat_path(space_id, chat_id), data)
    return msg
