"""VRAM-aware warm-cache model manager with per-model quantization.

Models are loaded on demand and **kept resident** once loaded (no swap between
embed / rerank / generate in later queries). Startup only preloads the embedder
in the background — loading all three at once can OOM on 6 GB cards because each
``from_pretrained`` needs a temporary VRAM spike on top of models already resident.

If a 4-bit load fails on CUDA we **do not** silently fall back to fp16 (one fp16
2B model alone is ~4 GB and makes multi-model residency impossible). An
``asyncio.Lock`` serialises GPU work across requests.

Quantization is configured PER MODEL via the ``PRECISION`` dict below.
"""

import os
import gc
import time
import asyncio
import warnings
import traceback

os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_BACKEND_DIR)
MODELS_DIR = os.environ.get("MODELS_DIR", os.path.join(_PROJECT_ROOT, "models"))

EMBEDDER_PATH = os.path.join(MODELS_DIR, "Qwen3-VL-Embedding-2B")
RERANKER_PATH = os.path.join(MODELS_DIR, "Qwen3-VL-Reranker-2B")
GENERATOR_PATH = os.path.join(MODELS_DIR, "Qwen3-VL-2B-Instruct")

# Stop background warmup after this model (safest on 6 GB). Set PRELOAD_MODELS=all
# to attempt all three (may OOM). Values: embedder | all | none
PRELOAD_MODELS = os.environ.get("PRELOAD_MODELS", "embedder").lower()

# After a single 4-bit model loads, used VRAM should stay below this (GB).
_SINGLE_MODEL_VRAM_WARN_GB = float(os.environ.get("SINGLE_MODEL_VRAM_WARN_GB", "2.5"))
# Need at least this much free before attempting another load while others resident.
_LOAD_HEADROOM_GB = float(os.environ.get("LOAD_HEADROOM_GB", "1.8"))


def _device() -> str:
    import torch
    return "cuda" if torch.cuda.is_available() else "cpu"


PRECISION = {
    "embedder": "4bit",
    "reranker": "4bit",
    "generator": "4bit",
}

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


def _compact_vram() -> None:
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.synchronize()
    except Exception:
        pass


def _vram_gb():
    """Return (used_gb, free_gb, total_gb). All zero if no CUDA."""
    try:
        import torch
        if not torch.cuda.is_available():
            return 0.0, 0.0, 0.0
        free, total = torch.cuda.mem_get_info()
        used = total - free
        return used / 1e9, free / 1e9, total / 1e9
    except Exception:
        return 0.0, 0.0, 0.0


def _log_vram(label: str) -> None:
    used, free, total = _vram_gb()
    if total > 0:
        print(f"[models] VRAM {label}: {used:.1f} GB used, {free:.1f} GB free / {total:.1f} GB", flush=True)


def _is_oom(exc: BaseException) -> bool:
    name = type(exc).__name__
    if "OutOfMemory" in name or "out of memory" in str(exc).lower():
        return True
    if isinstance(exc, RuntimeError) and "CUDA" in str(exc):
        return True
    return False


def _build_load_kwargs(precision: str) -> dict:
    import torch
    p = precision.lower()

    if p == "auto":
        return {"torch_dtype": _best_float_dtype()}

    if _is_quant(p):
        if _device() != "cuda":
            warnings.warn(f"Precision '{precision}' needs CUDA; using float32 on CPU.")
            return {"torch_dtype": torch.float32}
        from transformers import BitsAndBytesConfig

        if p in _EIGHT_BIT:
            qcfg = BitsAndBytesConfig(load_in_8bit=True)
        else:
            qcfg = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_use_double_quant=True,
                bnb_4bit_compute_dtype=_best_float_dtype(),
            )
        return {"quantization_config": qcfg, "device_map": "auto"}

    if p in _DTYPE_NAMES:
        return {"torch_dtype": _resolve_dtype(p)}

    warnings.warn(f"Unknown precision '{precision}'; defaulting to auto.")
    return {"torch_dtype": _best_float_dtype()}


def _load_generator_cls():
    try:
        from transformers import Qwen3VLForConditionalGeneration
        return Qwen3VLForConditionalGeneration
    except ImportError:
        from transformers import AutoModelForImageTextToText
        return AutoModelForImageTextToText


