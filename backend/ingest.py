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

SUPPORTED_EXTS = {
    ".pdf",
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff",
    ".mp4", ".avi", ".mov", ".mkv",
    ".txt", ".md", ".csv",
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


def _chunk_long_paragraph(text: str, max_words: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[tuple[str, int, int]]:
    """Adaptive Semantic Chunking (#4): Split long paragraphs along sentence boundaries."""
    # Split on sentence boundaries (period/question/exclamation + space)
    sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', text.strip()) if s.strip()]
    if not sentences:
        return []
    
    chunks = []
    current_chunk = []
    current_len = 0
    current_offset = 0
    
    for sent in sentences:
        words = tokenize_words(sent)
        w_len = len(words)
        
        if current_len + w_len > max_words and current_chunk:
            chunk_str = " ".join(current_chunk)
            chunks.append((chunk_str, current_len, current_offset))
            
            # Overlap: keep the last few sentences that fit in the overlap budget
            keep_len = 0
            keep_sentences = []
            for s in reversed(current_chunk):
                sl = len(tokenize_words(s))
                if keep_len + sl > overlap and keep_sentences:
                    break
                keep_sentences.insert(0, s)
                keep_len += sl
            
            advance_len = current_len - keep_len
            current_offset += advance_len
            
            current_chunk = keep_sentences + [sent]
            current_len = keep_len + w_len
        else:
            current_chunk.append(sent)
            current_len += w_len
            
    if current_chunk:
        chunk_str = " ".join(current_chunk)
        chunks.append((chunk_str, current_len, current_offset))
        
    return chunks


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
    
    # ── Document metadata extraction (#8) ──
    toc_text = ""
    try:
        toc = doc.get_toc()
        if toc:
            toc_lines = ["[TABLE OF CONTENTS]"]
            for level, title, pageno in toc:
                indent = "  " * (level - 1)
                toc_lines.append(f"{indent}- {title} (Page {pageno})")
            toc_text = "\n".join(toc_lines)
    except Exception:
        pass

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
                chunks = _chunk_long_paragraph(para_text)
                for c_text, c_len, c_offset in chunks:
                    if len(c_text.strip()) > MIN_CHUNK_CHARS:
                        items.append(_make_text_item(
                            c_text, filename, page_no, total_pages,
                            para_idx, global_para_idx,
                            paragraph_count_page, paragraph_count_doc,
                            region, "window",
                            doc_w_start + c_offset, page_w_start + c_offset, c_offset + 1,
                            page_word_count=page_word_count,
                            para_word_count=para_word_count,
                        ))

            doc_word_cursor += para_word_count
            page_word_cursor += para_word_count
            global_para_idx += 1

        for img in page.get_images(full=True):
            xref = img[0]
            try:
                base_image = doc.extract_image(xref)
                raw_img = Image.open(io.BytesIO(base_image["image"])).convert("RGB")
                pil_img = _cap_image(raw_img)
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

    if toc_text:
        toc_words = tokenize_words(toc_text)
        toc_item = _make_text_item(
            toc_text, filename, 1, total_pages,
            paragraph_index=0, global_paragraph_index=0,
            paragraph_count_page=0, paragraph_count_doc=paragraph_count_doc,
            region="body", chunk_kind="metadata_toc",
            doc_word_start=1, page_word_start=1, para_word_start=1,
            page_word_count=len(toc_words), doc_word_count=len(toc_words), para_word_count=len(toc_words),
        )
        items.insert(0, toc_item)

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
    raw_img = Image.open(io.BytesIO(file_bytes)).convert("RGB")
    pil_img = _cap_image(raw_img)
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
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as f:
            f.write(file_bytes)
            tmp_path = f.name
        cap = cv2.VideoCapture(tmp_path)
        fps = cap.get(cv2.CAP_PROP_FPS) or 25
        frame_interval = max(1, int(round(fps)))  # one frame per second
        frame_idx = 0
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            if frame_idx % frame_interval == 0:
                raw_img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                pil_img = _cap_image(raw_img)
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
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    return items, None


def process_text(file_bytes: bytes, filename: str) -> tuple[list, dict | None]:
    try:
        text = file_bytes.decode("utf-8")
    except UnicodeDecodeError:
        text = file_bytes.decode("latin1", errors="replace")
        
    items = []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", text) if p.strip()]
    doc_word_cursor = 0
    total_pages = 1
    
    # Doc stats
    doc_stats = compute_text_stats(text) if text else None
    
    if doc_stats and doc_stats.get("word_count", 0) > 0:
        summary = build_stats_summary_text(filename, doc_stats, total_pages=1, paragraph_count_doc=len(paragraphs))
        stats_item = _make_text_item(
            summary, filename, 1, 1,
            paragraph_index=0, global_paragraph_index=0,
            paragraph_count_page=0, paragraph_count_doc=len(paragraphs),
            region="body", chunk_kind="document_stats",
            doc_word_start=1, page_word_start=1, para_word_start=1,
            page_word_count=doc_stats["word_count"], doc_word_count=doc_stats["word_count"], para_word_count=doc_stats["word_count"],
        )
        attach_stats_to_meta(stats_item["meta"], doc_stats, doc_stats)
        items.append(stats_item)
        
    for para_idx, para_text in enumerate(paragraphs):
        words = tokenize_words(para_text)
        para_word_count = len(words)
        doc_w_start = doc_word_cursor + 1
        
        if len(words) <= CHUNK_SIZE:
            items.append(_make_text_item(
                para_text, filename, 1, 1,
                para_idx, para_idx,
                len(paragraphs), len(paragraphs),
                "body", "paragraph",
                doc_w_start, doc_w_start, 1,
                page_word_count=doc_stats["word_count"] if doc_stats else para_word_count,
                para_word_count=para_word_count,
            ))
        else:
            chunks = _chunk_long_paragraph(para_text)
            for c_text, c_len, c_offset in chunks:
                if len(c_text.strip()) > MIN_CHUNK_CHARS:
                    items.append(_make_text_item(
                        c_text, filename, 1, 1,
                        para_idx, para_idx,
                        len(paragraphs), len(paragraphs),
                        "body", "window",
                        doc_w_start + c_offset, doc_w_start + c_offset, c_offset + 1,
                        page_word_count=doc_stats["word_count"] if doc_stats else para_word_count,
                        para_word_count=para_word_count,
                    ))
                    
        doc_word_cursor += para_word_count
        
    doc_word_total = doc_stats["word_count"] if doc_stats else doc_word_cursor
    for item in items:
        if item["type"] == "text":
            attach_stats_to_meta(item["meta"], doc_stats, doc_stats)
            item["meta"]["doc_word_count"] = doc_word_total
            
    return items, doc_stats


def _items_from_bytes(file_bytes: bytes, filename: str) -> tuple[list, dict | None]:
    ext = Path(filename).suffix.lower()
    if ext == ".pdf":
        return process_pdf(file_bytes, filename)
    if ext in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff"}:
        return process_image(file_bytes, filename)
    if ext in {".mp4", ".avi", ".mov", ".mkv"}:
        return process_video(file_bytes, filename)
    if ext in {".txt", ".md", ".csv"}:
        return process_text(file_bytes, filename)
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
    db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "qdrant_data", "embed_cache")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    
    import shelve
    import hashlib
    
    with shelve.open(db_path) as cache:
        async with await manager.embedder() as embedder:
            for bi, i in enumerate(range(0, total, EMBED_BATCH)):
                batch = items[i:i + EMBED_BATCH]
                encode_inputs = []
                uncached_indices = []
                cached_vecs = {}
                
                for idx, item in enumerate(batch):
                    is_text = item["type"] == "text"
                    h = hashlib.sha256(item["content"].encode("utf-8")).hexdigest() if is_text else None
                    if h and str(h) in cache:
                        cached_vecs[idx] = cache[str(h)]
                    else:
                        uncached_indices.append(idx)
                        if is_text:
                            encode_inputs.append(item["content"])
                        else:
                            encode_inputs.append({"image": item["content"]})

                if encode_inputs:
                    new_vecs = await asyncio.to_thread(
                        embedder.encode,
                        encode_inputs,
                        prompt=DOC_INSTRUCTION,
                        normalize_embeddings=True,
                        batch_size=EMBED_BATCH,
                    )
                    
                    new_idx = 0
                    for idx, item in enumerate(batch):
                        if idx in uncached_indices:
                            vec = new_vecs[new_idx]
                            cached_vecs[idx] = vec
                            is_text = item["type"] == "text"
                            h = hashlib.sha256(item["content"].encode("utf-8")).hexdigest() if is_text else None
                            if h:
                                cache[str(h)] = vec
                            new_idx += 1

                for idx, item in enumerate(batch):
                    vec = cached_vecs[idx]
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
