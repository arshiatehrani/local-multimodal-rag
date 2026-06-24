"""FastAPI application: spaces, files, chats, and the SSE chat endpoint.

A "Space" groups uploaded files (persisted on disk + vectors in Qdrant) with
saved chats. Search and chat are always scoped to a single space.
"""

import json
import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel

# ---- Logging ---------------------------------------------------------------
_LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _LOG_LEVEL, logging.INFO),
    format="%(asctime)s  %(levelname)-5s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("rag")

# Suppress noisy per-request access logs for the health endpoint.
class _HealthFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "/health" in msg or "/models/status" in msg:
            return False
        return True

for _uv_name in ("uvicorn.access",):
    logging.getLogger(_uv_name).addFilter(_HealthFilter())

# ---- Constants -------------------------------------------------------------
# Maximum upload size per file (bytes). Default 200 MB.
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(200 * 1024 * 1024)))
# CORS origins — comma-separated list or "*" for development.
CORS_ORIGINS = os.environ.get(
    "CORS_ORIGINS",
    "http://localhost:3000,http://localhost:8000,http://127.0.0.1:3000,http://127.0.0.1:8000",
).split(",")

import spaces
import prompts
from qdrant_store import ensure_collection, delete_by_file, delete_by_space
from ingest import ingest_file_stream, is_supported
from query import run_query
from rag_context import context_for_messages
from model_manager import manager
import chat_stream


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_collection()
    # Warm up the embedder in the background (safe on 6 GB). Loading all three at
    # startup can OOM; set PRELOAD_MODELS=all to try. /health returns immediately.
    app.state.warmup_task = asyncio.create_task(manager.preload_all())
    yield
    task = getattr(app.state, "warmup_task", None)
    if task and not task.done():
        task.cancel()


