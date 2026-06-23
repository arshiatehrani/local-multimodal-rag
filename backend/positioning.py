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

    _parse_char_count_hints(q, hints)

    return hints


_COUNT_LEAD = (
    r"(?:how many|number of|count(?:\s+(?:the|all))?|total(?:\s+number)?\s+of|"
    r"tell me the number of)"
)

_PUNCT_CHAR_NAMES: dict[str, str] = {
    "comma": ",",
    "commas": ",",
    "period": ".",
    "periods": ".",
    "dot": ".",
    "dots": ".",
    "full stop": ".",
    "full stops": ".",
    "semicolon": ";",
    "semicolons": ";",
    "colon": ":",
    "colons": ":",
    "question mark": "?",
    "question marks": "?",
    "exclamation mark": "!",
    "exclamation marks": "!",
    "apostrophe": "'",
    "apostrophes": "'",
    "hyphen": "-",
    "hyphens": "-",
    "dash": "-",
    "dashes": "-",
    "quotation mark": '"',
    "quotation marks": '"',
    "quote": '"',
    "quotes": '"',
    "space": " ",
    "spaces": " ",
}


def _parse_char_count_hints(q: str, hints: dict[str, Any]) -> None:
    """Detect character/punctuation count questions (commas, letter s, etc.)."""
    if hints.get("wants_char_count"):
        return

    # Quoted single character: how many "," / number of 's'
    m = re.search(rf"\b{_COUNT_LEAD}\s+['\"](.)['\"]", q)
    if m:
        ch = m.group(1)
        hints["wants_char_count"] = True
        hints["char_target"] = ch
        hints["char_case_insensitive"] = ch.isalpha()
        _set_char_count_scope(q, hints)
        return

    # Named punctuation: how many commas
    for name in sorted(_PUNCT_CHAR_NAMES.keys(), key=len, reverse=True):
        if re.search(rf"\b{_COUNT_LEAD}\s+{re.escape(name)}\b", q):
            hints["wants_char_count"] = True
            hints["char_target"] = _PUNCT_CHAR_NAMES[name]
            hints["char_case_insensitive"] = False
            _set_char_count_scope(q, hints)
            return

    # "letter s" / "the letter a"
    m = re.search(rf"\b{_COUNT_LEAD}\s+(?:the\s+)?letter\s+([a-z])\b", q)
    if m:
        hints["wants_char_count"] = True
        hints["char_target"] = m.group(1)
        hints["char_case_insensitive"] = True
        _set_char_count_scope(q, hints)
        return

    # "s letters" / "s letter"
    m = re.search(rf"\b{_COUNT_LEAD}\s+([a-z])\s+letters?\b", q)
    if m:
        hints["wants_char_count"] = True
        hints["char_target"] = m.group(1)
        hints["char_case_insensitive"] = True
        _set_char_count_scope(q, hints)
        return

    # "s's" / "number of s's"
    m = re.search(rf"\b{_COUNT_LEAD}\s+([a-z])['']s\b", q)
    if m:
        hints["wants_char_count"] = True
        hints["char_target"] = m.group(1)
        hints["char_case_insensitive"] = True
        _set_char_count_scope(q, hints)
        return

    if re.search(rf"\b{_COUNT_LEAD}\s+characters?\b", q) and "char_target" not in hints:
        hints["wants_char_count"] = True
        hints["wants_total_char_count"] = True
        _set_char_count_scope(q, hints)


def _set_char_count_scope(q: str, hints: dict[str, Any]) -> None:
    if hints.get("count_scope"):
        return
    if re.search(r"\b(?:this|the)\s+page\b", q) or (
        hints.get("page") and "paragraph" not in q
    ):
        hints["count_scope"] = "page"
    elif "paragraph" in q:
        hints["count_scope"] = "paragraph"
    elif re.search(r"\b(?:whole|entire|full)\s+(?:document|pdf|file|text)\b", q):
        hints["count_scope"] = "document"
    elif re.search(r"\b(?:in\s+)?(?:the\s+)?(?:document|pdf|file|text)\b", q):
        hints["count_scope"] = "document"
    else:
        hints["count_scope"] = "document"


def char_target_label(char: str) -> str:
    if not char:
        return "character"
    if char == ",":
        return "comma"
    if char == ".":
        return "period"
    if char == " ":
        return "space"
    for name, ch in _PUNCT_CHAR_NAMES.items():
        if ch == char and not name.endswith("s"):
            return name
    if len(char) == 1 and char.isalpha():
        return f'letter "{char.upper()}"'
    return f'"{char}"'