class ModelManager:
    """Load-once warm cache: models stay in VRAM after first use, no hot-swap."""

    _ORDER = ("embedder", "reranker", "generator")

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
        loaded = {n: (n in self._models) for n in self._ORDER}
        used, free, total = _vram_gb()
        return {
            "loaded": loaded,
            "ready": all(loaded.values()),
            "count": sum(loaded.values()),
            "total": len(self._ORDER),
            "errors": dict(self._errors),
            "vram_gb": {"used": round(used, 2), "free": round(free, 2), "total": round(total, 2)},
            "warmup_mode": PRELOAD_MODELS,
        }

    def _evict_one(self, name: str) -> None:
        if name in self._models:
            print(f"[models] evicting {name} to free VRAM", flush=True)
            del self._models[name]
            _compact_vram()

    def _evict_all_except(self, keep: str | None) -> None:
        for name in list(self._models.keys()):
            if name != keep:
                self._evict_one(name)

    async def _load_one(self, name: str, *, evict_others_on_retry: bool = True) -> None:
        """Load a single model under the lock. Caller must hold ``self._lock``."""
        if name in self._models:
            return

        loader = self._loaders[name]
        _compact_vram()
        used_before, free_before, _ = _vram_gb()
        print(
            f"[models] loading {name} ... ({len(self._models)} already resident, "
            f"{free_before:.1f} GB free)",
            flush=True,
        )
        t0 = time.time()

        try:
            self._models[name] = await asyncio.to_thread(loader)
            self._errors.pop(name, None)
        except Exception as e:
            if evict_others_on_retry and self._models and _is_oom(e):
                print(
                    f"[models] OOM loading {name} with others resident — "
                    "evicting them and retrying once",
                    flush=True,
                )
                self._evict_all_except(None)
                _compact_vram()
                self._models[name] = await asyncio.to_thread(loader)
                self._errors.pop(name, None)
            else:
                raise

        elapsed = time.time() - t0
        print(f"[models] loaded {name} in {elapsed:.1f}s", flush=True)
        _log_vram(f"after {name}")

        used_after, _, _ = _vram_gb()
        delta = used_after - used_before
        if delta > _SINGLE_MODEL_VRAM_WARN_GB and _device() == "cuda":
            msg = (
                f"{name} added ~{delta:.1f} GB VRAM (now {used_after:.1f} GB total). "
                f"Expected ~1.3 GB for 4-bit — quantization may have failed."
            )
            print(f"[models] WARNING: {msg}", flush=True)
            self._errors[name] = msg

    async def preload_all(self):
        """Background warmup at startup. Default: embedder only (safe on 6 GB)."""
        if PRELOAD_MODELS == "none":
            print("[models] warmup skipped (PRELOAD_MODELS=none)", flush=True)
            return

        targets = list(self._ORDER) if PRELOAD_MODELS == "all" else ["embedder"]
        print(f"[models] warmup started (targets: {', '.join(targets)})", flush=True)

        for name in targets:
            async with self._lock:
                if name in self._models:
                    continue
                try:
                    await self._load_one(name)
                except Exception as e:
                    self._errors[name] = repr(e)
                    print(f"[models] FAILED to load {name}: {e!r}", flush=True)
                    traceback.print_exc()
                    break

                used, free, _ = _vram_gb()
                if used > _SINGLE_MODEL_VRAM_WARN_GB:
                    print(
                        f"[models] stopping warmup early — {used:.1f} GB used after "
                        f"{name} (likely not 4-bit). Remaining models load on first use.",
                        flush=True,
                    )
                    break
                if free < _LOAD_HEADROOM_GB and name != targets[-1]:
                    print(
                        f"[models] stopping warmup early — only {free:.1f} GB free "
                        f"(need {_LOAD_HEADROOM_GB} GB headroom for next load).",
                        flush=True,
                    )
                    break

        st = self.status()
        print(f"[models] warmup finished: {st['count']}/{st['total']} loaded", flush=True)

    async def embedder(self):
        return _ModelContext(self, "embedder")

    async def reranker(self):
        return _ModelContext(self, "reranker")

    async def generator(self):
        return _ModelContext(self, "generator")

    @staticmethod
    def _load_sentence_model(cls, path, role):
        precision = PRECISION.get(role, "bf16")
        load_kwargs = _build_load_kwargs(precision)
        quantized = "quantization_config" in load_kwargs
        device = None if quantized else _device()
        try:
            return cls(
                path,
                device=device,
                trust_remote_code=True,
                model_kwargs=load_kwargs,
            )
        except Exception as e:
            if quantized and _device() == "cuda":
                raise RuntimeError(
                    f"{role}: {precision} load failed ({e}). "
                    "fp16 fallback is disabled on GPU because it prevents keeping "
                    "multiple models resident on 6 GB cards. "
                    "Check bitsandbytes/CUDA, or fix PRECISION for this model."
                ) from e
            if quantized:
                warnings.warn(f"{role}: '{precision}' load failed ({e}); using float32 on CPU.")
                return cls(path, device=_device(), trust_remote_code=True,
                           model_kwargs=_build_load_kwargs("auto"))
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
        load_kwargs.setdefault("device_map", _device())
        quantized = "quantization_config" in load_kwargs

        model = None
        last_err = None
        for extra in ({"attn_implementation": "flash_attention_2"}, {}):
            try:
                model = model_cls.from_pretrained(GENERATOR_PATH, **load_kwargs, **extra)
                break
            except Exception as e:
                last_err = e
                continue

        if model is None:
            if quantized and _device() == "cuda":
                raise RuntimeError(
                    f"generator: {precision} load failed ({last_err}). "
                    "fp16 fallback disabled on GPU (see model_manager.py)."
                ) from last_err
            fb = dict(_build_load_kwargs("auto"))
            fb["trust_remote_code"] = True
            fb.setdefault("device_map", _device())
            model = model_cls.from_pretrained(GENERATOR_PATH, **fb)

        model.eval()
        return {"model": model, "processor": processor}


class _ModelContext:
    def __init__(self, manager: "ModelManager", name: str):
        self._manager = manager
        self._name = name

    async def __aenter__(self):
        await self._manager._lock.acquire()
        try:
            await self._manager._load_one(self._name)
        except Exception as e:
            self._manager._errors[self._name] = repr(e)
            self._manager._lock.release()
            raise
        return self._manager._models[self._name]

    async def __aexit__(self, exc_type, exc, tb):
        try:
            if exc_type is not None:
                self._manager._evict_one(self._name)
        finally:
            self._manager._lock.release()
        return False


manager = ModelManager()
