"""Precise word indexing, chunk positional metadata, and natural-language position parsing."""

from __future__ import annotations

import re
from typing import Any

# 1-indexed ordinals -> 0-based index
_ORDINALS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
    "eleventh": 11, "twelfth": 12,
}

_ORDINAL_RE = "|".join(sorted(_ORDINALS.keys(), key=len, reverse=True))
_NUM_WORD = r"(\d+|(?:" + _ORDINAL_RE + r"))"


def tokenize_words(text: str) -> list[str]:
    """Split text into words. All positional counts use this tokenizer."""
    if not text or not str(text).strip():
        return []
    return re.findall(r"\S+", str(text).strip())


def word_count(text: str) -> int:
    return len(tokenize_words(text))


def _to_int(s: str) -> int:
    s = s.strip().lower()
    if s.isdigit():
        return int(s)
    return _ORDINALS.get(s, 0)


def _clean_word(w: str) -> str:
    return re.sub(r"^[^\w]+|[^\w]+$", "", w, flags=re.UNICODE).lower()


def page_position_label(page: int, total_pages: int) -> str:
    if total_pages <= 1:
        return "single"
    if page == 1:
        return "first"
    if page == total_pages:
        return "last"
    return "middle"


def para_position_label(para_idx: int, para_count: int) -> str:
    if para_count <= 1:
        return "only"
    if para_idx == 0:
        return "first"
    if para_idx == para_count - 1:
        return "last"
    return "middle"


def region_from_y(y_mid: float, page_height: float) -> str:
    if page_height <= 0:
        return "body"
    rel = y_mid / page_height
    if rel < 0.14:
        return "header"
    if rel > 0.86:
        return "footer"
    return "body"


def build_text_chunk_meta(
    *,
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
    doc_word_count: int | None = None,
    page_word_count: int | None = None,
    para_word_count: int | None = None,
) -> dict[str, Any]:
    """Build full positional payload for a text chunk."""
    words = tokenize_words(content)
    n = len(words)
    doc_word_end = doc_word_start + n - 1 if n else doc_word_start
    page_word_end = page_word_start + n - 1 if n else page_word_start
    para_word_end = para_word_start + n - 1 if n else para_word_start

    leading = " ".join(words[:10])
    trailing = " ".join(words[-10:]) if n > 10 else leading

    meta: dict[str, Any] = {
        "filename": filename,
        "page": page,
        "total_pages": total_pages,
        "page_from_end": total_pages - page + 1,
        "page_position": page_position_label(page, total_pages),
        "paragraph_index": paragraph_index,
        "global_paragraph_index": global_paragraph_index,
        "paragraph_count_page": paragraph_count_page,
        "paragraph_count_doc": paragraph_count_doc,
        "para_position_on_page": para_position_label(paragraph_index, paragraph_count_page),
        "region": region,
        "chunk_kind": chunk_kind,
        "modality": "text",
        # Document-absolute words (1-indexed, primary keys for filters)
        "word_start": doc_word_start,
        "word_end": doc_word_end,
        "doc_word_start": doc_word_start,
        "doc_word_end": doc_word_end,
        # Page-relative words (1-indexed within page)
        "page_word_start": page_word_start,
        "page_word_end": page_word_end,
        # Paragraph-relative words (1-indexed within paragraph)
        "para_word_start": para_word_start,
        "para_word_end": para_word_end,
        # Counts
        "chunk_word_count": n,
        "para_word_count": para_word_count if para_word_count is not None else n,
        "page_word_count": page_word_count,
        "doc_word_count": doc_word_count,
        # Boundary tokens for anchor / highlight disambiguation
        "first_word": words[0] if words else "",
        "last_word": words[-1] if words else "",
        "leading_words": leading,
        "trailing_words": trailing,
    }
    return meta


