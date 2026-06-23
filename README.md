# Local Multimodal RAG

A fully local, multimodal Retrieval-Augmented Generation web app. Upload PDFs,
images, and videos (or whole folders) through the browser; they are embedded and
stored in a local Qdrant vector DB, and the original files are persisted on disk.
Then chat with your knowledge base — retrieval + reranking + grounded
generation, all on `localhost`, no external API calls.

Everything is organized into **Spaces** (projects): each space has its own files,
its own isolated vector search, its own saved chats, and its own editable system
prompt. There is also a reusable **prompt library** shared across spaces.

All three models are kept **warm in VRAM after first use** (no reload on later
queries). At startup only the **embedder** is preloaded in the background — loading
all three at once can OOM on 6 GB GPUs during the temporary load spike. The reranker
and generator load on the first chat and then stay resident:

| Role       | Model                  | HF ID                        | Default precision |
|------------|------------------------|------------------------------|-------------------|
| Embedding  | Qwen3-VL-Embedding-2B  | `Qwen/Qwen3-VL-Embedding-2B` | NF4 (4-bit)       |
| Reranker   | Qwen3-VL-Reranker-2B   | `Qwen/Qwen3-VL-Reranker-2B`  | NF4 (4-bit)       |
| Generator  | Qwen3-VL-2B-Instruct   | `Qwen/Qwen3-VL-2B-Instruct`  | NF4 (4-bit)       |

### Quantization (NF4 4-bit) — configurable per model

All three models are loaded **4-bit (NF4) quantized** via `bitsandbytes`
(`BitsAndBytesConfig(load_in_4bit=True)`). This is the lowest-VRAM option and is
what makes the whole pipeline fit comfortably on a 6 GB GPU.

Precision is controlled **per model** by a single dict at the top of
[`backend/model_manager.py`](backend/model_manager.py) — change any entry
independently:

```python
# backend/model_manager.py
PRECISION = {
    "embedder": "4bit",   # NF4 4-bit (bitsandbytes)
    "reranker": "4bit",
    "generator": "4bit",
}
```

Accepted values for each model:

| Value    | Meaning                                  | Requires                  |
|----------|------------------------------------------|---------------------------|
| `"4bit"` | NF4 4-bit weight quantization (default)  | NVIDIA GPU + bitsandbytes |
| `"8bit"` | Q8 / INT8 weight quantization            | NVIDIA GPU + bitsandbytes |
| `"bf16"` | bfloat16                                 | GPU (or CPU)              |
| `"fp16"` | float16                                  | GPU                       |
| `"fp32"` | float32                                  | CPU or GPU                |

Notes:

- `4bit` / `8bit` require an **NVIDIA GPU** and the `bitsandbytes` package. On CPU
  they automatically fall back to `float32`.
- If a quantized load fails at runtime (e.g. an unsupported GPU/driver), the
  loader logs a warning and **falls back to bf16/fp32** so the app keeps working.
- The code sets `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` to reduce CUDA
  fragmentation, which helps on small-VRAM cards.
- Mix and match freely — e.g. run the generator in `bf16` for max quality while
  keeping the embedder/reranker in `4bit` to save VRAM.

> Note on older GPUs: `8bit` (INT8) can hang during load on Turing cards
> (e.g. GTX 16xx / RTX 20xx), which lack INT8 tensor cores. `4bit` (NF4) is the
> safe, fast default and is what ships enabled.

## Project layout

```
RAG/
├── backend/
│   ├── main.py            # FastAPI app: spaces, files, chats, prompts, /chat SSE
│   ├── model_manager.py   # Resident model singleton + asyncio.Lock; per-model precision
│   ├── ingest.py          # Preprocess + chunk + embed + upsert (tagged space/file)
│   ├── query.py           # Embed query -> space-filtered search -> rerank -> generate
│   ├── qdrant_store.py    # Qdrant connection, payload indexes, filters, deletes
│   ├── spaces.py          # Disk persistence: spaces, files (media), and chats
│   ├── prompts.py         # Reusable system-prompt library (presets on disk)
│   └── requirements.txt
├── frontend/
│   └── app.html           # Single-file SPA (Spaces sidebar + Files/Chat/Instructions)
├── models/                # Downloaded weights (gitignored)
├── spaces/                # Per-space files, media, and chats (gitignored, auto-created)
├── prompts/               # Saved system-prompt presets (gitignored, auto-created)
├── qdrant_data/           # Qdrant volume (gitignored, auto-created)
├── run.bat / stop.bat     # One-click start / stop (Windows)
├── setup_env.py           # CUDA detection + matching PyTorch install
└── docker-compose.yml     # Qdrant service
```

### How data is stored

Each space lives under `spaces/<space_id>/`:

```
spaces/<space_id>/
├── space.json                  # {id, name, system_prompt, created_at, files[...]}
├── media/<file_id>__<name>     # the original uploaded files, kept on disk
└── chats/<chat_id>.json        # one conversation each (messages + sources)
```

