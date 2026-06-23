"""Filesystem persistence for Spaces, their files, and their chats.

A "Space" is a self-contained project. On disk:

    spaces/<folder_name>/              # human-readable, e.g. "test" or "my-project__a1b2c3d4"
        space.json                      # metadata + file list (id is stable UUID)
        media/<original-filename>       # uploaded files (deduped if same name)
        chats/<slug>__<short_id>.json   # e.g. assignment-questions__c0846060.json

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


def _slug(name: str, max_len: int = 48) -> str:
    """Filesystem-safe slug from a display name."""
    s = name.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return (s[:max_len] or "space")


def _chat_slug(title: str, max_len: int = 48) -> str:
    s = title.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return (s[:max_len] or "chat")


def _looks_like_uuid(value: str) -> bool:
    v = (value or "").strip().lower().removesuffix(".json")
    return bool(re.fullmatch(r"[a-f0-9]{32}", v))


def _title_from_text(text: str, max_len: int = 48) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t:
        return "New chat"
    if len(t) <= max_len:
        return t
    return t[: max_len - 3].rstrip() + "..."


def _resolve_chat_title(data: dict, filename: str) -> str:
    """Pick a human-readable title; repair legacy uuid-only titles."""
    title = (data.get("title") or "").strip()
    chat_id = data.get("id", "")

    if title and not _looks_like_uuid(title) and title.lower() != "chat":
        return title

    stem = filename[:-5] if filename.endswith(".json") else filename
    if "__" in stem:
        slug_part = stem.rsplit("__", 1)[0]
        if slug_part and slug_part != "chat":
            return slug_part.replace("-", " ")

    for msg in data.get("messages", []):
        if msg.get("role") == "user":
            return _title_from_text(msg.get("content", ""))

    if _looks_like_uuid(stem):
        return "New chat"
    return _title_from_text(stem.replace("-", " ")) or "New chat"


def _unique_chat_filename(space_id: str, title: str, chat_id: str) -> str:
    """Readable chat filename; stable id suffix avoids collisions."""
    base = _chat_slug(title)
    candidate = f"{base}__{chat_id[:8]}.json"
    path = os.path.join(_chats_dir(space_id), candidate)
    if not os.path.exists(path):
        return candidate
    return f"{base}__{chat_id[:12]}.json"


def _find_chat_path(space_id: str, chat_id: str) -> str | None:
    """Locate a chat JSON file by its stable id (supports legacy uuid.json names)."""
    cdir = _chats_dir(space_id)
    if not os.path.isdir(cdir):
        return None
    legacy = os.path.join(cdir, f"{chat_id}.json")
    if os.path.isfile(legacy):
        return legacy
    for fn in os.listdir(cdir):
        if not fn.endswith(".json"):
            continue
        path = os.path.join(cdir, fn)
        try:
            data = _read_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("id") == chat_id:
            return path
    return None


def _sync_chat_file(space_id: str, data: dict, path: str) -> str:
    """Ensure title is readable and on-disk name matches the title slug."""
    chat_id = data.get("id") or ""
    title = _resolve_chat_title(data, os.path.basename(path))
    if data.get("title") != title:
        data["title"] = title

    desired = _unique_chat_filename(space_id, title, chat_id)
    desired_path = os.path.join(_chats_dir(space_id), desired)
    if os.path.normpath(path) != os.path.normpath(desired_path):
        if os.path.exists(desired_path) and os.path.normpath(desired_path) != os.path.normpath(path):
            desired = f"{_chat_slug(title)}__{chat_id[:12]}.json"
            desired_path = os.path.join(_chats_dir(space_id), desired)
        _write_json(path, data)
        os.replace(path, desired_path)
        return desired_path

    _write_json(path, data)
    return path


def _find_space_dir(space_id: str) -> str | None:
    """Locate a space folder by its stable id (works for old UUID folder names too)."""
    if not os.path.isdir(SPACES_DIR):
        return None
    for entry in os.listdir(SPACES_DIR):
        meta = os.path.join(SPACES_DIR, entry, "space.json")
        if not os.path.isfile(meta):
            continue
        try:
            data = _read_json(meta)
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("id") == space_id:
            return os.path.join(SPACES_DIR, entry)
    return None


def _space_dir(space_id: str) -> str:
    d = _find_space_dir(space_id)
    if d is None:
        raise KeyError(f"Space '{space_id}' not found")
    return d


def _unique_folder(name: str, space_id: str) -> str:
    """Pick a readable folder name; append short id suffix on collision."""
    base = _slug(name)
    candidate = base
    if os.path.exists(os.path.join(SPACES_DIR, candidate)):
        candidate = f"{base}__{space_id[:8]}"
    return candidate


def _media_dir(space_id: str) -> str:
    return os.path.join(_space_dir(space_id), "media")


def _chats_dir(space_id: str) -> str:
    return os.path.join(_space_dir(space_id), "chats")


def _space_json_path(space_id: str) -> str:
    return os.path.join(_space_dir(space_id), "space.json")


def _unique_media_name(space_id: str, original_name: str) -> str:
    """Readable stored filename; add _2, _3 … if the name already exists."""
    safe = _safe_name(original_name)
    media = _media_dir(space_id)
    path = os.path.join(media, safe)
    if not os.path.exists(path):
        return safe
    stem, ext = os.path.splitext(safe)
    for i in range(2, 100):
        candidate = f"{stem}_{i}{ext}"
        if not os.path.exists(os.path.join(media, candidate)):
            return candidate
    return f"{stem}_{_new_id()[:6]}{ext}"


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
    folder = _unique_folder(name, space_id)
    root = os.path.join(SPACES_DIR, folder)
    with _lock:
        os.makedirs(os.path.join(root, "media"), exist_ok=True)
        os.makedirs(os.path.join(root, "chats"), exist_ok=True)
        data = {
            "id": space_id,
            "name": name,
            "folder": folder,
            "system_prompt": "",
            "created_at": _now(),
            "files": [],
        }
        _write_json(os.path.join(root, "space.json"), data)
    return data


def update_space(space_id: str, name: str | None = None,
                 system_prompt: str | None = None) -> dict:
    """Update a space's name and/or system prompt. Renames folder when name changes."""
    with _lock:
        data = get_space(space_id)
        old_dir = _space_dir(space_id)
        if name is not None and name.strip():
            new_name = name.strip()
            if new_name != data.get("name"):
                data["name"] = new_name
                new_folder = _unique_folder(new_name, space_id)
                new_dir = os.path.join(SPACES_DIR, new_folder)
                if new_dir != old_dir:
                    os.rename(old_dir, new_dir)
                data["folder"] = new_folder
        if system_prompt is not None:
            data["system_prompt"] = system_prompt
        _write_json(_space_json_path(space_id), data)
    return data


