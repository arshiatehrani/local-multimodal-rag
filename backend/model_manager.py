"""Resident model manager with per-model quantization (Option A).

All three models (embedder / reranker / generator), quantized to NF4 4-bit, are
kept resident in VRAM at the same time (~5 GB total on a 6 GB card). Each is
loaded at most once -- eagerly via ``preload_all()`` at startup, or lazily on
first use -- which removes the per-query load/evict latency of the old hot-swap
design. An asyncio.Lock serialises GPU work so two heavy operations never spike
VRAM concurrently. On error a single model is evicted and reloaded on next use.

Quantization is configured PER MODEL via the ``PRECISION`` dict below. Change a
single entry to switch how that one model is loaded.
"""

import os
import gc
import time
import asyncio
import warnings
import traceback

# Reduce CUDA fragmentation OOMs on small GPUs. Must be set before the CUDA
# caching allocator initialises (i.e. before the first GPU allocation).
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

# NOTE: torch / sentence_transformers / transformers are imported LAZILY inside
# the loader functions (not at module top-level). Importing them eagerly adds
# 30-60s to server startup; deferring it lets the API come online in ~1-2s and
# the first model request pays the import cost (already in a worker thread).

# Resolve model paths relative to the project root (the parent of backend/), so
# the app works no matter which directory uvicorn is launched from. Override the
# location with the MODELS_DIR environment variable if needed.
_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_BACKEND_DIR)
MODELS_DIR = os.environ.get("MODELS_DIR", os.path.join(_PROJECT_ROOT, "models"))

EMBEDDER_PATH = os.path.join(MODELS_DIR, "Qwen3-VL-Embedding-2B")
RERANKER_PATH = os.path.join(MODELS_DIR, "Qwen3-VL-Reranker-2B")
GENERATOR_PATH = os.path.join(MODELS_DIR, "Qwen3-VL-2B-Instruct")

def _device() -> str:
    """Resolve the compute device lazily (imports torch on first call)."""
    import torch
    return "cuda" if torch.cuda.is_available() else "cpu"

# ---------------------------------------------------------------------------
# PER-MODEL PRECISION  --  edit these to control quantization independently.
#
# Accepted values:
#   "8bit"  -> Q8 / INT8 weight quantization (bitsandbytes)   [default]
#   "4bit"  -> NF4 4-bit weight quantization (bitsandbytes)
#   "bf16"  -> bfloat16
#   "fp16"  -> float16
#   "fp32"  -> float32
#
# "8bit"/"4bit" require an NVIDIA GPU + the `bitsandbytes` package. On CPU they
# fall back to float32 automatically.
# ---------------------------------------------------------------------------
# NOTE on small GPUs (e.g. 6 GB GTX 1660 Ti): three 2B multimodal models do NOT
# fit in fp16 (~4.2 GB weights each + CUDA overhead -> OOM during inference).
# 4-bit (NF4) shrinks each model to ~1.3 GB, which fits comfortably and leaves
# room for activations. If a 4-bit load fails on your card, the loader falls
# back to fp16 automatically (see _load_sentence_model / _load_generator).
PRECISION = {
    "embedder": "4bit",
    "reranker": "4bit",
    "generator": "4bit",
}

# Map precision aliases -> torch dtype attribute names (resolved lazily so we
# don't import torch at module load time).
_DTYPE_NAMES = {
    "bf16": "bfloat16", "bfloat16": "bfloat16",
    "fp16": "float16", "float16": "float16", "half": "float16",
    "fp32": "float32", "float32": "float32", "full": "float32",
}
_EIGHT_BIT = {"8bit", "int8", "q8"}
_FOUR_BIT = {"4bit", "int4", "nf4"}


def _resolve_dtype(name: str):
    import torch
    return getattr(torch, _DTYPE_NAMES[name.lower()])


def _best_float_dtype():
    """Best 16-bit dtype for this device.

    Ampere+ GPUs support bfloat16; older cards (e.g. Turing GTX 16xx / RTX 20xx)
    do not, so we use float16 there. CPU stays float32.
    """
    import torch
    if _device() != "cuda":
        return torch.float32
    try:
        if torch.cuda.is_bf16_supported():
            return torch.bfloat16
    except Exception:
        pass
    return torch.float16