def parse_position(query: str) -> dict[str, Any]:
    """Extract positional hints from natural language."""
    q = query.lower()
    hints: dict[str, Any] = {}

    # --- Region / layout ---
    if re.search(r"\b(?:in\s+the\s+)?(?:page\s+)?header\b", q):
        hints["region"] = "header"
    elif re.search(r"\b(?:in\s+the\s+)?(?:page\s+)?footer\b", q):
        hints["region"] = "footer"
    elif re.search(r"\btitle\b", q) and "subtitle" not in q:
        hints["region"] = "header"

    # --- Word count questions ---
    if re.search(r"\bhow\s+many\s+words\b", q) or re.search(r"\bword\s+count\b", q):
        hints["wants_word_count"] = True
        if re.search(r"\b(?:whole|entire|full)\s+(?:document|pdf|file|page)\b", q):
            hints["count_scope"] = "document"
        elif re.search(r"\b(?:in\s+)?(?:the\s+)?(?:document|pdf|file)\b", q):
            hints["count_scope"] = "document"
        elif re.search(r"\b(?:this|the)\s+page\b", q) or re.search(r"\bon\s+page\b", q):
            hints["count_scope"] = "page"
        elif "paragraph" in q:
            hints["count_scope"] = "paragraph"
        else:
            hints["count_scope"] = "document"

    # --- Nth word in Mth paragraph (e.g. "third word in second paragraph") ---
    m = re.search(
        rf"\b{_NUM_WORD}\s+word\s+in\s+(?:the\s+)?{_NUM_WORD}\s+paragraph\b", q,
    )
    if m:
        hints["para_word_target"] = _to_int(m.group(1))
        para_num = _to_int(m.group(2))
        if hints.get("page"):
            hints["paragraph_index"] = para_num - 1
        else:
            hints["global_paragraph_index"] = para_num - 1

    # --- First word in a named paragraph (e.g. "first word in the submission instruction paragraph") ---
    m = re.search(r"\bfirst\s+word\s+in\s+(?:the\s+)?(.+?)\s+paragraph\b", q)
    if m and "para_word_target" not in hints:
        hints["para_word_target"] = 1
        hints["anchor_phrase"] = m.group(1).strip()

    # --- Nth word on page / in document ---
    m = re.search(rf"\b{_NUM_WORD}\s+word\s+on\s+(?:the\s+)?page\b", q)
    if m and "para_word_target" not in hints:
        hints["page_word_target"] = _to_int(m.group(1))

    m = re.search(rf"\b{_NUM_WORD}\s+word\s+of\s+(?:the\s+)?(?:document|pdf|file)\b", q)
    if m:
        hints["doc_word_target"] = _to_int(m.group(1))

    # --- Absolute / relative word targets ---
    m = re.search(r"\b(\d+)(?:st|nd|rd|th)?\s+word\b", q)
    if m and "para_word_target" not in hints and "doc_word_target" not in hints:
        hints["word_target"] = int(m.group(1))

    m = re.search(r"\bword\s+(\d+)\b", q)
    if m and "word_target" not in hints and "para_word_target" not in hints:
        hints["word_target"] = int(m.group(1))

    # second-to-last word / third from the end
    m = re.search(rf"\b{_NUM_WORD}\s+(?:to\s+last|from\s+(?:the\s+)?end)\s+word\b", q)
    if m:
        hints["word_from_end"] = _to_int(m.group(1))

    m = re.search(r"\blast\s+word\s+of\s+(?:the\s+)?(?:document|pdf|file)\b", q)
    if m:
        hints["word_from_end"] = 1

    m = re.search(r"\blast\s+word\s+on\s+(?:the\s+)?page\b", q)
    if m:
        hints["page_word_from_end"] = 1

    # --- Word before / after anchor ---
    m = re.search(r"\bword\s+after\s+['\"]?([\w-]+)['\"]?", q)
    if m:
        hints["anchor_word"] = m.group(1)
        hints["anchor_direction"] = "after"

    m = re.search(r"\bword\s+before\s+['\"]?([\w-]+)['\"]?", q)
    if m:
        hints["anchor_word"] = m.group(1)
        hints["anchor_direction"] = "before"

    m = re.search(r"['\"]([\w\s-]{2,40})['\"]\s+word\s+after", q)
    if m:
        hints["anchor_phrase"] = m.group(1).strip()
        hints["anchor_direction"] = "after"

    # --- Paragraph ordinals / positions ---
    for word, idx in _ORDINALS.items():
        if (
            re.search(rf"\b{word}\s+paragraph\b", q)
            and "paragraph_index" not in hints
            and "global_paragraph_index" not in hints
        ):
            if hints.get("page"):
                hints["paragraph_index"] = idx - 1
            else:
                hints["global_paragraph_index"] = idx - 1
            break

    m = re.search(r"\bparagraph\s+(\d+)\s+on\s+page\s+(\d+)\b", q)
    if m:
        hints["paragraph_index"] = int(m.group(1)) - 1
        hints["page"] = int(m.group(2))
    else:
        m = re.search(r"\bparagraph\s+(\d+)\b", q)
        if m and "paragraph_index" not in hints and "global_paragraph_index" not in hints:
            hints["global_paragraph_index"] = int(m.group(1)) - 1

    if re.search(r"\bfirst\s+paragraph\b", q) and "paragraph_index" not in hints and "global_paragraph_index" not in hints:
        if hints.get("page"):
            hints["paragraph_index"] = 0
            hints["para_position_on_page"] = "first"
        else:
            hints["global_paragraph_index"] = 0
    if re.search(r"\blast\s+paragraph\b", q) and "paragraph_index" not in hints and "global_paragraph_index" not in hints:
        if hints.get("page"):
            hints["para_position_on_page"] = "last"
        else:
            hints["global_paragraph_from_end"] = 1

    # --- Page positions ---
    m = re.search(r"\bpage\s+(\d+)\s+from\s+(?:the\s+)?end\b", q)
    if m:
        hints["page_from_end"] = int(m.group(1))
    else:
        m = re.search(r"\b(\d+)(?:st|nd|rd|th)?\s+page\s+from\s+(?:the\s+)?end\b", q)
        if m:
            hints["page_from_end"] = int(m.group(1))

    m = re.search(r"\bpage\s+(\d+)\b", q)
    if m and "page_from_end" not in hints:
        hints["page"] = int(m.group(1))

    if re.search(r"\bfirst\s+page\b", q):
        hints["page"] = 1
    if re.search(r"\blast\s+page\b", q):
        hints["page_from_end"] = 1

    if re.search(r"\b(?:opening|beginning)\s+of\s+(?:the\s+)?(?:document|pdf)\b", q):
        hints["doc_word_target"] = hints.get("doc_word_target", 1)
    if re.search(r"\b(?:end|ending)\s+of\s+(?:the\s+)?(?:document|pdf)\b", q):
        hints["word_from_end"] = hints.get("word_from_end", 1)

    return hints