def list_spaces() -> list:
    if not os.path.isdir(SPACES_DIR):
        return []
    out = []
    for entry in os.listdir(SPACES_DIR):
        meta = os.path.join(SPACES_DIR, entry, "space.json")
        if not os.path.isfile(meta):
            continue
        try:
            data = _read_json(meta)
        except (OSError, json.JSONDecodeError):
            continue
        out.append({
            "id": data.get("id", entry),
            "name": data.get("name", entry),
            "folder": data.get("folder", entry),
            "created_at": data.get("created_at", ""),
            "n_files": len(data.get("files", [])),
        })
    out.sort(key=lambda s: s.get("created_at", ""))
    return out


def get_space(space_id: str) -> dict:
    path = _space_json_path(space_id)
    if not os.path.isfile(path):
        raise KeyError(f"Space '{space_id}' not found")
    return _read_json(path)


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
    if not os.path.isfile(_space_json_path(space_id)):
        raise KeyError(f"Space '{space_id}' not found")
    file_id = _new_id()
    stored_name = _unique_media_name(space_id, original_name)
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
        _write_json(_space_json_path(space_id), data)
    return entry


def remove_file(space_id: str, file_id: str) -> dict:
    """Delete the media file and its space.json record. Returns the record."""
    with _lock:
        data = get_space(space_id)
        match = next((f for f in data["files"] if f["file_id"] == file_id), None)
        if match is None:
            raise KeyError(f"File '{file_id}' not found in space '{space_id}'")
        data["files"] = [f for f in data["files"] if f["file_id"] != file_id]
        _write_json(_space_json_path(space_id), data)
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
    path = _find_chat_path(space_id, chat_id)
    if path is None:
        raise KeyError(f"Chat '{chat_id}' not found in space '{space_id}'")
    return path