def _is_quant(precision: str) -> bool:
    return precision.lower() in (_EIGHT_BIT | _FOUR_BIT)


def _build_load_kwargs(precision: str) -> dict:
    """Translate a precision label into transformers ``from_pretrained`` kwargs."""
    import torch
    p = precision.lower()

    if p == "auto":
        return {"torch_dtype": _best_float_dtype()}

    if _is_quant(p):
        if _device() != "cuda":
            warnings.warn(
                f"Precision '{precision}' needs a CUDA GPU; using float32 on CPU."
            )
            return {"torch_dtype": torch.float32}
        try:
            from transformers import BitsAndBytesConfig
        except ImportError as e:  # pragma: no cover - depends on env
            raise ImportError(
                "8-bit/4-bit quantization requires `bitsandbytes`. "
                "Install it with `pip install bitsandbytes`."
            ) from e

        if p in _EIGHT_BIT:
            qcfg = BitsAndBytesConfig(load_in_8bit=True)
        else:
            qcfg = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_use_double_quant=True,
                # bf16 on Ampere+, fp16 on older GPUs (Turing GTX 16xx etc.).
                bnb_4bit_compute_dtype=_best_float_dtype(),
            )
        # device_map lets accelerate place the quantized weights on the GPU.
        return {"quantization_config": qcfg, "device_map": "auto"}

    if p in _DTYPE_NAMES:
        return {"torch_dtype": _resolve_dtype(p)}

    warnings.warn(f"Unknown precision '{precision}'; defaulting to auto.")
    return {"torch_dtype": _best_float_dtype()}


def _load_generator_cls():
    """Import the correct generation class for Qwen3-VL.

    transformers>=4.57 ships ``Qwen3VLForConditionalGeneration``. Fall back to
    the auto class on older builds.
    """
    try:
        from transformers import Qwen3VLForConditionalGeneration
        return Qwen3VLForConditionalGeneration
    except ImportError:
        from transformers import AutoModelForImageTextToText
        return AutoModelForImageTextToText