def format_position_header(meta: dict) -> str:
    """Human-readable source header with full positional detail."""
    parts = [meta.get("filename", ""), f"p.{meta.get('page', '')}/{meta.get('total_pages', '?')}"]
    if meta.get("page_position"):
        parts.append(f"page_{meta['page_position']}")
    if meta.get("paragraph_index") is not None:
        pi = int(meta["paragraph_index"]) + 1
        pc = meta.get("paragraph_count_page")
        parts.append(f"page_para {pi}" + (f"/{pc}" if pc else ""))
    if meta.get("global_paragraph_index") is not None:
        gpi = int(meta["global_paragraph_index"]) + 1
        gdc = meta.get("paragraph_count_doc")
        parts.append(f"doc_para {gpi}" + (f"/{gdc}" if gdc else ""))
    if meta.get("para_position_on_page"):
        parts.append(f"para_{meta['para_position_on_page']}")
    if meta.get("region") and meta["region"] != "body":
        parts.append(meta["region"])
    if meta.get("doc_word_start") and meta.get("doc_word_end"):
        parts.append(f"doc_w {meta['doc_word_start']}-{meta['doc_word_end']}")
    if meta.get("page_word_start") and meta.get("page_word_end"):
        parts.append(f"page_w {meta['page_word_start']}-{meta['page_word_end']}")
    if meta.get("para_word_start") and meta.get("para_word_end"):
        parts.append(f"para_w {meta['para_word_start']}-{meta['para_word_end']}")
    if meta.get("chunk_word_count"):
        parts.append(f"{meta['chunk_word_count']}w")
    if meta.get("chunk_kind"):
        parts.append(f"[{meta['chunk_kind']}]")
    return " | ".join(p for p in parts if p)


