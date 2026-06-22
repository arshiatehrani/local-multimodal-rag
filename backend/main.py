"""FastAPI application: CORS, ingest endpoint, and SSE chat endpoint."""

from contextlib import asynccontextmanager

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from qdrant_store import ensure_collection
from ingest import ingest_file
from query import run_query


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_collection()
    yield


app = FastAPI(title="Multimodal RAG API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/ingest")
async def ingest_endpoint(file: UploadFile = File(...)):
    contents = await file.read()
    try:
        n = await ingest_file(contents, file.filename)
        return {"status": "ok", "chunks_stored": n, "filename": file.filename}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


class ChatRequest(BaseModel):
    query: str


@app.post("/chat")
async def chat_endpoint(req: ChatRequest):
    return StreamingResponse(
        run_query(req.query),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/health")
def health():
    return {"status": "ok"}
