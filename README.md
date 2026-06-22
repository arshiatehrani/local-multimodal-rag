# Local Multimodal RAG

A fully local, multimodal Retrieval-Augmented Generation web app. Upload PDFs,
images, and videos through the browser; they are embedded and stored in a local
Qdrant vector DB. Then chat with your knowledge base — retrieval + reranking +
grounded generation, all on `localhost`, no external API calls.

All three models are **hot-swapped** so only one is ever resident in VRAM:

| Role       | Model                  | HF ID                        | Default precision |
|------------|------------------------|------------------------------|-------------------|
| Embedding  | Qwen3-VL-Embedding-2B  | `Qwen/Qwen3-VL-Embedding-2B` | Q8 (INT8)         |
| Reranker   | Qwen3-VL-Reranker-2B   | `Qwen/Qwen3-VL-Reranker-2B`  | Q8 (INT8)         |
| Generator  | Qwen3-VL-2B-Instruct   | `Qwen/Qwen3-VL-2B-Instruct`  | Q8 (INT8)         |

### Quantization (Q8) — configurable per model

All three models are loaded **8-bit (Q8 / INT8) quantized** via `bitsandbytes`
(`BitsAndBytesConfig(load_in_8bit=True)`). This roughly halves VRAM vs bfloat16.

Precision is controlled **per model** by a single dict at the top of
`backend/model_manager.py` — change any entry independently:

```python
# backend/model_manager.py
PRECISION = {
    "embedder": "8bit",   # Q8 / INT8 (bitsandbytes)
    "reranker": "8bit",
    "generator": "8bit",
}
```

Accepted values for each model:

| Value    | Meaning                                  | Requires            |
|----------|------------------------------------------|---------------------|
| `"8bit"` | Q8 / INT8 weight quantization (default)  | NVIDIA GPU + bitsandbytes |
| `"4bit"` | NF4 4-bit weight quantization            | NVIDIA GPU + bitsandbytes |
| `"bf16"` | bfloat16                                 | GPU (or CPU)        |
| `"fp16"` | float16                                  | GPU                 |
| `"fp32"` | float32                                  | CPU or GPU          |

Notes:

- `8bit` / `4bit` require an **NVIDIA GPU** and the `bitsandbytes` package. On CPU
  they automatically fall back to `float32`.
- If a quantized load fails at runtime (e.g. an unsupported GPU/driver), the
  loader logs a warning and **falls back to bf16/fp32** so the app keeps working.
- Mix and match freely — e.g. run the generator in `bf16` for max quality while
  keeping the embedder/reranker in `8bit` to save VRAM.

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

> Activate your conda environment before running any `python`, `pip`, or
> `hf` commands (for example: `conda activate your-env-name`).

### 1. Install dependencies (one time)

```powershell
conda activate <your-env-name>
pip install -r backend/requirements.txt
```

#### GPU (CUDA) PyTorch — required for Q8 / GPU

`pip install torch` usually installs a **CPU-only** build (`x.y.z+cpu`), which
disables CUDA and forces fp32 on CPU. Verify what you have:

```powershell
python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
```

If it prints `...+cpu` or `False`, install a CUDA build (pick the cuXXX matching
your driver from https://pytorch.org/get-started/locally/ — check `nvidia-smi`):

```powershell
pip uninstall -y torch torchvision torchaudio
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121
```

Re-run the verify command; you want `True`. Notes:

- The default **Q8 quantization needs `bitsandbytes` + an NVIDIA GPU** (already in
  `requirements.txt`; Windows is supported on `bitsandbytes>=0.43`).
- `flash_attention_2` is optional and skipped automatically if not installed.
- **No GPU?** The app still runs on CPU: set the models to `bf16`/`fp32` in the
  `PRECISION` dict in `backend/model_manager.py` (or just rely on the automatic
  CPU fallback — it will be slow but functional).

### 2. Download the model weights (one time, ~ a few GB each)

```powershell
conda activate <your-env-name>
hf download Qwen/Qwen3-VL-Embedding-2B --local-dir ./models/Qwen3-VL-Embedding-2B
hf download Qwen/Qwen3-VL-Reranker-2B  --local-dir ./models/Qwen3-VL-Reranker-2B
hf download Qwen/Qwen3-VL-2B-Instruct  --local-dir ./models/Qwen3-VL-2B-Instruct
```

> Uses the `hf` CLI (from `huggingface-hub`, already in `requirements.txt`). The
> old `huggingface-cli` command is deprecated — `hf download ...` replaces it.

### 3. Start Qdrant (Terminal 1)

> **Docker Desktop must be running first.** Launch it and wait until the tray
> icon says "Docker Desktop is running" (run `docker version` and check for a
> `Server` section). Otherwise you'll get
> `failed to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine`.

```powershell
docker compose up -d
```

Qdrant will be available at `http://localhost:6333` with data persisted in
`./qdrant_data`.

### 4. Start the backend (Terminal 2)

```powershell
conda activate <your-env-name>
cd backend
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

> Run this from inside `backend/` (the modules import each other as top-level
> names). Model paths are resolved automatically relative to the project root, so
> the models in `RAG/models/` are found regardless of the launch directory. To
> store models elsewhere, set the `MODELS_DIR` environment variable.

### 5. Open the frontend (Terminal 3)

Serve the single-file SPA and open it in the browser:

```powershell
conda activate <your-env-name>
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

## Expected VRAM usage

Because of hot-swapping, only one model is resident at a time, so peak VRAM is
bounded by the largest single model (the generator). With the default **Q8**
quantization, each 2B model is roughly half its bf16 size.

| State        | Active model | VRAM (Q8 / INT8) | VRAM (bf16)      |
|--------------|--------------|------------------|------------------|
| Idle         | None         | ~0.5 GB (CUDA context) | ~0.5 GB    |
| Ingesting    | Embedder     | ~1.5 GB          | ~2.5 GB          |
| Query step 1 | Embedder     | ~1.5 GB          | ~2.5 GB          |
| Query step 2 | Reranker     | ~1.5 GB          | ~2.5 GB          |
| Query step 3 | Generator    | ~2.0 GB          | ~3.2 GB          |
| **Peak**     | Any one model| **~2.5 GB max**  | **~3.5 GB max**  |

## Notes & tuning

- **Precision / quantization**: per-model `PRECISION` dict in
  `backend/model_manager.py` (default `8bit` for all three). Supports
  `8bit`/`4bit`/`bf16`/`fp16`/`fp32`; quantized loads fall back to bf16/fp32 if
  unavailable.
- **Chunking**: 256-word chunks with 64-word overlap (`backend/ingest.py`).
- **Retrieval**: top-20 vector search → rerank → top-5 to the generator
  (`backend/query.py`).
- **Embedding dim**: 2048 (must match the Qdrant collection in `qdrant_store.py`).
- **Swap latency**: embedder/reranker load in ~1–2 s, generator ~3–5 s from an
  NVMe SSD — normal for hot-swapping on a single GPU.
- **Reset the DB**: `docker compose down` then delete `./qdrant_data`.