def count_character_matches(text: str, char: str, case_insensitive: bool = False) -> int:
    if not text or not char:
        return 0
    if len(char) == 1 and char.isalpha() and case_insensitive:
        target = char.lower()
        return sum(1 for c in text if c.isalpha() and c.lower() == target)
    return text.count(char)


def _payload_matches_scope(pay: dict, hints: dict) -> bool:
    if hints.get("page") and pay.get("page") != hints["page"]:
        return False
    if hints.get("page_from_end") and pay.get("page_from_end") != hints["page_from_end"]:
        return False
    if hints.get("region") and pay.get("region") != hints["region"]:
        return False
    if hints.get("global_paragraph_index") is not None:
        if pay.get("global_paragraph_index") != hints["global_paragraph_index"]:
            return False
    if hints.get("paragraph_index") is not None:
        if pay.get("paragraph_index") != hints["paragraph_index"]:
            return False
    if hints.get("global_paragraph_from_end"):
        gdc = pay.get("paragraph_count_doc")
        gpi = pay.get("global_paragraph_index")
        if gdc is None or gpi is None:
            return False
        if gpi != int(gdc) - hints["global_paragraph_from_end"]:
            return False
    return True


def _dedupe_text_chunks(payloads: list[dict], scope: str) -> list[dict]:
    """Pick one canonical text blob per page or paragraph (avoid window overlap)."""
    if scope == "paragraph":
        seen: set = set()
        out = []
        for pay in sorted(
            payloads,
            key=lambda p: (
                p.get("file_id", ""),
                p.get("global_paragraph_index", p.get("paragraph_index", 0)),
            ),
        ):
            key = (
                pay.get("file_id"),
                pay.get("global_paragraph_index"),
                pay.get("paragraph_index"),
            )
            if key in seen:
                continue
            seen.add(key)
            if pay.get("chunk_kind") == "window":
                continue
            out.append(pay)
        return out

    # page / document — prefer page_full, else longest paragraph per page
    by_page: dict[tuple, dict] = {}
    for pay in payloads:
        if pay.get("modality") != "text":
            continue
        key = (pay.get("file_id"), pay.get("page"))
        existing = by_page.get(key)
        if pay.get("chunk_kind") == "page_full":
            by_page[key] = pay
            continue
        if existing and existing.get("chunk_kind") == "page_full":
            continue
        if not existing or len(pay.get("text", "")) > len(existing.get("text", "")):
            by_page[key] = pay
    return sorted(
        by_page.values(),
        key=lambda p: (p.get("file_id", ""), int(p.get("page") or 0)),
    )


def filter_chunks_for_char_count(payloads: list[dict], hints: dict) -> list[dict]:
    scope = hints.get("count_scope", "document")
    text_payloads = [
        p for p in payloads
        if p.get("modality") == "text" and (p.get("text") or "").strip()
    ]
    filtered = [p for p in text_payloads if _payload_matches_scope(p, hints)]
    if scope == "paragraph":
        return _dedupe_text_chunks(filtered, "paragraph")
    if scope == "page":
        return _dedupe_text_chunks(filtered, "page")
    return _dedupe_text_chunks(filtered, "document")


