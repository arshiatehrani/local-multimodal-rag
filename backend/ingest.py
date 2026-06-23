"""File preprocessing -> chunking -> embedding -> Qdrant upsert.

Supports PDFs (text chunks + embedded images), standalone images, and videos
(keyframes sampled at ~1 fps).
"""

import io
import os
import base64
import tempfile
from pathlib import Path

from PIL import Image
import fitz  # pymupdf

from model_manager import manager
from qdrant_store import ensure_collection, upsert_points

CHUNK_SIZE = 256      # words per chunk (proxy for tokens)
CHUNK_OVERLAP = 64    # words of overlap between chunks
EMBED_BATCH = 4       # small batches keep activation memory low on 6 GB GPUs
MAX_IMAGE_DIM = 1024  # downscale large images before embedding to cap VRAM use

DOC_INSTRUCTION = "Represent the content for retrieval."

# File types we can ingest. Used by the folder-upload path to skip the rest.
SUPPORTED_EXTS = {
    ".pdf",
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff",
    ".mp4", ".avi", ".mov", ".mkv",
}


def is_supported(filename: str) -> bool:
    return Path(filename).suffix.lower() in SUPPORTED_EXTS


def _cap_image(pil_img: Image.Image, max_dim: int = MAX_IMAGE_DIM) -> Image.Image:
    """Downscale very large images so the vision encoder doesn't blow up VRAM."""
    w, h = pil_img.size
    if max(w, h) <= max_dim:
        return pil_img
    scale = max_dim / float(max(w, h))
    new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
    return pil_img.resize(new_size, Image.LANCZOS)


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list:
    words = text.split()
    chunks, i = [], 0
    step = max(1, size - overlap)
    while i < len(words):
        chunks.append(" ".join(words[i:i + size]))
        i += step
    return [c for c in chunks if len(c.strip()) > 20]


def _thumbnail_b64(pil_img: Image.Image) -> str:
    thumb = pil_img.copy()
    thumb.thumbnail((256, 256))
    buf = io.BytesIO()
    thumb.save(buf, format="JPEG")
    return base64.b64encode(buf.getvalue()).decode()


def process_pdf(file_bytes: bytes, filename: str) -> list:
    items = []
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    for page_num, page in enumerate(doc):
        text = page.get_text("text")
        for chunk in chunk_text(text):
            items.append({
                "type": "text",
                "content": chunk,
                "meta": {"filename": filename, "page": page_num + 1, "modality": "text"},
            })
        for img in page.get_images(full=True):
            xref = img[0]
            try:
                base_image = doc.extract_image(xref)
                pil_img = Image.open(io.BytesIO(base_image["image"])).convert("RGB")
            except Exception:
                continue
            items.append({
                "type": "image",
                "content": pil_img,
                "meta": {
                    "filename": filename,
                    "page": page_num + 1,
                    "modality": "image",
                    "thumbnail_b64": _thumbnail_b64(pil_img),
                },
            })
    doc.close()
    return items


def process_image(file_bytes: bytes, filename: str) -> list:
    pil_img = Image.open(io.BytesIO(file_bytes)).convert("RGB")
    return [{
        "type": "image",
        "content": pil_img,
        "meta": {
            "filename": filename,
            "page": 1,
            "modality": "image",
            "thumbnail_b64": _thumbnail_b64(pil_img),
        },
    }]


def process_video(file_bytes: bytes, filename: str) -> list:
    import cv2

    items = []
    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as f:
        f.write(file_bytes)
        tmp_path = f.name
    try:
        cap = cv2.VideoCapture(tmp_path)
        fps = cap.get(cv2.CAP_PROP_FPS) or 25
        frame_interval = max(1, int(round(fps)))  # one frame per second
        frame_idx = 0
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            if frame_idx % frame_interval == 0:
                pil_img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                items.append({
                    "type": "image",
                    "content": pil_img,
                    "meta": {
                        "filename": filename,
                        "page": frame_idx // frame_interval,  # second index
                        "modality": "video_frame",
                        "thumbnail_b64": _thumbnail_b64(pil_img),
                    },
                })
            frame_idx += 1
        cap.release()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    return items


async def ingest_file(file_bytes: bytes, filename: str, space_id: str, file_id: str) -> int:
    """Chunk + embed a file and store its vectors, tagged with space_id/file_id."""
    ensure_collection()
    ext = Path(filename).suffix.lower()

    if ext == ".pdf":
        items = process_pdf(file_bytes, filename)
    elif ext in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff"}:
        items = process_image(file_bytes, filename)
    elif ext in {".mp4", ".avi", ".mov", ".mkv"}:
        items = process_video(file_bytes, filename)
    else:
        raise ValueError(f"Unsupported file type: {ext}")

    if not items:
        return 0

    all_vectors, all_payloads = [], []
    async with await manager.embedder() as embedder:
        for i in range(0, len(items), EMBED_BATCH):
            batch = items[i:i + EMBED_BATCH]
            encode_inputs = []
            for item in batch:
                if item["type"] == "text":
                    encode_inputs.append(item["content"])
                else:
                    # Qwen3-VL embedder accepts a dict with an "image" PIL object.
                    encode_inputs.append({"image": _cap_image(item["content"])})

            vecs = embedder.encode(
                encode_inputs,
                prompt=DOC_INSTRUCTION,
                normalize_embeddings=True,
                batch_size=EMBED_BATCH,
            )

            for vec, item in zip(vecs, batch):
                all_vectors.append(vec)
                pay = dict(item["meta"])
                pay["space_id"] = space_id
                pay["file_id"] = file_id
                if item["type"] == "text":
                    pay["text"] = item["content"]
                all_payloads.append(pay)

    upsert_points(all_vectors, all_payloads)
    return len(all_vectors)
