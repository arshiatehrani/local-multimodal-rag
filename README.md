# Local Multimodal RAG

A fully local, multimodal Retrieval-Augmented Generation web app. Upload PDFs,
images, and videos through the browser; they are embedded and stored in a local
Qdrant vector DB. Then chat with your knowledge base — retrieval + reranking +
grounded generation, all on `localhost`, no external API calls.

All three models are **hot-swapped** so only one is ever resident in VRAM:

| Role       | Model                      | HF ID                          |
|------------|----------------------------|--------------------------------|
| Embedding  | Qwen3-VL-Embedding-2B      | `Qwen/Qwen3-VL-Embedding-2B`   |
| Reranker   | Qwen3-VL-Reranker-2B       | `Qwen/Qwen3-VL-Reranker-2B`    |
| Generator  | Qwen3-VL-2B-Instruct       | `Qwen/Qwen3-VL-2B-Instruct`    |

## Project layout

```
RAG/
├── backend/
│   ├── main.py            # FastAPI app, CORS, routes
│   ├── model_manager.py   # Hot-swap singleton + asyncio.Lock
│   ├── ingest.py          # Preprocess + chunk + embed + upsert
│   ├── query.py           # Embed query + search + rerank + generate (SSE)
│   ├── qdrant_store.py    # Qdrant connection + collection setup
│   └── requirements.txt
├── frontend/
│   └── app.html           # Single-file SPA (Ingest + Chat tabs)
├── models/                # Downloaded weights (gitignored)
├── qdrant_data/           # Qdrant volume (gitignored, auto-created)
└── docker-compose.yml     # Qdrant service
```

---

## How to run

> All `python` / `pip` / `huggingface-cli` commands assume your conda env `p` is
> active: run `conda activate p` first in each terminal.

### 1. Install dependencies (one time)

```powershell
conda activate p
pip install -r backend/requirements.txt
```

> For GPU, make sure your `torch` build matches your CUDA version
> (see https://pytorch.org/get-started/locally/). `flash_attention_2` is optional
> and is skipped automatically if it isn't installed.

### 2. Download the model weights (one time, ~ a few GB each)

```powershell
conda activate p
huggingface-cli download Qwen/Qwen3-VL-Embedding-2B --local-dir ./models/Qwen3-VL-Embedding-2B
huggingface-cli download Qwen/Qwen3-VL-Reranker-2B  --local-dir ./models/Qwen3-VL-Reranker-2B
huggingface-cli download Qwen/Qwen3-VL-2B-Instruct  --local-dir ./models/Qwen3-VL-2B-Instruct
```

### 3. Start Qdrant (Terminal 1)

```powershell
docker compose up -d
```

Qdrant will be available at `http://localhost:6333` with data persisted in
`./qdrant_data`.

### 4. Start the backend (Terminal 2)

```powershell
conda activate p
cd backend
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

> The backend resolves `./models/...` relative to the directory it's launched
> from, so start `uvicorn` from inside `backend/` (or use an absolute path).

### 5. Open the frontend (Terminal 3)

Serve the single-file SPA and open it in the browser:

```powershell
conda activate p
python -m http.server 3000 --directory frontend
```

Then go to **http://localhost:3000/app.html**.

(You can also just double-click `frontend/app.html` — it talks to the backend at
`http://localhost:8000`.)

---

## Using the app

1. **Ingest tab** — drag & drop or click to upload `.pdf`, image, or video files.
   Each file shows `processing... → ✓ stored N chunks`.
2. **Chat tab** — ask a question. The answer streams in token-by-token, with a
   collapsible **Sources** panel (thumbnails + filename + page) below it.

## Notes & tuning

- **VRAM**: peak ≈ 3.5 GB (one model at a time). Idle ≈ 0.5 GB CUDA context.
- **Chunking**: 256-word chunks with 64-word overlap (`backend/ingest.py`).
- **Retrieval**: top-20 vector search → rerank → top-5 to the generator
  (`backend/query.py`).
- **Embedding dim**: 2048 (must match the Qdrant collection in `qdrant_store.py`).
- **Swap latency**: embedder/reranker load in ~1–2 s, generator ~3–5 s from an
  NVMe SSD — normal for hot-swapping on a single GPU.
- **Reset the DB**: `docker compose down` then delete `./qdrant_data`.