class ModelManager:
    """Singleton that keeps ALL models resident in VRAM (Option A).

    The three 2B models, quantized to NF4 4-bit, fit together in ~5 GB, so we
    load each one once and keep it warm for the lifetime of the process. This
    eliminates the per-query load/evict overhead of the old hot-swap design.

    A single ``asyncio.Lock`` still serialises GPU work so two heavy operations
    (e.g. an ingest embed pass and a chat generation) never overlap and spike
    VRAM at the same time. Loads happen at most once per model, under the lock.
    """

    def __init__(self):
        self._models: dict = {}
        self._errors: dict = {}
        self._lock = asyncio.Lock()
        self._loaders = {
            "embedder": self._load_embedder,
            "reranker": self._load_reranker,
            "generator": self._load_generator,
        }

    def status(self) -> dict:
        """Report which models are currently resident (for UI warmup display)."""
        names = ("embedder", "reranker", "generator")
        loaded = {n: (n in self._models) for n in names}
        return {
            "loaded": loaded,
            "ready": all(loaded.values()),
            "count": sum(loaded.values()),
            "total": len(names),
            "errors": dict(self._errors),
        }

    def _evict_one(self, name: str):
        """Drop a single model and free its VRAM. Caller must hold the lock.

        Used only as a recovery path (e.g. after a CUDA OOM) so the next request
        reloads it from a clean slate; the other resident models stay warm.
        """
        if name in self._models:
            del self._models[name]
            gc.collect()
            try:
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass

    async def preload_all(self):
        """Load every model once, sequentially, under the lock.

        Kicked off as a background task at startup so the API is reachable
        immediately while the GPU warms up. Failures are logged, not fatal.
        """
        print("[models] warmup started", flush=True)
        for name in ("embedder", "reranker", "generator"):
            async with self._lock:
                if name in self._models:
                    continue
                print(f"[models] loading {name} ...", flush=True)
                t0 = time.time()
                try:
                    self._models[name] = await asyncio.to_thread(self._loaders[name])
                    self._errors.pop(name, None)
                    print(f"[models] loaded {name} in {time.time() - t0:.1f}s", flush=True)
                except Exception as e:  # pragma: no cover - depends on env
                    self._errors[name] = repr(e)
                    print(f"[models] FAILED to load {name}: {e!r}", flush=True)
                    traceback.print_exc()
        print(f"[models] warmup finished: {self.status()['count']}/3 loaded", flush=True)

    # --- public async context managers (use with `async with await manager.x()`) ---

    async def embedder(self):
        return _ModelContext(self, "embedder")

    async def reranker(self):
        return _ModelContext(self, "reranker")

    async def generator(self):
        return _ModelContext(self, "generator")

    # --- private loaders ---

    @staticmethod
    def _load_sentence_model(cls, path, role):
        """Load a SentenceTransformer / CrossEncoder honouring PRECISION[role]."""
        precision = PRECISION.get(role, "bf16")
        load_kwargs = _build_load_kwargs(precision)
        quantized = "quantization_config" in load_kwargs
        # When quantized, accelerate places weights via device_map; passing an
        # explicit device on top would double-place, so leave it to the library.
        device = None if quantized else _device()
        try:
            return cls(
                path,
                device=device,
                trust_remote_code=True,
                model_kwargs=load_kwargs,
            )
        except Exception as e:  # pragma: no cover - depends on env
            if quantized:
                warnings.warn(
                    f"{role}: '{precision}' load failed ({e}); falling back to fp16/fp32."
                )
                return cls(
                    path,
                    device=_device(),
                    trust_remote_code=True,
                    model_kwargs=_build_load_kwargs("auto"),
                )
            raise

    def _load_embedder(self):
        from sentence_transformers import SentenceTransformer
        return self._load_sentence_model(SentenceTransformer, EMBEDDER_PATH, "embedder")

    def _load_reranker(self):
        from sentence_transformers import CrossEncoder
        return self._load_sentence_model(CrossEncoder, RERANKER_PATH, "reranker")

    def _load_generator(self):
        from transformers import AutoProcessor

        processor = AutoProcessor.from_pretrained(GENERATOR_PATH, trust_remote_code=True)
        model_cls = _load_generator_cls()

        precision = PRECISION.get("generator", "bf16")
        load_kwargs = dict(_build_load_kwargs(precision))
        load_kwargs["trust_remote_code"] = True
        # bf16/fp16/fp32 paths need an explicit device_map; the quant path
        # already supplies device_map="auto".
        load_kwargs.setdefault("device_map", _device())

        # Try flash-attention-2 first (faster), then without it. flash_attention_2
        # is optional and frequently unavailable on Windows.
        model = None
        for extra in ({"attn_implementation": "flash_attention_2"}, {}):
            try:
                model = model_cls.from_pretrained(GENERATOR_PATH, **load_kwargs, **extra)
                break
            except Exception:
                continue

        if model is None:  # pragma: no cover - depends on env
            warnings.warn(
                f"generator: '{precision}' load failed; falling back to fp16/fp32."
            )
            fb = dict(_build_load_kwargs("auto"))
            fb["trust_remote_code"] = True
            fb.setdefault("device_map", _device())
            model = model_cls.from_pretrained(GENERATOR_PATH, **fb)

        model.eval()
        return {"model": model, "processor": processor}


class _ModelContext:
    """Async context manager: acquire the GPU lock, ensure the model is loaded
    (once), yield it, then release the lock.

    Models are kept resident, so the load only happens the first time (or after a
    recovery eviction). The lock is held for the duration of the operation so GPU
    work is serialised across requests.
    """

    def __init__(self, manager: "ModelManager", name: str):
        self._manager = manager
        self._name = name

    async def __aenter__(self):
        await self._manager._lock.acquire()
        try:
            if self._name not in self._manager._models:
                # Run the (slow, blocking) load in a worker thread so the asyncio
                # event loop stays responsive (e.g. /health keeps answering and
                # client connections don't drop while a model loads).
                loader = self._manager._loaders[self._name]
                self._manager._models[self._name] = await asyncio.to_thread(loader)
        except Exception:
            self._manager._lock.release()
            raise
        return self._manager._models[self._name]

    async def __aexit__(self, exc_type, exc, tb):
        # On error (e.g. CUDA OOM) this model may be in a bad state / VRAM may be
        # fragmented. Evict just this one so the NEXT request reloads it cleanly,
        # while the other resident models stay warm.
        try:
            if exc_type is not None:
                self._manager._evict_one(self._name)
        finally:
            self._manager._lock.release()
        return False


# Global singleton
manager = ModelManager()
