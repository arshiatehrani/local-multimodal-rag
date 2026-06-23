"""File preprocessing -> chunking -> embedding -> Qdrant upsert.

Supports PDFs (text chunks + embedded images), standalone images, and videos
(keyframes sampled at ~1 fps).
"""

import io
import os
import re
import base64
import tempfile
from pathlib import Path

from PIL import Image
import fitz  # pymupdf

from model_manager import manager
from positioning import (
    build_text_chunk_meta,
    tokenize_words,
    word_count,
    region_from_y,
)
from qdrant_store import ensure_collection, upsert_points

CHUNK_SIZE = 128      # words per chunk (smaller windows for finer retrieval)
CHUNK_OVERLAP = 32    # words of overlap between chunks
EMBED_BATCH = 4       # small batches keep activation memory low on 6 GB GPUs
MAX_IMAGE_DIM = 1024  # downscale large images before embedding to cap VRAM use
MIN_CHUNK_CHARS = 20

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
    return [c for c in chunks if len(c.strip()) > MIN_CHUNK_CHARS]


def _paragraphs_from_page(page) -> list[dict]:
    """Split page into paragraphs with layout region (header/body/footer)."""
    page_height = float(page.rect.height) or 1.0
    text = page.get_text("text") or ""
    raw_parts = [p.strip() for p in re.split(r"\n\s*\n+", text) if p.strip()]

    blocks = page.get_text("blocks") or []
    block_regions: list[tuple[str, str]] = []
    for b in blocks:
        if len(b) < 5 or b[6] != 0:
            continue
        t = (b[4] or "").strip()
        if not t:
            continue
        y_mid = (float(b[1]) + float(b[3])) / 2.0
        block_regions.append((t, region_from_y(y_mid, page_height)))

    if len(raw_parts) > 1:
        paragraphs = []
        for part in raw_parts:
            region = "body"
            for bt, br in block_regions:
                if part.startswith(bt[: min(40, len(bt))]) or bt in part:
                    region = br
                    break
            if region == "body" and block_regions:
                for bt, br in block_regions:
                    if bt == part or part in bt:
                        region = br
                        break
            paragraphs.append({"text": part, "region": region})
        return paragraphs

    if block_regions:
        return [{"text": t, "region": r} for t, r in block_regions]

    return [{"text": text.strip(), "region": "body"}] if text.strip() else []


def _make_text_item(
    content: str,
    filename: str,
    page: int,
    total_pages: int,
    paragraph_index: int,
    global_paragraph_index: int,
    paragraph_count_page: int,
    paragraph_count_doc: int,
    region: str,
    chunk_kind: str,
    doc_word_start: int,
    page_word_start: int,
    para_word_start: int,
    page_word_count: int | None = None,
    doc_word_count: int | None = None,
    para_word_count: int | None = None,
) -> dict:
    meta = build_text_chunk_meta(
        content=content,
        filename=filename,
        page=page,
        total_pages=total_pages,
        paragraph_index=paragraph_index,
        global_paragraph_index=global_paragraph_index,
        paragraph_count_page=paragraph_count_page,
        paragraph_count_doc=paragraph_count_doc,
        region=region,
        chunk_kind=chunk_kind,
        doc_word_start=doc_word_start,
        page_word_start=page_word_start,
        para_word_start=para_word_start,
        page_word_count=page_word_count,
        doc_word_count=doc_word_count,
        para_word_count=para_word_count,
    )
    return {"type": "text", "content": content, "meta": meta}


def _thumbnail_b64(pil_img: Image.Image) -> str:
    thumb = pil_img.copy()
    thumb.thumbnail((256, 256))
    buf = io.BytesIO()
    thumb.save(buf, format="JPEG")
    return base64.b64encode(buf.getvalue()).decode()


def process_pdf(file_bytes: bytes, filename: str) -> list:
    items = []
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    total_pages = len(doc)
    doc_word_cursor = 0
    global_para_idx = 0

    # First pass: count paragraphs for doc-level stats.
    page_paragraphs: list[list[dict]] = []
    for page_num in range(total_pages):
        page_paragraphs.append(_paragraphs_from_page(doc[page_num]))
    paragraph_count_doc = sum(len(p) for p in page_paragraphs)

    for page_num, page in enumerate(doc):
        page_no = page_num + 1
        page_from_end = total_pages - page_num
        full_text = (page.get_text("text") or "").strip()
        paragraphs = page_paragraphs[page_num]
        paragraph_count_page = len(paragraphs)
        page_word_count = word_count(full_text)
        page_word_cursor = 1

        if total_pages == 1 and full_text:
            words = tokenize_words(full_text)
            items.append(_make_text_item(
                full_text, filename, page_no, total_pages,
                paragraph_index=0, global_paragraph_index=0,
                paragraph_count_page=paragraph_count_page,
                paragraph_count_doc=paragraph_count_doc,
                region="body", chunk_kind="page_full",
                doc_word_start=1, page_word_start=1, para_word_start=1,
                page_word_count=page_word_count,
                para_word_count=len(words),
            ))

        for para_idx, para in enumerate(paragraphs):
            para_text = para["text"]
            region = para.get("region", "body")
            words = tokenize_words(para_text)
            para_word_count = len(words)
            doc_w_start = doc_word_cursor + 1
            page_w_start = page_word_cursor
            para_w_start = 1

            if len(words) <= CHUNK_SIZE:
                items.append(_make_text_item(
                    para_text, filename, page_no, total_pages,
                    para_idx, global_para_idx,
                    paragraph_count_page, paragraph_count_doc,
                    region, "paragraph",
                    doc_w_start, page_w_start, para_w_start,
                    page_word_count=page_word_count,
                    para_word_count=para_word_count,
                ))
            else:
                step = max(1, CHUNK_SIZE - CHUNK_OVERLAP)
                i = 0
                while i < len(words):
                    sub_words = words[i:i + CHUNK_SIZE]
                    sub = " ".join(sub_words)
                    if len(sub.strip()) > MIN_CHUNK_CHARS:
                        items.append(_make_text_item(
                            sub, filename, page_no, total_pages,
                            para_idx, global_para_idx,
                            paragraph_count_page, paragraph_count_doc,
                            region, "window",
                            doc_w_start + i, page_w_start + i, i + 1,
                            page_word_count=page_word_count,
                            para_word_count=para_word_count,
                        ))
                    i += step

            doc_word_cursor += para_word_count
            page_word_cursor += para_word_count
            global_para_idx += 1

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
                    "page": page_no,
                    "total_pages": total_pages,
                    "page_from_end": page_from_end,
                    "modality": "image",
                    "thumbnail_b64": _thumbnail_b64(pil_img),
                },
            })
    doc.close()

    doc_word_count = doc_word_cursor
    for item in items:
        if item["type"] == "text":
            item["meta"]["doc_word_count"] = doc_word_count

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
