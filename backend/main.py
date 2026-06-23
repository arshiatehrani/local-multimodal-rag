"""FastAPI application: spaces, files, chats, and the SSE chat endpoint.

A "Space" groups uploaded files (persisted on disk + vectors in Qdrant) with
saved chats. Search and chat are always scoped to a single space.
"""

import json
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel

import spaces
import prompts
from qdrant_store import ensure_collection, delete_by_file, delete_by_space
from ingest import ingest_file, is_supported
from query import run_query
from model_manager import manager


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
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


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
def create_space(req: CreateSpaceRequest):
    return spaces.create_space(req.name)


@app.get("/spaces")
def list_spaces():
    return {"spaces": spaces.list_spaces()}


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
def delete_space(space_id: str):
    try:
        spaces.get_space(space_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Space not found")
    delete_by_space(space_id)
    spaces.delete_space(space_id)
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

    results = []
    for file in files:
        name = file.filename or "file"
        if not is_supported(name):
            results.append({"filename": name, "status": "skipped",
                            "error": "unsupported file type"})
            continue
        try:
            contents = await file.read()
            record = spaces.store_file_bytes(space_id, contents, name)
            n = await ingest_file(contents, name, space_id, record["file_id"])
            spaces.register_file(space_id, record, n)
            results.append({
                "filename": name,
                "status": "ok",
                "file_id": record["file_id"],
                "chunks_stored": n,
            })
        except Exception as e:  # noqa: BLE001 - surface any ingest error per file
            results.append({"filename": name, "status": "error", "error": str(e)})
    return {"results": results}


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
def list_chats(space_id: str):
    try:
        spaces.get_space(space_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Space not found")
    return {"chats": spaces.list_chats(space_id)}


@app.post("/spaces/{space_id}/chats")
def create_chat(space_id: str, req: CreateChatRequest):
    try:
        return spaces.create_chat(space_id, req.title)
    except KeyError:
        raise HTTPException(status_code=404, detail="Space not found")


@app.get("/spaces/{space_id}/chats/{chat_id}")
def get_chat(space_id: str, chat_id: str):
    try:
        return spaces.get_chat(space_id, chat_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Chat not found")


@app.delete("/spaces/{space_id}/chats/{chat_id}")
def delete_chat(space_id: str, chat_id: str):
    spaces.delete_chat(space_id, chat_id)
    return {"status": "ok"}


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
async def chat_endpoint(req: ChatRequest):
    # Validate up front so we can return a clean error before streaming starts.
    try:
        spaces.get_chat(req.space_id, req.chat_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Space or chat not found")

    async def stream():
        spaces.append_message(req.space_id, req.chat_id, "user", req.query)
        answer_parts, sources = [], []
        async for ev in run_query(req.query, req.space_id):
            if ev["type"] == "token":
                answer_parts.append(ev["text"])
            elif ev["type"] == "sources":
                sources = ev["sources"]
            yield _sse(ev)
        spaces.append_message(
            req.space_id, req.chat_id, "assistant", "".join(answer_parts), sources
        )

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