def _word_at_doc_position(meta: dict, pos: int) -> str | None:
    text = meta.get("text", "")
    words = tokenize_words(text)
    ds = meta.get("doc_word_start", meta.get("word_start", 1))
    idx = pos - ds
    if 0 <= idx < len(words):
        return words[idx]
    return None


def _chunk_matches_hints(meta: dict, hints: dict) -> bool:
    """Post-filter for relational hints Qdrant cannot express."""
    if not hints:
        return True

    text = meta.get("text", "")
    words = tokenize_words(text)
    if not words:
        return hints.get("anchor_word") is None and hints.get("anchor_phrase") is None

    if hints.get("para_position_on_page"):
        if meta.get("para_position_on_page") != hints["para_position_on_page"]:
            return False

    if hints.get("page_word_target"):
        pwt = hints["page_word_target"]
        pws, pwe = meta.get("page_word_start", 0), meta.get("page_word_end", 0)
        if not (pws <= pwt <= pwe):
            return False

    if hints.get("doc_word_target"):
        dwt = hints["doc_word_target"]
        dws = meta.get("doc_word_start", meta.get("word_start", 0))
        dwe = meta.get("doc_word_end", meta.get("word_end", 0))
        if not (dws <= dwt <= dwe):
            return False

    if hints.get("para_word_target") and hints.get("paragraph_index") is not None:
        if meta.get("paragraph_index") != hints["paragraph_index"]:
            return False
        pwt = hints["para_word_target"]
        pws, pwe = meta.get("para_word_start", 0), meta.get("para_word_end", 0)
        if not (pws <= pwt <= pwe):
            return False

    if hints.get("para_word_target") and hints.get("global_paragraph_index") is not None:
        if meta.get("global_paragraph_index") != hints["global_paragraph_index"]:
            return False
        pwt = hints["para_word_target"]
        pws, pwe = meta.get("para_word_start", 0), meta.get("para_word_end", 0)
        if not (pws <= pwt <= pwe):
            return False

    if hints.get("global_paragraph_index") is not None and hints.get("para_word_target") is None:
        if meta.get("global_paragraph_index") != hints["global_paragraph_index"]:
            return False

    if hints.get("global_paragraph_from_end"):
        gdc = meta.get("paragraph_count_doc")
        gpi = meta.get("global_paragraph_index")
        if gdc is None or gpi is None:
            return False
        if gpi != int(gdc) - hints["global_paragraph_from_end"]:
            return False

    if hints.get("para_word_target") and hints.get("anchor_phrase") and hints.get("paragraph_index") is None:
        phrase = hints["anchor_phrase"].lower()
        if phrase not in text.lower():
            return False
        pwt = hints["para_word_target"]
        pws, pwe = meta.get("para_word_start", 0), meta.get("para_word_end", 0)
        if not (pws <= pwt <= pwe):
            return False

    if hints.get("word_from_end"):
        dwc = meta.get("doc_word_count")
        dwe = meta.get("doc_word_end", meta.get("word_end"))
        if dwc and dwe:
            target = dwc - hints["word_from_end"] + 1
            dws = meta.get("doc_word_start", meta.get("word_start", 0))
            if not (dws <= target <= dwe):
                return False

    if hints.get("page_word_from_end"):
        pwc = meta.get("page_word_count")
        pwe = meta.get("page_word_end")
        if pwc and pwe:
            target = pwc - hints["page_word_from_end"] + 1
            pws = meta.get("page_word_start", 0)
            if not (pws <= target <= pwe):
                return False

    anchor = hints.get("anchor_word") or hints.get("anchor_phrase")
    direction = hints.get("anchor_direction")
    if anchor and direction:
        anchor_clean = anchor.lower()
        if hints.get("anchor_phrase"):
            hay = text.lower()
            pos = hay.find(anchor_clean)
            if pos < 0:
                return False
            after = hay[pos + len(anchor_clean):].lstrip()
            return len(after.split()) > 0 if direction == "after" else pos > 0

        cleaned = [_clean_word(w) for w in words]
        for i, w in enumerate(cleaned):
            if w != _clean_word(anchor):
                continue
            if direction == "after" and i + 1 < len(words):
                return True
            if direction == "before" and i > 0:
                return True
        return False

    return True


