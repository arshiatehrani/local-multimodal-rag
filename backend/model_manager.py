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
from concurrent.futures import ThreadPoolExecutor

os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_BACKEND_DIR)
MODELS_DIR = os.environ.get("MODELS_DIR", os.path.join(_PROJECT_ROOT, "models"))

EMBEDDER_PATH = os.path.join(MODELS_DIR, "Qwen3-VL-Embedding-2B")
RERANKER_PATH = os.path.join(MODELS_DIR, "Qwen3-VL-Reranker-2B")
GENERATOR_PATH = os.path.join(MODELS_DIR, "Qwen3-VL-2B-Instruct")

# embedder | all | none  — default loads all three at startup
PRELOAD_MODELS = os.environ.get("PRELOAD_MODELS", "all").lower()

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


def _inspect_quantization(name: str, obj) -> dict:
    """Walk loaded modules and report whether weights are truly 4-bit/8-bit."""
    configured = PRECISION.get(name, "?")
    stats = {
        "configured": configured,
        "linear4bit": 0,
        "linear8bit": 0,
        "other_linear": 0,
        "total_modules": 0,
        "verdict": "unknown",
    }

    def _walk(root):
        if isinstance(root, dict):
            for v in root.values():
                yield from _walk(v)
            return
        # SentenceTransformer / CrossEncoder
        for attr in ("_first_module", "auto_model", "model", "transformer"):
            child = getattr(root, attr, None)
            if child is not None:
                yield from _walk(child)
        if hasattr(root, "modules"):
            for mod in root.modules():
                yield mod

    for mod in _walk(obj):
        stats["total_modules"] += 1
        cls = type(mod).__name__
        mod_name = type(mod).__module__ or ""
        if cls == "Linear4bit" or "Linear4bit" in cls:
            stats["linear4bit"] += 1
        elif cls == "Linear8bitLt" or "Linear8bit" in cls:
            stats["linear8bit"] += 1
        elif cls == "Linear" and "torch.nn" in mod_name:
            stats["other_linear"] += 1

    if stats["linear4bit"] > 0:
        stats["verdict"] = "4bit (NF4) confirmed"
    elif stats["linear8bit"] > 0:
        stats["verdict"] = "8bit confirmed"
    elif stats["other_linear"] > 0 and configured in ("4bit", "8bit"):
        stats["verdict"] = f"NOT quantized — {stats['other_linear']} regular Linear layers (likely fp16/fp32)"
    elif configured in ("bf16", "fp16", "fp32", "auto"):
        stats["verdict"] = f"float ({configured})"
    else:
        stats["verdict"] = "could not detect quant layers (check logs)"

    print(
        f"[models] quant check {name}: configured={configured} | "
        f"Linear4bit={stats['linear4bit']} Linear8bit={stats['linear8bit']} "
        f"regular_Linear={stats['other_linear']} | {stats['verdict']}",
        flush=True,
    )
    return stats


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


class _WeightLoadProgress:
    """Patch tqdm during ``from_pretrained`` so the UI tracks real weight shards."""

    def __init__(self, on_progress):
        self._on_progress = on_progress
        self._orig_tqdm = None

    def __enter__(self):
        import tqdm
        import tqdm.std

        orig = tqdm.std.tqdm
        hook = self

        class ReportingTqdm(orig):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                if self.total and self.total > 1:
                    hook._on_progress(int(self.n), int(self.total))

            def update(self, n=1):
                super().update(n)
                if self.total and self.total > 1:
                    hook._on_progress(int(self.n), int(self.total))

        self._orig_tqdm = orig
        tqdm.std.tqdm = ReportingTqdm
        tqdm.tqdm = ReportingTqdm
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._orig_tqdm:
            import tqdm
            import tqdm.std
            tqdm.std.tqdm = self._orig_tqdm
            tqdm.tqdm = self._orig_tqdm
        return False