app = FastAPI(title="Multimodal RAG API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _sse(payload: dict) -> str:
    # Leading comment helps proxies flush each event promptly.
    return f":\ndata: {json.dumps(payload)}\n\n"


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/models/status")
def models_status():
    return manager.status()


# --------------------------------------------------------------------------- #
# Spaces
# --------------------------------------------------------------------------- #
class CreateSpaceRequest(BaseModel):
    name: str = ""


@app.post("/spaces")
async def create_space(req: CreateSpaceRequest):
    return await asyncio.to_thread(spaces.create_space, req.name)


@app.get("/spaces")
async def list_spaces():
    return {"spaces": await asyncio.to_thread(spaces.list_spaces)}


@app.get("/spaces/{space_id}")
def get_space(space_id: str):
    try:
        return spaces.get_space(space_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Space not found")


class UpdateSpaceRequest(BaseModel):
    name: str | None = None
    system_prompt: str | None = None


@app.patch("/spaces/{space_id}")
def update_space(space_id: str, req: UpdateSpaceRequest):
    try:
        return spaces.update_space(space_id, name=req.name, system_prompt=req.system_prompt)
    except KeyError:
        raise HTTPException(status_code=404, detail="Space not found")


@app.delete("/spaces/{space_id}")
async def delete_space(space_id: str):
    try:
        await asyncio.to_thread(spaces.get_space, space_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Space not found")
    await asyncio.to_thread(spaces.delete_space, space_id)
    asyncio.create_task(asyncio.to_thread(delete_by_space, space_id))
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Files within a space
# --------------------------------------------------------------------------- #
@app.post("/spaces/{space_id}/files")
async def upload_files(space_id: str, files: list[UploadFile] = File(...)):
    try:
        spaces.get_space(space_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Space not found")

    async def stream():
        supported = [f for f in files if is_supported(f.filename or "")]
        n_files = len(supported)
        results = []

        for idx, file in enumerate(files):
            name = file.filename or "file"
            if not is_supported(name):
                results.append({"filename": name, "status": "skipped", "error": "unsupported file type"})
                yield _sse({"type": "progress", "pct": 100, "text": f"Skipped {name}", "file": name})
                continue

            sup_idx = len([f for f in files[:idx] if is_supported(f.filename or "")])
            base = int(100 * sup_idx / max(n_files, 1))
            span = max(1, int(100 / max(n_files, 1)))

            yield _sse({
                "type": "progress",
                "pct": base + max(1, int(span * 0.02)),
                "text": f"Uploading {name}…",
                "file": name,
                "file_index": sup_idx,
                "file_count": n_files,
                "stage": "upload",
            })

            try:
                contents = await file.read()
                if len(contents) > MAX_UPLOAD_BYTES:
                    results.append({
                        "filename": name, "status": "error",
                        "error": f"File exceeds {MAX_UPLOAD_BYTES // (1024*1024)} MB limit",
                    })
                    yield _sse({
                        "type": "file_done", "filename": name,
                        "status": "error",
                        "error": f"File exceeds {MAX_UPLOAD_BYTES // (1024*1024)} MB limit",
                    })
                    continue
                yield _sse({
                    "type": "progress",
                    "pct": base + int(span * 0.08),
                    "text": "Saving file…",
                    "file": name,
                    "stage": "store",
                })
                record = spaces.store_file_bytes(space_id, contents, name)
                n = 0
                text_stats = None
                async for ev in ingest_file_stream(contents, name, space_id, record["file_id"]):
                    if ev.get("type") == "complete":
                        n = int(ev.get("chunks", 0))
                        text_stats = ev.get("text_stats")
                        inner_pct = 100
                        text = ev.get("text", "Complete")
                    else:
                        inner_pct = int(ev.get("pct", 0))
                        text = ev.get("text", "Processing…")
                    scaled = base + int(span * (0.1 + 0.9 * inner_pct / 100))
                    yield _sse({
                        "type": "progress",
                        "pct": min(99, scaled),
                        "text": text,
                        "file": name,
                        "file_index": sup_idx,
                        "file_count": n_files,
                        "stage": ev.get("stage", "embed"),
                    })

                spaces.register_file(space_id, record, n, text_stats=text_stats)
                results.append({
                    "filename": name,
                    "status": "ok",
                    "file_id": record["file_id"],
                    "chunks_stored": n,
                })
                yield _sse({
                    "type": "file_done",
                    "filename": name,
                    "status": "ok",
                    "chunks_stored": n,
                    "pct": base + span,
                })
            except Exception as e:  # noqa: BLE001
                results.append({"filename": name, "status": "error", "error": str(e)})
                yield _sse({
                    "type": "file_done",
                    "filename": name,
                    "status": "error",
                    "error": str(e),
                })

        yield _sse({"type": "done", "pct": 100, "results": results})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.delete("/spaces/{space_id}/files/{file_id}")
def delete_file(space_id: str, file_id: str):
    try:
        spaces.get_space(space_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Space not found")
    delete_by_file(file_id)
    try:
        spaces.remove_file(space_id, file_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="File not found")
    return {"status": "ok"}


@app.get("/spaces/{space_id}/files/{file_id}/raw")
def get_file_raw(space_id: str, file_id: str):
    try:
        path = spaces.get_file_path(space_id, file_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path)


# --------------------------------------------------------------------------- #
# Chats within a space
# --------------------------------------------------------------------------- #
class CreateChatRequest(BaseModel):
    title: str = ""


@app.get("/spaces/{space_id}/chats")
async def list_chats(space_id: str):
    try:
        await asyncio.to_thread(spaces.get_space, space_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Space not found")
    chats = await asyncio.to_thread(spaces.list_chats, space_id)
    return {"chats": chats}


@app.post("/spaces/{space_id}/chats")
async def create_chat(space_id: str, req: CreateChatRequest):
    try:
        return await asyncio.to_thread(spaces.create_chat, space_id, req.title)
    except KeyError:
        raise HTTPException(status_code=404, detail="Space not found")


@app.get("/spaces/{space_id}/chats/{chat_id}")
def get_chat(space_id: str, chat_id: str):
    try:
        chat = spaces.get_chat(space_id, chat_id)
        chat["context"] = context_for_messages(chat.get("messages", []))
        active = chat_stream.snapshot(space_id, chat_id)
        if active:
            chat["generating"] = True
            chat["partial_answer"] = active.get("partial") or ""
        else:
            chat["generating"] = False
        return chat
    except KeyError:
        raise HTTPException(status_code=404, detail="Chat not found")


@app.delete("/spaces/{space_id}/chats/{chat_id}")
async def delete_chat(space_id: str, chat_id: str):
    await asyncio.to_thread(spaces.delete_chat, space_id, chat_id)
    return {"status": "ok"}


class UpdateChatRequest(BaseModel):
    title: str


@app.patch("/spaces/{space_id}/chats/{chat_id}")
def update_chat(space_id: str, chat_id: str, req: UpdateChatRequest):
    try:
        return spaces.update_chat(space_id, chat_id, req.title)
    except KeyError:
        raise HTTPException(status_code=404, detail="Chat not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# --------------------------------------------------------------------------- #
# Prompt library (reusable system prompts, shared across spaces)
# --------------------------------------------------------------------------- #
class CreatePromptRequest(BaseModel):
    name: str = ""
    content: str = ""


class UpdatePromptRequest(BaseModel):
    name: str | None = None
    content: str | None = None


@app.get("/prompts")
def list_prompts():
    return {"prompts": prompts.list_prompts()}


@app.post("/prompts")
def create_prompt(req: CreatePromptRequest):
    return prompts.create_prompt(req.name, req.content)


@app.get("/prompts/{prompt_id}")
def get_prompt(prompt_id: str):
    try:
        return prompts.get_prompt(prompt_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Prompt not found")


@app.patch("/prompts/{prompt_id}")
def update_prompt(prompt_id: str, req: UpdatePromptRequest):
    try:
        return prompts.update_prompt(prompt_id, name=req.name, content=req.content)
    except KeyError:
        raise HTTPException(status_code=404, detail="Prompt not found")


@app.delete("/prompts/{prompt_id}")
def delete_prompt(prompt_id: str):
    prompts.delete_prompt(prompt_id)
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Chat (streaming + persistence)
# --------------------------------------------------------------------------- #
class ChatRequest(BaseModel):
    space_id: str
    chat_id: str
    query: str


@app.post("/chat")
async def chat_endpoint(req: ChatRequest, request: Request):
    # Validate up front so we can return a clean error before streaming starts.
    try:
        spaces.get_chat(req.space_id, req.chat_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Space or chat not found")

    async def stream():
        cancel = await chat_stream.begin(req.space_id, req.chat_id)
        await chat_stream.set_query(req.space_id, req.chat_id, req.query)
        answer_parts: list[str] = []
        sources: list = []
        completed = False
        try:
            spaces.append_message(req.space_id, req.chat_id, "user", req.query)
            async for ev in run_query(req.query, req.space_id, req.chat_id, cancel=cancel):
                if await request.is_disconnected():
                    cancel.set()
                    break
                if ev["type"] == "token":
                    answer_parts.append(ev["text"])
                    await chat_stream.set_partial(
                        req.space_id, req.chat_id, "".join(answer_parts),
                    )
                elif ev["type"] == "sources":
                    sources = ev["sources"]
                elif ev["type"] == "replace":
                    answer_parts = [ev["text"]]
                    await chat_stream.set_partial(req.space_id, req.chat_id, ev["text"])
                elif ev["type"] == "done":
                    completed = True
                yield _sse(ev)
            if completed and answer_parts and not cancel.is_set():
                spaces.append_message(
                    req.space_id, req.chat_id, "assistant", "".join(answer_parts), sources,
                )
        finally:
            await chat_stream.end(req.space_id, req.chat_id)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
