"""Hot-swapping model manager with per-model quantization.

Ensures only ONE model (embedder / reranker / generator) is resident in VRAM at
any given time. Models are loaded on demand and the previously loaded model is
evicted (del -> gc.collect -> torch.cuda.empty_cache) before a different one is
loaded. An asyncio.Lock serialises swaps so concurrent requests cannot corrupt
the VRAM state.

Quantization is configured PER MODEL via the ``PRECISION`` dict below. Change a
single entry to switch how that one model is loaded.
"""

import os
import gc
import asyncio
import warnings

import torch
from sentence_transformers import SentenceTransformer, CrossEncoder

# Resolve model paths relative to the project root (the parent of backend/), so
# the app works no matter which directory uvicorn is launched from. Override the
# location with the MODELS_DIR environment variable if needed.
_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_BACKEND_DIR)
MODELS_DIR = os.environ.get("MODELS_DIR", os.path.join(_PROJECT_ROOT, "models"))

EMBEDDER_PATH = os.path.join(MODELS_DIR, "Qwen3-VL-Embedding-2B")
RERANKER_PATH = os.path.join(MODELS_DIR, "Qwen3-VL-Reranker-2B")
GENERATOR_PATH = os.path.join(MODELS_DIR, "Qwen3-VL-2B-Instruct")

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

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
PRECISION = {
    "embedder": "8bit",
    "reranker": "8bit",
    "generator": "8bit",
}

_DTYPE_ALIASES = {
    "bf16": torch.bfloat16, "bfloat16": torch.bfloat16,
    "fp16": torch.float16, "float16": torch.float16, "half": torch.float16,
    "fp32": torch.float32, "float32": torch.float32, "full": torch.float32,
}
_EIGHT_BIT = {"8bit", "int8", "q8"}
_FOUR_BIT = {"4bit", "int4", "nf4"}


def _best_float_dtype():
    """Best 16-bit dtype for this device.

    Ampere+ GPUs support bfloat16; older cards (e.g. Turing GTX 16xx / RTX 20xx)
    do not, so we use float16 there. CPU stays float32.
    """
    if DEVICE != "cuda":
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
    p = precision.lower()

    if p == "auto":
        return {"torch_dtype": _best_float_dtype()}

    if _is_quant(p):
        if DEVICE != "cuda":
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

    dtype = _DTYPE_ALIASES.get(p)
    if dtype is None:
        warnings.warn(f"Unknown precision '{precision}'; defaulting to auto.")
        dtype = _best_float_dtype()
    return {"torch_dtype": dtype}


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
    """Singleton that keeps at most one model in VRAM."""

    def __init__(self):
        self._model = None
        self._name = None
        self._lock = asyncio.Lock()

    def _evict(self):
        """Unload the current model and free VRAM. Caller must hold the lock."""
        if self._model is not None:
            del self._model
            self._model = None
            self._name = None
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

    # --- public async context managers (use with `async with await manager.x()`) ---

    async def embedder(self):
        return _ModelContext(self, "embedder", self._load_embedder)

    async def reranker(self):
        return _ModelContext(self, "reranker", self._load_reranker)

    async def generator(self):
        return _ModelContext(self, "generator", self._load_generator)

    # --- private loaders ---

    @staticmethod
    def _load_sentence_model(cls, path, role):
        """Load a SentenceTransformer / CrossEncoder honouring PRECISION[role]."""
        precision = PRECISION.get(role, "bf16")
        load_kwargs = _build_load_kwargs(precision)
        quantized = "quantization_config" in load_kwargs
        # When quantized, accelerate places weights via device_map; passing an
        # explicit device on top would double-place, so leave it to the library.
        device = None if quantized else DEVICE
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
                    device=DEVICE,
                    trust_remote_code=True,
                    model_kwargs=_build_load_kwargs("auto"),
                )
            raise

    def _load_embedder(self):
        return self._load_sentence_model(SentenceTransformer, EMBEDDER_PATH, "embedder")

    def _load_reranker(self):
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
        load_kwargs.setdefault("device_map", DEVICE)

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
            fb.setdefault("device_map", DEVICE)
            model = model_cls.from_pretrained(GENERATOR_PATH, **fb)

        model.eval()
        return {"model": model, "processor": processor}


class _ModelContext:
    """Async context manager: acquire lock, (lazily) load model, yield, release.

    Eviction happens lazily: a model is only unloaded when a *different* model is
    requested. This keeps the model warm for back-to-back calls of the same kind.
    """

    def __init__(self, manager: "ModelManager", name: str, loader):
        self._manager = manager
        self._name = name
        self._loader = loader

    async def __aenter__(self):
        await self._manager._lock.acquire()
        try:
            if self._manager._name != self._name:
                self._manager._evict()
                self._manager._model = self._loader()
                self._manager._name = self._name
        except Exception:
            self._manager._lock.release()
            raise
        return self._manager._model

    async def __aexit__(self, *_):
        self._manager._lock.release()
        return False


# Global singleton
manager = ModelManager()