def build_char_count_sources(chunks: list[dict], hints: dict) -> tuple[str, list[dict]]:
    """Return (answer markdown, source dicts with char highlight metadata)."""
    char = hints.get("char_target", "")
    case_insensitive = bool(hints.get("char_case_insensitive"))
    label = char_target_label(char)
    scope = hints.get("count_scope", "document")

    total = 0
    sources: list[dict] = []
    for pay in chunks:
        text = pay.get("text", "") or ""
        n = count_character_matches(text, char, case_insensitive)
        if n <= 0:
            continue
        total += n
        plural = label + ("s" if n != 1 and not label.startswith("letter") else "")
        if label.startswith("letter"):
            plural = label + ("" if n == 1 else " (matches)")
        sources.append({
            "file_id": pay.get("file_id", ""),
            "filename": pay.get("filename", ""),
            "page": pay.get("page", ""),
            "total_pages": pay.get("total_pages"),
            "modality": "text",
            "chunk_kind": pay.get("chunk_kind", ""),
            "text": text[:240] + ("…" if len(text) > 240 else ""),
            "highlight_mode": "chars",
            "highlight_chars": [char],
            "char_case_insensitive": case_insensitive,
            "char_match_count": n,
            "char_target_label": label,
            "highlight_phrases": [],
        })

    scope_phrase = {
        "document": "the document text",
        "page": f"page {hints.get('page', chunks[0].get('page') if chunks else '?')}",
        "paragraph": "the selected paragraph",
    }.get(scope, "the text")

    if total == 0:
        answer = (
            f"There are **0** {label}s in {scope_phrase} "
            f"(precise count from ingested text)."
        )
    elif total == 1:
        answer = (
            f"There is **1** {label} in {scope_phrase} "
            f"(precise count from ingested text)."
        )
    else:
        answer = (
            f"There are **{total}** {label}s in {scope_phrase} "
            f"(precise count from ingested text)."
        )

    if len(sources) > 1:
        per_page = ", ".join(
            f"p.{s['page']}: {s['char_match_count']}" for s in sources[:12]
        )
        if len(sources) > 12:
            per_page += ", …"
        answer += f" Breakdown by page — {per_page}."

    answer += " Open each source card to see every match highlighted in the PDF."

    return answer, sources


def build_total_char_count_response(space_id: str, hints: dict) -> tuple[str, list[dict]]:
    """Deterministic total character count from stored ingest statistics."""
    from document_stats import format_stats_context_block
    import spaces as _spaces

    try:
        data = _spaces.get_space(space_id)
        files = [f for f in data.get("files", []) if f.get("text_stats")]
    except Exception:
        files = []

    scope = hints.get("count_scope", "document")
    if not files:
        return (
            "No document statistics are available yet. Re-upload PDFs to compute precise counts.",
            [],
        )

    if scope == "document" and len(files) == 1:
        stats = files[0]["text_stats"]
        answer = (
            f"The document contains **{stats['char_count']}** characters "
            f"({stats['char_count_no_space']} excluding whitespace, "
            f"{stats['whitespace_count']} whitespace) and "
            f"**{stats['word_count']}** words (precise counts at ingest)."
        )
    elif scope == "document":
        chars = sum(int(f["text_stats"].get("char_count", 0)) for f in files)
        no_ws = sum(int(f["text_stats"].get("char_count_no_space", 0)) for f in files)
        words = sum(int(f["text_stats"].get("word_count", 0)) for f in files)
        answer = (
            f"This space contains **{chars}** characters "
            f"({no_ws} excluding whitespace) and **{words}** words "
            f"across {len(files)} file(s) (precise counts at ingest)."
        )
    else:
        answer = (
            "Character totals for a specific page or paragraph are best asked with "
            "'how many characters on page N' after re-uploading with page statistics."
        )

    sources = []
    for f in files:
        stats = f["text_stats"]
        sources.append({
            "file_id": f.get("file_id", ""),
            "filename": f.get("original_name", ""),
            "page": 1,
            "modality": "text",
            "chunk_kind": "document_stats",
            "text": format_stats_context_block(f.get("original_name", ""), stats),
            "highlight_mode": "",
            "highlight_phrases": [f"{stats.get('char_count', 0)} characters"],
            "char_match_count": stats.get("char_count"),
            "char_target_label": "character total",
        })
    return answer, sources