class ModelManager:
    """Load-once warm cache: models stay in VRAM after first use, no hot-swap."""

    _ORDER = ("embedder", "reranker", "generator")

    def __init__(self):
        self._models: dict = {}
        self._errors: dict = {}
        self._quant: dict = {}
        self._lock = asyncio.Lock()
        self._loaders = {
            "embedder": self._load_embedder,
            "reranker": self._load_reranker,
            "generator": self._load_generator,
        }
        self._loading: dict | None = None  # {name, pct} while a model is loading
        self._load_durations: dict[str, float] = {}
        self._progress_done: asyncio.Event | None = None
        self._progress_task: asyncio.Task | None = None
        self._load_waiters: dict[str, asyncio.Event] = {}
        self._load_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="model-load")

    def _set_loading_pct(self, pct: int) -> None:
        if self._loading is not None:
            self._loading["pct"] = min(99, max(0, int(pct)))

    def _set_weight_progress(self, current: int, total: int) -> None:
        if self._loading is None or total <= 0:
            return
        self._loading["step"] = "weights"
        self._loading["current"] = min(current, total)
        self._loading["total"] = total
        self._set_loading_pct(int(100 * current / total))

    async def _progress_ticker(self, name: str, done: asyncio.Event) -> None:
        """Keep pct at 0 until real weight-shard progress arrives; jump to 100 when done."""
        while not done.is_set():
            try:
                await asyncio.wait_for(done.wait(), timeout=0.35)
            except asyncio.TimeoutError:
                pass
        self._set_loading_pct(100)

    def _start_load_progress(self, name: str) -> asyncio.Event:
        self._loading = {"name": name, "pct": 0, "step": None, "current": 0, "total": 0}
        done = asyncio.Event()
        self._progress_done = done
        self._progress_task = asyncio.create_task(self._progress_ticker(name, done))
        return done

    async def _finish_load_progress(self, done: asyncio.Event, name: str, elapsed: float) -> None:
        self._load_durations[name] = elapsed
        done.set()
        if self._progress_task:
            try:
                await asyncio.wait_for(self._progress_task, timeout=2.0)
            except asyncio.TimeoutError:
                self._progress_task.cancel()
            self._progress_task = None
        self._loading = None
        self._progress_done = None

    def status(self) -> dict:
        loaded = {n: (n in self._models) for n in self._ORDER}
        used, free, total = _vram_gb()
        # Drop stale loading state if the model is already resident.
        if self._loading and self._loading.get("name") in self._models:
            self._loading = None
        loading = dict(self._loading) if self._loading else None
        count = sum(loaded.values())
        total_n = len(self._ORDER)
        if all(loaded.values()):
            overall_pct = 100
            ready = True
            loading = None
        elif loading:
            overall_pct = min(99, int((count * 100 + loading.get("pct", 0)) / total_n))
            ready = False
        else:
            overall_pct = int(count * 100 / total_n)
            ready = False
        chat_ready = bool(loaded.get("embedder")) and loading is None
        return {
            "loaded": loaded,
            "ready": ready,
            "chat_ready": chat_ready,
            "count": count,
            "total": total_n,
            "loading": loading,
            "overall_pct": overall_pct,
            "errors": dict(self._errors),
            "quantization": dict(self._quant),
            "vram_gb": {"used": round(used, 2), "free": round(free, 2), "total": round(total, 2)},
            "warmup_mode": PRELOAD_MODELS,
        }

    async def _cancel_load_progress(self) -> None:
        if self._progress_done:
            self._progress_done.set()
        if self._progress_task:
            try:
                await asyncio.wait_for(self._progress_task, timeout=1.0)
            except asyncio.TimeoutError:
                self._progress_task.cancel()
            self._progress_task = None
        self._loading = None
        self._progress_done = None

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
        """Load a single model. Heavy work runs off the API thread pool."""
        async with self._lock:
            if name in self._models:
                return
            if name in self._load_waiters:
                waiter = self._load_waiters[name]
                is_leader = False
            else:
                waiter = asyncio.Event()
                self._load_waiters[name] = waiter
                is_leader = True

        if not is_leader:
            await waiter.wait()
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
        progress_done = self._start_load_progress(name)
        loop = asyncio.get_running_loop()

        def _run_loader():
            with _WeightLoadProgress(self._set_weight_progress):
                return loader()

        try:
            try:
                model = await loop.run_in_executor(self._load_executor, _run_loader)
                async with self._lock:
                    self._models[name] = model
                    self._errors.pop(name, None)
            except Exception as e:
                if evict_others_on_retry and self._models and _is_oom(e):
                    print(
                        f"[models] OOM loading {name} with others resident — "
                        "evicting them and retrying once",
                        flush=True,
                    )
                    async with self._lock:
                        self._evict_all_except(None)
                    _compact_vram()
                    await self._cancel_load_progress()
                    progress_done = self._start_load_progress(name)
                    model = await loop.run_in_executor(self._load_executor, _run_loader)
                    async with self._lock:
                        self._models[name] = model
                        self._errors.pop(name, None)
                else:
                    await self._cancel_load_progress()
                    raise

            elapsed = time.time() - t0
            await self._finish_load_progress(progress_done, name, elapsed)
            self._quant[name] = _inspect_quantization(name, self._models[name])
            print(f"[models] loaded {name} in {elapsed:.1f}s", flush=True)
            _log_vram(f"after {name}")

            used_after, _, _ = _vram_gb()
            delta = used_after - used_before
            if delta > _SINGLE_MODEL_VRAM_WARN_GB and _device() == "cuda":
                msg = (
                    f"{name} added ~{delta:.1f} GB VRAM (now {used_after:.1f} GB total). "
                    f"Expected ~1.3 GB for 4-bit — see quant check above."
                )
                print(f"[models] WARNING: {msg}", flush=True)
                if self._quant[name].get("verdict", "").startswith("NOT quantized"):
                    self._errors[name] = self._quant[name]["verdict"]
        finally:
            async with self._lock:
                self._load_waiters.pop(name, None)
                waiter.set()

    async def preload_all(self):
        """Background warmup at startup — loads all three models sequentially."""
        if PRELOAD_MODELS == "none":
            print("[models] warmup skipped (PRELOAD_MODELS=none)", flush=True)
            return

        targets = list(self._ORDER) if PRELOAD_MODELS == "all" else ["embedder"]
        print(f"[models] warmup started (targets: {', '.join(targets)})", flush=True)

        for name in targets:
            if name in self._models:
                continue
            try:
                await self._load_one(name, evict_others_on_retry=False)
            except Exception as e:
                self._errors[name] = repr(e)
                print(f"[models] FAILED to load {name}: {e!r}", flush=True)
                traceback.print_exc()
                break

        st = self.status()
        print(f"[models] warmup finished: {st['count']}/{st['total']} loaded", flush=True)
        if st["quantization"]:
            for n, q in st["quantization"].items():
                print(f"[models]   {n}: {q.get('verdict', '?')}", flush=True)

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
        try:
            await self._manager._load_one(self._name)
        except Exception as e:
            self._manager._errors[self._name] = repr(e)
            raise
        async with self._manager._lock:
            return self._manager._models[self._name]

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is not None:
            async with self._manager._lock:
                self._manager._evict_one(self._name)
        return False


manager = ModelManager()
