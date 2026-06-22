"""Hot-swapping model manager.

Ensures only ONE model (embedder / reranker / generator) is resident in VRAM at
any given time. Models are loaded on demand and the previously loaded model is
evicted (del -> gc.collect -> torch.cuda.empty_cache) before a different one is
loaded. An asyncio.Lock serialises swaps so concurrent requests cannot corrupt
the VRAM state.
"""

import gc
import asyncio
import torch
from sentence_transformers import SentenceTransformer, CrossEncoder

EMBEDDER_PATH = "./models/Qwen3-VL-Embedding-2B"
RERANKER_PATH = "./models/Qwen3-VL-Reranker-2B"
GENERATOR_PATH = "./models/Qwen3-VL-2B-Instruct"

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
DTYPE = torch.bfloat16 if DEVICE == "cuda" else torch.float32


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

    def _load_embedder(self):
        return SentenceTransformer(
            EMBEDDER_PATH,
            device=DEVICE,
            trust_remote_code=True,
            model_kwargs={"torch_dtype": DTYPE},
        )

    def _load_reranker(self):
        return CrossEncoder(
            RERANKER_PATH,
            device=DEVICE,
            trust_remote_code=True,
            model_kwargs={"torch_dtype": DTYPE},
        )

    def _load_generator(self):
        from transformers import AutoProcessor

        processor = AutoProcessor.from_pretrained(GENERATOR_PATH, trust_remote_code=True)
        model_cls = _load_generator_cls()

        load_kwargs = dict(
            torch_dtype=DTYPE,
            device_map=DEVICE,
            trust_remote_code=True,
        )
        # flash_attention_2 is optional and frequently unavailable on Windows.
        try:
            model = model_cls.from_pretrained(
                GENERATOR_PATH,
                attn_implementation="flash_attention_2",
                **load_kwargs,
            )
        except Exception:
            model = model_cls.from_pretrained(GENERATOR_PATH, **load_kwargs)

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