def build_word_count_response(space_id: str, hints: dict) -> tuple[str, list[dict]] | None:
    """Deterministic word count from ingest statistics — bypasses the LLM."""
    from document_stats import format_stats_context_block
    import spaces as _spaces

    scope = hints.get("count_scope", "document")

    def _file_source(f: dict, stats: dict) -> dict:
        name = f.get("original_name", "file")
        wc = int(stats.get("word_count", 0))
        return {
            "file_id": f.get("file_id", ""),
            "filename": name,
            "page": 1,
            "modality": "text",
            "chunk_kind": "document_stats",
            "text": format_stats_context_block(name, stats),
            "highlight_mode": "",
            "highlight_phrases": [f"{wc} words"],
        }

    def _chunk_source(pay: dict, label: str) -> dict:
        return {
            "file_id": pay.get("file_id", ""),
            "filename": pay.get("filename", ""),
            "page": pay.get("page", 1),
            "modality": pay.get("modality", "text"),
            "chunk_kind": pay.get("chunk_kind", ""),
            "text": (pay.get("text") or "")[:240],
            "highlight_mode": "",
            "highlight_phrases": [label],
        }

    if scope == "document":
        try:
            data = _spaces.get_space(space_id)
            files = [f for f in data.get("files", []) if f.get("text_stats")]
        except Exception:
            files = []
        if not files:
            return (
                "No word-count statistics are available yet. Re-upload PDFs to compute precise counts.",
                [],
            )
        if len(files) == 1:
            stats = files[0]["text_stats"]
            wc = int(stats.get("word_count", 0))
            cc = int(stats.get("char_count", 0))
            answer = (
                f"The document contains **{wc}** words "
                f"({cc} characters — precise counts computed at ingest)."
            )
            return answer, [_file_source(files[0], stats)]
        lines = [f"This space contains **{len(files)}** files with the following word counts:"]
        sources = []
        total = 0
        for f in files:
            stats = f["text_stats"]
            wc = int(stats.get("word_count", 0))
            total += wc
            name = f.get("original_name", "file")
            lines.append(f"- **{name}**: **{wc}** words")
            sources.append(_file_source(f, stats))
        lines.append(f"\n**Total: {total} words** across all files.")
        return "\n".join(lines), sources

    if scope == "page":
        from qdrant_store import scroll_payloads

        page = hints.get("page")
        if page is None:
            return None
        payloads = [
            p for p in scroll_payloads(space_id, chunk_kind="page_full", modality="text")
            if p.get("page") == page
        ]
        if not payloads:
            return None
        pay = payloads[0]
        pwc = pay.get("page_word_count")
        if pwc is None:
            pwc = word_count(pay.get("text", ""))
        return (
            f"Page {page} contains **{pwc}** words (precise count at ingest).",
            [_chunk_source(pay, f"{pwc} words")],
        )

    if scope == "paragraph":
        from qdrant_store import scroll_payloads

        payloads = scroll_payloads(space_id, chunk_kind="paragraph", modality="text")
        if hints.get("global_paragraph_index") is not None:
            gpi = hints["global_paragraph_index"]
            payloads = [p for p in payloads if p.get("global_paragraph_index") == gpi]
        elif hints.get("paragraph_index") is not None:
            pi = hints["paragraph_index"]
            page = hints.get("page")
            payloads = [
                p for p in payloads
                if p.get("paragraph_index") == pi
                and (page is None or p.get("page") == page)
            ]
        else:
            return None
        if not payloads:
            return None
        pay = payloads[0]
        pwc = pay.get("para_word_count") or word_count(pay.get("text", ""))
        gpi = int(pay.get("global_paragraph_index", 0)) + 1
        return (
            f"Paragraph {gpi} (page {pay.get('page', '?')}) contains **{pwc}** words "
            f"(precise count at ingest).",
            [_chunk_source(pay, f"{pwc} words")],
        )

    return None


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


def word_count_answer(hits: list, hints: dict, space_id: str | None = None) -> str | None:
    """If the user asked for a word count, return a precise answer from ingest metadata."""
    if not hints.get("wants_word_count"):
        return None
    scope = hints.get("count_scope", "document")

    if space_id and scope == "document":
        try:
            import spaces as _spaces
            data = _spaces.get_space(space_id)
            totals = [f.get("text_stats") for f in data.get("files", []) if f.get("text_stats")]
            if len(totals) == 1:
                s = totals[0]
                return (
                    f"The document contains **{s['word_count']}** words and "
                    f"**{s['char_count']}** characters "
                    f"({s['char_count_no_space']} excluding whitespace) "
                    f"(precise counts at ingest)."
                )
            if totals:
                words = sum(int(t.get("word_count", 0)) for t in totals)
                chars = sum(int(t.get("char_count", 0)) for t in totals)
                return (
                    f"This space contains **{words}** words and **{chars}** characters "
                    f"across {len(totals)} text file(s) (precise counts at ingest)."
                )
        except Exception:
            pass

    if not hits:
        return None

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
        extra = ""
        if pay.get("doc_char_count"):
            extra = (
                f" and **{pay['doc_char_count']}** characters "
                f"({pay.get('doc_char_count_no_space', '?')} excluding whitespace)"
            )
        return (
            f"The document contains **{pay['doc_word_count']}** words{extra} "
            f"(precise count at ingest)."
        )
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
