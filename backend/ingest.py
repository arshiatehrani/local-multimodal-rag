"""File preprocessing -> chunking -> embedding -> Qdrant upsert.

Supports PDFs (text chunks + embedded images), standalone images, and videos
(keyframes sampled at ~1 fps).
"""

import io
import os
import re
import asyncio
import base64
import tempfile
from pathlib import Path

from PIL import Image
import fitz  # pymupdf

from model_manager import manager
from document_stats import (
    attach_stats_to_meta,
    build_stats_summary_text,
    compute_text_stats,
)
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


def process_pdf(file_bytes: bytes, filename: str) -> tuple[list, dict | None]:
    items = []
    page_texts: dict[int, str] = {}
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
        page_texts[page_no] = full_text
        paragraphs = page_paragraphs[page_num]
        paragraph_count_page = len(paragraphs)
        page_word_count = word_count(full_text)
        page_word_cursor = 1

        if full_text:
            words = tokenize_words(full_text)
            items.append(_make_text_item(
                full_text, filename, page_no, total_pages,
                paragraph_index=0, global_paragraph_index=global_para_idx,
                paragraph_count_page=paragraph_count_page,
                paragraph_count_doc=paragraph_count_doc,
                region="body", chunk_kind="page_full",
                doc_word_start=doc_word_cursor + 1, page_word_start=1, para_word_start=1,
                page_word_count=page_word_count,
                doc_word_count=None,
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

    doc_text = "\n\n".join(page_texts[p] for p in sorted(page_texts) if page_texts[p])
    doc_stats = compute_text_stats(doc_text) if doc_text else None
    page_stats_map = {
        p: compute_text_stats(t) for p, t in page_texts.items() if t
    }

    if doc_stats and doc_stats.get("word_count", 0) > 0:
        summary = build_stats_summary_text(
            filename,
            doc_stats,
            total_pages=total_pages,
            paragraph_count_doc=paragraph_count_doc,
        )
        stats_item = _make_text_item(
            summary, filename, 1, total_pages,
            paragraph_index=0, global_paragraph_index=0,
            paragraph_count_page=0, paragraph_count_doc=paragraph_count_doc,
            region="body", chunk_kind="document_stats",
            doc_word_start=1, page_word_start=1, para_word_start=1,
            page_word_count=doc_stats["word_count"],
            doc_word_count=doc_stats["word_count"],
            para_word_count=doc_stats["word_count"],
        )
        attach_stats_to_meta(stats_item["meta"], doc_stats, page_stats_map.get(1))
        items.insert(0, stats_item)

    doc_word_total = doc_stats["word_count"] if doc_stats else doc_word_cursor
    for item in items:
        if item["type"] != "text":
            continue
        page_no = int(item["meta"].get("page") or 1)
        attach_stats_to_meta(
            item["meta"],
            doc_stats,
            page_stats_map.get(page_no),
        )
        item["meta"]["doc_word_count"] = doc_word_total

    return items, doc_stats


def process_image(file_bytes: bytes, filename: str) -> tuple[list, dict | None]:
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
    }], None


def process_video(file_bytes: bytes, filename: str) -> tuple[list, dict | None]:
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
    return items, None


def _items_from_bytes(file_bytes: bytes, filename: str) -> tuple[list, dict | None]:
    ext = Path(filename).suffix.lower()
    if ext == ".pdf":
        return process_pdf(file_bytes, filename)
    if ext in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff"}:
        return process_image(file_bytes, filename)
    if ext in {".mp4", ".avi", ".mov", ".mkv"}:
        return process_video(file_bytes, filename)
    raise ValueError(f"Unsupported file type: {ext}")


async def ingest_file_stream(file_bytes: bytes, filename: str, space_id: str, file_id: str):
    """Yield progress events while chunking, embedding, and upserting."""
    ensure_collection()
    yield {"type": "progress", "pct": 5, "text": "Processing document…", "stage": "process"}

    items, text_stats = await asyncio.to_thread(_items_from_bytes, file_bytes, filename)
    if not items:
        yield {"type": "complete", "chunks": 0, "pct": 100, "text": "No content extracted", "text_stats": None}
        return

    total = len(items)
    n_batches = max(1, (total + EMBED_BATCH - 1) // EMBED_BATCH)
    yield {
        "type": "progress",
        "pct": 15,
        "text": f"Prepared {total} chunk{'s' if total != 1 else ''}",
        "stage": "process",
        "chunks_total": total,
    }

    all_vectors, all_payloads = [], []
    async with await manager.embedder() as embedder:
        for bi, i in enumerate(range(0, total, EMBED_BATCH)):
            batch = items[i:i + EMBED_BATCH]
            encode_inputs = []
            for item in batch:
                if item["type"] == "text":
                    encode_inputs.append(item["content"])
                else:
                    encode_inputs.append({"image": _cap_image(item["content"])})

            vecs = await asyncio.to_thread(
                embedder.encode,
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

            done = min(i + len(batch), total)
            pct = 15 + int(78 * (bi + 1) / n_batches)
            yield {
                "type": "progress",
                "pct": pct,
                "text": f"Embedding {done}/{total} chunks…",
                "stage": "embed",
                "chunks_done": done,
                "chunks_total": total,
            }

    yield {"type": "progress", "pct": 96, "text": "Saving to vector store…", "stage": "store"}
    await asyncio.to_thread(upsert_points, all_vectors, all_payloads)
    yield {
        "type": "complete",
        "chunks": len(all_vectors),
        "pct": 100,
        "text": f"Stored {len(all_vectors)} chunk{'s' if len(all_vectors) != 1 else ''}",
        "text_stats": text_stats,
    }


async def ingest_file(file_bytes: bytes, filename: str, space_id: str, file_id: str) -> int:
    """Chunk + embed a file and store its vectors, tagged with space_id/file_id."""
    chunks = 0
    async for ev in ingest_file_stream(file_bytes, filename, space_id, file_id):
        if ev.get("type") == "complete":
            chunks = int(ev.get("chunks", 0))
    return chunks