def post_filter_hits(hits: list, hints: dict) -> list:
    """Keep hits matching relational positional hints; fall back to all if none match."""
    if not hints or not hits:
        return hits
    needs_post = any(
        hints.get(k) is not None
        for k in (
            "anchor_word", "anchor_phrase", "anchor_direction",
            "para_word_target", "doc_word_target", "page_word_target",
            "word_from_end", "page_word_from_end", "para_position_on_page",
            "paragraph_index", "global_paragraph_index", "global_paragraph_from_end",
        )
    )
    if hints.get("para_word_target") and hints.get("anchor_phrase"):
        needs_post = True
    if not needs_post:
        return hits
    filtered = [h for h in hits if _chunk_matches_hints(h.payload, hints)]
    return filtered if filtered else hits


def boost_hits_by_position(hits: list, hints: dict) -> list:
    """Re-order hits so positional matches rank higher."""
    if not hints or not hits:
        return hits

    def score(hit) -> float:
        pay = hit.payload
        s = 0.0
        if hints.get("region") and pay.get("region") == hints["region"]:
            s += 3.0
        if hints.get("paragraph_index") is not None and pay.get("paragraph_index") == hints["paragraph_index"]:
            s += 2.0
        if hints.get("global_paragraph_index") is not None and pay.get("global_paragraph_index") == hints["global_paragraph_index"]:
            s += 2.0
        if hints.get("page") and pay.get("page") == hints["page"]:
            s += 1.5
        if hints.get("word_target"):
            wt = hints["word_target"]
            if pay.get("doc_word_start", 0) <= wt <= pay.get("doc_word_end", 0):
                s += 2.5
        if hints.get("para_word_target"):
            pwt = hints["para_word_target"]
            if pay.get("para_word_start", 0) <= pwt <= pay.get("para_word_end", 0):
                s += 2.5
        if hints.get("anchor_phrase"):
            phrase = hints["anchor_phrase"].lower()
            if phrase in pay.get("text", "").lower():
                s += 3.5
                if pay.get("chunk_kind") == "paragraph":
                    s += 1.0
        if _chunk_matches_hints(pay, hints):
            s += 1.0
        return s

    return sorted(hits, key=score, reverse=True)


def word_count_answer(hits: list, hints: dict) -> str | None:
    """If the user asked for a word count, return a precise answer from metadata."""
    if not hints.get("wants_word_count") or not hits:
        return None
    scope = hints.get("count_scope", "document")

    pay = hits[0].payload
    if scope == "paragraph":
        if hints.get("global_paragraph_index") is not None:
            for h in hits:
                p = h.payload
                if p.get("global_paragraph_index") == hints["global_paragraph_index"]:
                    pay = p
                    break
        elif hints.get("paragraph_index") is not None:
            for h in hits:
                p = h.payload
                if p.get("paragraph_index") == hints["paragraph_index"]:
                    pay = p
                    break

    if scope == "page" and hints.get("page"):
        for h in hits:
            p = h.payload
            if p.get("page") == hints["page"]:
                pay = p
                break

    if scope == "document" and pay.get("doc_word_count"):
        return f"The document contains **{pay['doc_word_count']}** words (precise count at ingest)."
    if scope == "page" and pay.get("page_word_count"):
        return (
            f"Page {pay.get('page', '?')} contains **{pay['page_word_count']}** words "
            f"(precise count at ingest)."
        )
    if scope == "paragraph" and pay.get("para_word_count"):
        if hints.get("global_paragraph_index") is not None:
            gpi = int(hints["global_paragraph_index"]) + 1
            return (
                f"Document paragraph {gpi} (page {pay.get('page', '?')}) contains "
                f"**{pay['para_word_count']}** words (precise count at ingest)."
            )
        pi = int(pay.get("paragraph_index", 0)) + 1
        pc = pay.get("paragraph_count_page")
        page_para = f"Paragraph {pi}" + (f"/{pc}" if pc else "")
        return (
            f"{page_para} on page {pay.get('page', '?')} contains "
            f"**{pay['para_word_count']}** words (precise count at ingest)."
        )
    if pay.get("doc_word_count"):
        return f"The document contains **{pay['doc_word_count']}** words (precise count at ingest)."
    return None