Vectors live in Qdrant, and every point is tagged with `space_id` and `file_id`
(keyword-indexed). This gives **isolated search per space** and lets a delete
remove a file's (or a whole space's) vectors from Qdrant *and* its files from
disk together. Reusable system prompts are stored separately in `prompts/<id>.json`.

---

## How to run

> Activate your conda environment before running any `python`, `pip`, or
> `hf` commands (for example: `conda activate your-env-name`).

### 1. Install dependencies (one time) — automated

Run the setup script. It auto-detects your CUDA version (via `nvidia-smi`),
installs the matching CUDA build of PyTorch, then installs everything else:

```powershell
conda activate <your-env-name>
python setup_env.py
```

This is portable: a different machine/CUDA version gets the right wheel
automatically, and a machine with no NVIDIA GPU falls back to the CPU build. When
it finishes it prints something like `torch ... | cuda available: True`.

<details>
<summary>What if I want to do it manually instead?</summary>

`pip install torch` from PyPI gives a **CPU-only** build (`x.y.z+cpu`). To get
GPU support, install a CUDA wheel matching your driver (find the `cuXXX` at
https://pytorch.org/get-started/locally/), then the rest:

```powershell
pip uninstall -y torch torchvision torchaudio
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu124
pip install -r backend/requirements.txt
python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
```
</details>

Notes:

- The default **4-bit quantization needs `bitsandbytes` + an NVIDIA GPU** (already
  in `requirements.txt`; Windows is supported on `bitsandbytes>=0.43`).
- `flash_attention_2` is optional and skipped automatically if not installed.
- The code auto-selects the best dtype per GPU for the non-quantized fallback
  (bfloat16 on Ampere+, **float16** on older cards like Turing GTX 16xx / RTX 20xx).
- **No GPU?** It still runs on CPU (fp32) — slower but functional.

### 2. Download the model weights (one time, ~ a few GB each)

```powershell
conda activate <your-env-name>
hf download Qwen/Qwen3-VL-Embedding-2B --local-dir ./models/Qwen3-VL-Embedding-2B
hf download Qwen/Qwen3-VL-Reranker-2B  --local-dir ./models/Qwen3-VL-Reranker-2B
hf download Qwen/Qwen3-VL-2B-Instruct  --local-dir ./models/Qwen3-VL-2B-Instruct
```

> Uses the `hf` CLI (from `huggingface-hub`, already in `requirements.txt`). The
> old `huggingface-cli` command is deprecated — `hf download ...` replaces it.

### 3. One-click launch (Windows, recommended)

Once dependencies and weights are in place, just run the launcher (or double-click
it). It starts Qdrant, the backend, and the frontend, waits for the backend to be
healthy, then opens the app:

```powershell
run.bat
```

To stop everything:

```powershell
stop.bat
```

> `run.bat` uses the project venv's Python directly (`venv\Scripts\python.exe`)
> and binds the backend to `127.0.0.1:8000` and the frontend to
> `127.0.0.1:3000`. It runs uvicorn **without `--reload`** on purpose — on Windows
> the file watcher tries to scan the whole project (`venv/`, `models/`,
> `.pip-cache/`) and can make the server unresponsive.

<details>
<summary>Prefer to start the three services manually?</summary>

**Terminal 1 — Qdrant** (Docker Desktop must be running first):

```powershell
docker compose up -d
```

**Terminal 2 — Backend:**

```powershell
conda activate <your-env-name>
uvicorn main:app --app-dir backend --host 127.0.0.1 --port 8000
```

> Model paths resolve relative to the project root automatically, so the models in
> `RAG/models/` are found regardless of the launch directory. To store models
> elsewhere, set the `MODELS_DIR` environment variable. You can likewise override
> `SPACES_DIR` and `PROMPTS_DIR`.

**Terminal 3 — Frontend:**

```powershell
conda activate <your-env-name>
python -m http.server 3000 --bind 127.0.0.1 --directory frontend
```

Then open **http://127.0.0.1:3000/app.html**.
</details>

---

## Using the app

1. **Create a space** — click `+` next to "Spaces" in the left sidebar and name it.
   Everything (files, search, chats, system prompt) is scoped to the active space.
2. **Files tab** — add content two ways:
   - **Browse files** (or drag & drop) to upload individual `.pdf` / image / video files.
   - **Select a folder** to ingest every supported file inside it in one go
     (unsupported files are skipped automatically).
   Each uploaded file is stored on disk and shows its chunk count. Use the `✕` on a
   file row to remove it — this deletes both its vectors and its stored copy.
3. **Instructions tab** — write a **system prompt** for this space (e.g. tone,
   persona, or a required output format). It is applied to every question in the
   space, on top of the built-in grounding rules. You can:
   - **Save to space** to apply it.
   - **Save as preset** to add it to the shared **prompt library**.
   - **Load** any library preset into the editor to reuse it in another space.
4. **Chat tab** — start a new chat (chats are saved per space and persist across
   restarts). Answers stream in token-by-token with a collapsible **Sources**
   panel (thumbnails + filename + page) below each response. Search only returns
   results from the current space.

Supported file types: `.pdf`, `.png`, `.jpg`, `.jpeg`, `.webp`, `.bmp`, `.tiff`,
`.mp4`, `.mov`, `.avi`, `.mkv`.

## API endpoints (reference)

| Method   | Path                                   | Purpose                                  |
|----------|----------------------------------------|------------------------------------------|
| `GET`    | `/health`                              | Liveness check                           |
| `GET`    | `/models/status`                       | Model warmup progress (`{ready, count, total, loaded}`) |
| `GET`    | `/spaces`                              | List spaces                              |
| `POST`   | `/spaces`                              | Create a space                           |
| `GET`    | `/spaces/{id}`                         | Get a space (incl. files + system prompt)|
| `PATCH`  | `/spaces/{id}`                         | Update name and/or `system_prompt`       |
| `DELETE` | `/spaces/{id}`                         | Delete a space (vectors + disk)          |
| `POST`   | `/spaces/{id}/files`                   | Upload one or more files (multipart)     |
| `DELETE` | `/spaces/{id}/files/{file_id}`         | Remove a file (vectors + disk)           |
| `GET`    | `/spaces/{id}/files/{file_id}/raw`     | Serve the original stored file           |
| `GET`    | `/spaces/{id}/chats`                   | List chats in a space                    |
| `POST`   | `/spaces/{id}/chats`                   | Create a chat                            |
| `GET`    | `/spaces/{id}/chats/{chat_id}`         | Get a chat (messages + sources)          |
| `DELETE` | `/spaces/{id}/chats/{chat_id}`         | Delete a chat                            |
| `GET`    | `/prompts`                             | List saved prompt presets                |
| `POST`   | `/prompts`                             | Create a preset                          |
| `GET`    | `/prompts/{id}`                        | Get a preset                             |
| `PATCH`  | `/prompts/{id}`                        | Update a preset                          |
| `DELETE` | `/prompts/{id}`                        | Delete a preset                          |
| `POST`   | `/chat`                                | Stream an answer (SSE) for `{space_id, chat_id, query}` and persist messages |

## Expected VRAM usage

All three models stay resident, so VRAM is the **sum** of the three (the CUDA
context is counted once, not per model). With the default **NF4 4-bit**
quantization each 2B model's weights are roughly a quarter of their bf16 size.
Figures are approximate.

| Component                                   | VRAM (NF4 / 4-bit) |
|---------------------------------------------|--------------------|
| CUDA context + kernels (once)               | ~0.7 GB            |
| Embedder weights                            | ~1.3 GB            |
| Reranker weights                            | ~1.3 GB            |
| Generator weights                           | ~1.4 GB            |
| **Steady state (all resident, idle)**       | **~4.7 GB**        |
| + generation activations / KV cache         | +0.3–0.7 GB        |
| **Peak during generation**                  | **~5.0–5.4 GB**    |

This fits a 6 GB GPU with ~0.6–1.0 GB of headroom. The tightest moment is
ingesting PDFs that contain images (activation spikes stack on top of the
resident floor); to keep that safe, ingest uses a small embed batch size (4) and
downscales large images to a max dimension of 1024 px before embedding
([`backend/ingest.py`](backend/ingest.py)).

> If you run other GPU apps alongside this and need the VRAM back, switch one or
> more models to a smaller footprint, or change the manager back to evicting
> idle models (see `ModelManager` in
> [`backend/model_manager.py`](backend/model_manager.py)).

## Notes & tuning

- **Precision / quantization**: per-model `PRECISION` dict in
  [`backend/model_manager.py`](backend/model_manager.py) (default `4bit` for all
  three). Supports `4bit`/`8bit`/`bf16`/`fp16`/`fp32`; quantized loads fall back to
  bf16/fp32 if unavailable.
- **System prompt**: per-space instructions are appended to the base grounding
  prompt in [`backend/query.py`](backend/query.py); presets live in `prompts/`.
- **Chunking**: 256-word chunks with 64-word overlap
  ([`backend/ingest.py`](backend/ingest.py)).
- **Retrieval**: top-20 vector search (filtered to the active space) → rerank →
  top-5 to the generator ([`backend/query.py`](backend/query.py)).
- **Empty-space fast path**: if a space has no vectors yet, chat answers instantly
  without loading any model.
- **Embedding dim**: 2048 (must match the Qdrant collection in `qdrant_store.py`).
- **Model warmup**: at startup the **embedder** preloads in the background
  (`PRELOAD_MODELS=embedder`, default). Set `PRELOAD_MODELS=all` to attempt all
  three (may OOM on 6 GB). Progress is at `GET /models/status`; the sidebar shows
  `warming embedder...` then `1/3 ready (more on first chat)`. After the first full
  chat, all three are warm with **no per-query reload**. If a 4-bit load fails,
  fp16 fallback is **disabled on GPU** (one fp16 model alone is ~4 GB and caused
  process crashes when loading a second). Check the backend terminal for
  `[models]` lines and VRAM warnings.
- **Reset everything**: `stop.bat`, then delete `./qdrant_data` (vectors) and
  `./spaces` (files + chats). Delete `./prompts` to clear the prompt library.