def create_chat(space_id: str, title: str = "") -> dict:
    if not os.path.isfile(_space_json_path(space_id)):
        raise KeyError(f"Space '{space_id}' not found")
    chat_id = _new_id()
    clean_title = _title_from_text(title) if (title or "").strip() else "New chat"
    with _lock:
        os.makedirs(_chats_dir(space_id), exist_ok=True)
        data = {
            "id": chat_id,
            "space_id": space_id,
            "title": clean_title,
            "created_at": _now(),
            "updated_at": _now(),
            "messages": [],
        }
        fn = _unique_chat_filename(space_id, clean_title, chat_id)
        _write_json(os.path.join(_chats_dir(space_id), fn), data)
    return data


def list_chats(space_id: str) -> list:
    cdir = _chats_dir(space_id)
    if not os.path.isdir(cdir):
        return []
    out = []
    for fn in os.listdir(cdir):
        if not fn.endswith(".json"):
            continue
        path = os.path.join(cdir, fn)
        try:
            data = _read_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        with _lock:
            path = _sync_chat_file(space_id, data, path)
            data = _read_json(path)
        out.append({
            "id": data.get("id"),
            "title": data.get("title", "New chat"),
            "created_at": data.get("created_at", ""),
            "updated_at": data.get("updated_at", ""),
        })
    out.sort(key=lambda c: c.get("updated_at", ""), reverse=True)
    return out


def get_chat(space_id: str, chat_id: str) -> dict:
    path = _chat_path(space_id, chat_id)
    data = _read_json(path)
    with _lock:
        path = _sync_chat_file(space_id, data, path)
    return _read_json(path)


def delete_chat(space_id: str, chat_id: str) -> None:
    with _lock:
        try:
            os.remove(_chat_path(space_id, chat_id))
        except OSError:
            pass


def update_chat(space_id: str, chat_id: str, title: str) -> dict:
    title = _title_from_text(title)
    if not title:
        raise ValueError("Chat title cannot be empty")
    with _lock:
        path = _chat_path(space_id, chat_id)
        data = _read_json(path)
        data["title"] = title
        data["updated_at"] = _now()
        path = _sync_chat_file(space_id, data, path)
    return _read_json(path)


def append_message(space_id: str, chat_id: str, role: str, content: str,
                   sources: list | None = None) -> dict:
    """Append a message; auto-title from first user question when title is generic."""
    with _lock:
        path = _chat_path(space_id, chat_id)
        data = _read_json(path)
        msg = {"role": role, "content": content}
        if sources:
            msg["sources"] = sources
        data["messages"].append(msg)
        data["updated_at"] = _now()

        user_msgs = [m for m in data["messages"] if m.get("role") == "user"]
        current_title = (data.get("title") or "").strip()
        if (
            role == "user"
            and len(user_msgs) == 1
            and (not current_title or current_title == "New chat" or _looks_like_uuid(current_title))
        ):
            data["title"] = _title_from_text(content)

        path = _sync_chat_file(space_id, data, path)
    return msg
