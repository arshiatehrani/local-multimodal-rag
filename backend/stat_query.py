"""Deterministic fast-path answers for ingest-time document statistics."""

from __future__ import annotations

import re
from typing import Any

from document_stats import PUNCTUATION_KEYS, format_stats_context_block, stats_from_payload

# Flexible count-intent prefixes (tolerate tweaked wording).
COUNT_LEAD = (
    r"(?:how many|how much|what(?:'s| is) the(?: total)?(?: number| count)? of|"
    r"what(?:'s| is)|what is|number of|count of|count(?:\s+(?:the|all))?|"
    r"total(?:\s+number)? of|tell me (?:the )?(?:number|count) of|"
    r"give me (?:the )?(?:number|count) of|can you (?:tell|count)|"
    r"do you know (?:the )?how many|what(?:'s| is) the count)"
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
    "exclamation point": "!",
    "exclamation points": "!",
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
    "left parenthesis": "(",
    "left parentheses": "(",
    "right parenthesis": ")",
    "right parentheses": ")",
    "open parenthesis": "(",
    "close parenthesis": ")",
}

_PUNCT_KEY_BY_CHAR = {ch: key for key, ch in PUNCTUATION_KEYS if key not in ("quote_single",)}

# Heads that correspond to ingest-time structural stats (not arbitrary document topics).
_STRUCTURAL_HEADS = frozenset({
    "document", "file", "pdf", "text", "page", "paragraph", "line",
    "word", "character", "char", "letter", "digit", "whitespace", "space",
    "comma", "period", "semicolon", "colon", "mark", "apostrophe", "hyphen",
    "quote", "paren", "parenthesis", "punctuation", "stat", "stats",
    "statistics", "total", "all", "whole", "entire", "full", "non", "no",
})
for _name in _PUNCT_CHAR_NAMES:
    _STRUCTURAL_HEADS = _STRUCTURAL_HEADS | frozenset(_name.split())

_STRUCTURAL_PHRASE = re.compile(
    r"^(?:"
    r"(?:whole|entire|full|this|the|a|an|non[\s-]?)?"
    r"(?:document|file|pdf|text|page|paragraph|line|word|character|char|letter|digit|whitespace|space|punctuation|statistics?)"
    r"(?:\s+\d+)?"
    r"|(?:first|second|third|fourth|fifth|last|\d+(?:st|nd|rd|th)?)\s+(?:page|paragraph|line)"
    r"|(?:page|paragraph|line)\s+\d+"
    r"|no[\s-]?space\s+characters?"
    r"|non[\s-]?whitespace\s+characters?"
    r")$",
    re.I,
)

_COUNT_PREP = re.compile(
    r"(?:"
    r"(?:word|character|char|paragraph|line)s?\s+(?:count|limit|length)\s+"
    r"|how\s+many\s+(?:\w+\s+)*"
    r"|(?:number|count)\s+of\s+"
    r")"
    r"(?:of|for|in|about|inside|within)\s+"
    r"(?:the\s+|this\s+|a\s+|an\s+|our\s+|your\s+)?"
    r"(.+?)"
    r"(?=\?|$|\.\s|\s+(?:that\b|which\b|we\b|I\b|in\s+the\s+doc\b))",
    re.I,
)

_CLARIFICATION = re.compile(
    r"^(?:no|nope|nah)[,!\s]+|^(?:i\s+)?(?:mean|meant)\b|\bnot\s+what\s+i\s+(?:asked|meant)\b",
    re.I,
)

_STAT_HINT_KEYS = (
    "stat_metric", "wants_word_count", "wants_paragraph_count",
    "wants_char_count", "wants_total_char_count", "char_target", "char_case_insensitive",
)


def _clear_stat_hints(hints: dict[str, Any]) -> None:
    for key in _STAT_HINT_KEYS:
        hints.pop(key, None)


def _normalize_target_phrase(phrase: str) -> str:
    s = phrase.strip().lower()
    s = re.sub(r"^(?:whole|entire|full|this|the|a|an|our|your)\s+", "", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s


def _is_structural_count_target(phrase: str) -> bool:
    """True when the count target is a file/page/paragraph/inventory dimension we store at ingest."""
    s = _normalize_target_phrase(phrase)
    if not s:
        return True
    if _STRUCTURAL_PHRASE.match(s):
        return True
    words = s.split()
    if len(words) == 1:
        head = words[0].rstrip("s")
        return head in _STRUCTURAL_HEADS
    if len(words) == 2 and words[0] in {"non", "no"} and "space" in words[1]:
        return True
    return False


def _extract_count_targets(q: str) -> list[str]:
    targets = [m.group(1).strip() for m in _COUNT_PREP.finditer(q)]
    m = re.search(
        r"\bhow\s+many\s+(?:words?|characters?|chars?|paragraphs?|lines?|digits?)\s+"
        r"(?:should|is|are|was|were|does|do|did|will|would|can|must)\s+"
        r"(?:the\s+|this\s+|a\s+|an\s+)?(.+?)\s+(?:be|require|need|take|have)\b",
        q,
        re.I,
    )
    if m:
        targets.append(m.group(1).strip())
    return targets


def is_ingest_stat_eligible(q: str, hints: dict[str, Any] | None = None) -> bool:
    """
    Fast-path only when the question targets structural ingest stats.

    Rule: ingest stores counts for the file shell (document/page/paragraph) and
    character inventory (letters, punctuation, whitespace). Any count scoped via
    of/for/in/about to a non-structural noun phrase is content inside the document
    and must go through retrieval + the LLM.
    """
    if not q or not q.strip():
        return False
    if _CLARIFICATION.search(q.strip()):
        return False
    targets = _extract_count_targets(q)
    if targets:
        return all(_is_structural_count_target(t) for t in targets)
    return True


# Backward-compatible alias
is_document_level_stat_query = is_ingest_stat_eligible


def _is_positional_word_query(hints: dict[str, Any]) -> bool:
    return any(
        hints.get(k)
        for k in (
            "word_target", "para_word_target", "doc_word_target", "page_word_target",
            "word_from_end", "page_word_from_end", "anchor_word", "anchor_phrase",
        )
    )


def set_stat_count_scope(q: str, hints: dict[str, Any]) -> None:
    if hints.get("count_scope"):
        return
    if hints.get("page") and re.search(r"\bparagraphs?\b", q):
        hints["count_scope"] = "page"
    elif hints.get("global_paragraph_index") is not None or hints.get("paragraph_index") is not None:
        hints["count_scope"] = "paragraph"
    elif hints.get("page") and re.search(
        r"\b(?:lines?|words?|characters?|chars?|digits?|commas?|periods?)\b", q
    ):
        hints["count_scope"] = "page"
    elif re.search(r"\b(?:on|in)\s+(?:the\s+)?page\b", q):
        hints["count_scope"] = "page"
    elif re.search(r"\b(?:this|the)\s+page\b", q) and not re.search(
        r"\b(?:document|pdf|file|text)\b", q
    ):
        hints["count_scope"] = "page"
    elif re.search(r"\b(?:whole|entire|full)\s+(?:document|pdf|file|text)\b", q):
        hints["count_scope"] = "document"
    elif re.search(r"\b(?:in|inside|within)\s+(?:the\s+)?(?:this\s+)?(?:document|pdf|file|text)\b", q):
        hints["count_scope"] = "document"
    elif re.search(r"\b(?:this|the)\s+(?:document|pdf|file|text)\b", q):
        hints["count_scope"] = "document"
    elif "paragraph" in q and re.search(r"\b(?:in|inside|within)\s+(?:the\s+)?paragraph\b", q):
        hints["count_scope"] = "paragraph"
    else:
        hints["count_scope"] = "document"


def _set_metric(hints: dict[str, Any], metric: str, q: str) -> None:
    if not is_ingest_stat_eligible(q, hints):
        return
    hints["stat_metric"] = metric
    set_stat_count_scope(q, hints)
    if metric == "word":
        hints["wants_word_count"] = True
    elif metric == "paragraph":
        hints["wants_paragraph_count"] = True
    elif metric == "char":
        hints["wants_char_count"] = True
        hints["wants_total_char_count"] = True
    elif metric.startswith("letter:"):
        hints["wants_char_count"] = True
        hints["char_target"] = metric.split(":", 1)[1]
        hints["char_case_insensitive"] = True
    elif metric.startswith("punct:"):
        hints["wants_char_count"] = True
        key = metric.split(":", 1)[1]
        hints["char_target"] = dict(PUNCTUATION_KEYS)[key] if key in dict(PUNCTUATION_KEYS) else _PUNCT_CHAR_NAMES.get(key, ",")


def parse_stat_count_hints(q: str, hints: dict[str, Any]) -> None:
    """Detect count questions answerable from ingest statistics."""
    if _is_positional_word_query(hints):
        return

    # Full stats summary
    if re.search(
        r"\b(?:document|file|pdf)\s+statistics\b|\bstatistics(?:\s+for|\s+of|\s+about)?\s+(?:the\s+)?(?:document|file|pdf|text)\b",
        q,
    ) or re.search(rf"\b{COUNT_LEAD}\s+(?:all\s+)?(?:the\s+)?stats\b", q):
        _set_metric(hints, "summary", q)
        return

    # Characters excluding whitespace (before generic "characters")
    if re.search(
        rf"\b{COUNT_LEAD}\s+(?:characters?|chars?)\s+(?:excluding|without|minus)\s+(?:spaces?|whitespace)\b",
        q,
    ) or re.search(rf"\b{COUNT_LEAD}\s+(?:non[\s-]?whitespace|no[\s-]?space)\s+characters?\b", q):
        _set_metric(hints, "char_no_space", q)
        return

    # Whitespace
    if re.search(r"\bhow much\s+whitespace\b", q):
        _set_metric(hints, "whitespace", q)
        return
    if re.search(rf"\b{COUNT_LEAD}\s+(?:whitespace|white\s+space)\s+characters?\b", q):
        _set_metric(hints, "whitespace", q)
        return
    if re.search(rf"\b{COUNT_LEAD}\s+spaces?\b", q):
        _set_metric(hints, "whitespace", q)
        return

    # Quoted single character
    m = re.search(rf"\b{COUNT_LEAD}\s+['\"](.)['\"]", q)
    if m:
        ch = m.group(1)
        if ch.isspace():
            _set_metric(hints, "whitespace", q)
            return
        if ch.isalpha():
            _set_metric(hints, f"letter:{ch.lower()}", q)
        else:
            key = _PUNCT_KEY_BY_CHAR.get(ch)
            _set_metric(hints, f"punct:{key}" if key else "char", q)
            if not key:
                hints["char_target"] = ch
        return

    # Letter patterns
    m = re.search(rf"\b{COUNT_LEAD}\s+(?:the\s+)?letters?\s+([a-z])\b", q)
    if m:
        _set_metric(hints, f"letter:{m.group(1)}", q)
        return
    m = re.search(rf"\b{COUNT_LEAD}\s+([a-z])\s+letters?\b", q)
    if m:
        _set_metric(hints, f"letter:{m.group(1)}", q)
        return
    m = re.search(rf"\b{COUNT_LEAD}\s+([a-z])['']s\b", q)
    if m:
        _set_metric(hints, f"letter:{m.group(1)}", q)
        return
    m = re.search(rf"\b{COUNT_LEAD}\s+([a-z])\s+characters?\b", q)
    if m:
        _set_metric(hints, f"letter:{m.group(1)}", q)
        return
    if re.search(rf"\b{COUNT_LEAD}\s+letter\s+frequenc", q):
        _set_metric(hints, "summary", q)
        return

    # Named punctuation
    for name in sorted(_PUNCT_CHAR_NAMES.keys(), key=len, reverse=True):
        if re.search(rf"\b{COUNT_LEAD}\s+{re.escape(name)}\b", q):
            ch = _PUNCT_CHAR_NAMES[name]
            key = _PUNCT_KEY_BY_CHAR.get(ch, "comma")
            _set_metric(hints, f"punct:{key}", q)
            return
    if re.search(rf"\b{COUNT_LEAD}\s+punctuation\s+marks?\b", q):
        _set_metric(hints, "punct_all", q)
        return

    # Digits
    if re.search(rf"\b{COUNT_LEAD}\s+(?:digits?|numeric\s+digits?)\b", q):
        _set_metric(hints, "digit", q)
        return

    # Lines
    if re.search(rf"\b{COUNT_LEAD}\s+lines?\b", q) or re.search(r"\bline\s+count\b", q):
        _set_metric(hints, "line", q)
        return

    # Paragraphs
    if re.search(rf"\b{COUNT_LEAD}\s+paragraphs?\b", q) or re.search(r"\bparagraph\s+count\b", q):
        _set_metric(hints, "paragraph", q)
        return

    # Words
    if re.search(rf"\b{COUNT_LEAD}\s+words?\b", q) or re.search(r"\bword\s+count\b", q):
        _set_metric(hints, "word", q)
        return
    if re.search(r"\btotal\s+words?\b", q) and re.search(r"\b(?:document|pdf|file|text)\b", q):
        _set_metric(hints, "word", q)
        return

    # Characters (total)
    if re.search(rf"\b{COUNT_LEAD}\s+(?:characters?|chars?)\b", q) or re.search(r"\bcharacter\s+count\b", q):
        _set_metric(hints, "char", q)


def resolve_stat_metric(hints: dict[str, Any]) -> str | None:
    if hints.get("stat_metric"):
        return hints["stat_metric"]
    if hints.get("wants_word_count"):
        return "word"
    if hints.get("wants_paragraph_count"):
        return "paragraph"
    if hints.get("wants_total_char_count"):
        return "char"
    if hints.get("char_target"):
        ch = hints["char_target"]
        if len(ch) == 1 and ch.isalpha():
            return f"letter:{ch.lower()}"
        key = _PUNCT_KEY_BY_CHAR.get(ch)
        if key:
            return f"punct:{key}"
    return None


def _space_files(space_id: str) -> list[dict]:
    import spaces as _spaces
    try:
        data = _spaces.get_space(space_id)
        return [f for f in data.get("files", []) if f.get("text_stats")]
    except Exception:
        return []


def _doc_paragraph_count(space_id: str, file_id: str, stats: dict) -> int:
    from qdrant_store import scroll_payloads
    for pay in scroll_payloads(space_id, chunk_kind="document_stats", modality="text"):
        if pay.get("file_id") == file_id:
            if pay.get("paragraph_count_doc") is not None:
                return int(pay["paragraph_count_doc"])
            if pay.get("doc_paragraph_count") is not None:
                return int(pay["doc_paragraph_count"])
    return int(stats.get("paragraph_count", 0))


def _page_paragraph_count(space_id: str, page: int) -> int | None:
    from qdrant_store import scroll_payloads
    chunks = [
        p for p in scroll_payloads(space_id, chunk_kind="paragraph", modality="text")
        if p.get("page") == page
    ]
    if not chunks:
        return None
    indices = {
        p.get("global_paragraph_index") for p in chunks
        if p.get("global_paragraph_index") is not None
    }
    if indices:
        return len(indices)
    pc = chunks[0].get("paragraph_count_page")
    return int(pc) if pc is not None else len(chunks)


def _page_payload(space_id: str, page: int) -> dict | None:
    from qdrant_store import scroll_payloads
    for pay in scroll_payloads(space_id, chunk_kind="page_full", modality="text"):
        if pay.get("page") == page:
            return pay
    return None


def _metric_value(
    stats: dict,
    metric: str,
    *,
    space_id: str = "",
    file_id: str = "",
) -> tuple[int, str]:
    if metric == "word":
        return int(stats.get("word_count", 0)), "words"
    if metric == "char":
        return int(stats.get("char_count", 0)), "characters"
    if metric == "char_no_space":
        return int(stats.get("char_count_no_space", 0)), "characters excluding whitespace"
    if metric == "whitespace":
        return int(stats.get("whitespace_count", 0)), "whitespace characters"
    if metric == "line":
        return int(stats.get("line_count", 0)), "lines"
    if metric == "paragraph":
        return _doc_paragraph_count(space_id, file_id, stats), "paragraphs"
    if metric == "digit":
        return int(stats.get("digit_count", 0)), "digits"
    if metric.startswith("letter:"):
        ch = metric.split(":", 1)[1]
        n = int((stats.get("letter_counts") or {}).get(ch, 0))
        return n, f'letter "{ch.upper()}"'
    if metric.startswith("punct:"):
        key = metric.split(":", 1)[1]
        n = int((stats.get("punctuation") or {}).get(key, 0))
        label = key.replace("_", " ")
        return n, label + ("s" if n != 1 else "")
    if metric == "punct_all":
        total = sum(int(v) for v in (stats.get("punctuation") or {}).values())
        return total, "punctuation marks"
    return 0, metric


def _file_source(f: dict, stats: dict, highlight: str) -> dict:
    name = f.get("original_name", "file")
    return {
        "file_id": f.get("file_id", ""),
        "filename": name,
        "page": 1,
        "modality": "text",
        "chunk_kind": "document_stats",
        "text": format_stats_context_block(name, stats),
        "highlight_mode": "",
        "highlight_phrases": [highlight],
    }


def _format_count_answer(count: int, label: str, scope_phrase: str) -> str:
    if count == 1 and not label.endswith("s") and "excluding" not in label:
        return f"There is **1** {label} in {scope_phrase} (precise count at ingest)."
    if label.endswith("s") or "characters" in label or "marks" in label:
        return f"There are **{count}** {label} in {scope_phrase} (precise count at ingest)."
    return f"There are **{count}** {label} in {scope_phrase} (precise count at ingest)."


def _summary_answer(stats: dict, filename: str, space_id: str, file_id: str) -> str:
    wc = int(stats.get("word_count", 0))
    cc = int(stats.get("char_count", 0))
    no_ws = int(stats.get("char_count_no_space", 0))
    ws = int(stats.get("whitespace_count", 0))
    lines = int(stats.get("line_count", 0))
    paras = _doc_paragraph_count(space_id, file_id, stats)
    digits = int(stats.get("digit_count", 0))
    p = stats.get("punctuation") or {}
    letters = stats.get("letter_counts") or {}
    top = sorted(letters.items(), key=lambda x: -x[1])[:6]
    top_line = ", ".join(f"{k}={v}" for k, v in top) if top else "n/a"
    return (
        f"**Document statistics for {filename}** (computed at ingest):\n\n"
        f"- **Words:** {wc}\n"
        f"- **Characters:** {cc} ({no_ws} excluding whitespace, {ws} whitespace)\n"
        f"- **Lines:** {lines}\n"
        f"- **Paragraphs:** {paras}\n"
        f"- **Digits:** {digits}\n"
        f"- **Punctuation:** commas={p.get('comma', 0)}, periods={p.get('period', 0)}, "
        f"question marks={p.get('question_mark', 0)}, exclamation marks={p.get('exclamation', 0)}, "
        f"hyphens={p.get('hyphen', 0)}, apostrophes={p.get('apostrophe', 0)}\n"
        f"- **Top letters:** {top_line}\n\n"
        f"Counts are from PDF text extracted at ingest and may differ slightly from Word or Google Docs."
    )


def build_stat_count_response(space_id: str, hints: dict) -> tuple[str, list[dict]] | None:
    """Answer a single stat-metric question from ingest statistics."""
    metric = resolve_stat_metric(hints)
    if not metric:
        return None

    files = _space_files(space_id)
    if not files:
        return (
            "No document statistics are available yet. Re-upload PDFs to compute precise counts.",
            [],
        )

    scope = hints.get("count_scope", "document")

    if scope == "page":
        page = hints.get("page")
        if page is None:
            return None
        pay = _page_payload(space_id, page)
        if not pay:
            return None
        stats = stats_from_payload(pay, "page")
        if metric == "paragraph":
            count = _page_paragraph_count(space_id, page)
            if count is None:
                return None
            label = "paragraphs"
        else:
            count, label = _metric_value(stats, metric, space_id=space_id, file_id=pay.get("file_id", ""))
        answer = _format_count_answer(count, label, f"page {page}")
        src = _file_source(
            {"file_id": pay.get("file_id", ""), "original_name": pay.get("filename", "")},
            stats,
            f"{count} {label}",
        )
        src["page"] = page
        return answer, [src]

    if scope == "paragraph":
        from positioning import word_count
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
        gpi = int(pay.get("global_paragraph_index", 0)) + 1
        scope_phrase = f"paragraph {gpi} (page {pay.get('page', '?')})"
        if metric == "word":
            count = int(pay.get("para_word_count") or word_count(pay.get("text", "")))
            label = "words"
        else:
            stats = stats_from_payload(pay, "page")
            count, label = _metric_value(stats, metric, space_id=space_id, file_id=pay.get("file_id", ""))
        answer = _format_count_answer(count, label, scope_phrase)
        src = {
            "file_id": pay.get("file_id", ""),
            "filename": pay.get("filename", ""),
            "page": pay.get("page", 1),
            "modality": pay.get("modality", "text"),
            "chunk_kind": pay.get("chunk_kind", "paragraph"),
            "text": (pay.get("text") or "")[:240],
            "highlight_mode": "",
            "highlight_phrases": [f"{count} {label}"],
        }
        return answer, [src]

    if metric == "summary" and len(files) == 1:
        f = files[0]
        stats = f["text_stats"]
        answer = _summary_answer(stats, f.get("original_name", "file"), space_id, f.get("file_id", ""))
        return answer, [_file_source(f, stats, "document statistics")]

    if metric == "summary":
        lines = [f"**Statistics for {len(files)} files** (precise counts at ingest):"]
        sources = []
        for f in files:
            stats = f["text_stats"]
            wc = int(stats.get("word_count", 0))
            cc = int(stats.get("char_count", 0))
            paras = _doc_paragraph_count(space_id, f.get("file_id", ""), stats)
            name = f.get("original_name", "file")
            lines.append(f"- **{name}:** {wc} words, {cc} characters, {paras} paragraphs")
            sources.append(_file_source(f, stats, "statistics"))
        return "\n".join(lines), sources

    if len(files) == 1:
        f = files[0]
        stats = f["text_stats"]
        count, label = _metric_value(stats, metric, space_id=space_id, file_id=f.get("file_id", ""))
        scope_phrase = "the document"
        if metric == "word":
            answer = (
                f"The document contains **{count}** words "
                f"({int(stats.get('char_count', 0))} characters — precise counts at ingest)."
            )
        else:
            answer = _format_count_answer(count, label, scope_phrase)
        return answer, [_file_source(f, stats, f"{count} {label}")]

    total = 0
    lines = [f"This space contains **{len(files)}** files:"]
    sources = []
    for f in files:
        stats = f["text_stats"]
        count, label = _metric_value(stats, metric, space_id=space_id, file_id=f.get("file_id", ""))
        total += count
        name = f.get("original_name", "file")
        lines.append(f"- **{name}:** **{count}** {label}")
        sources.append(_file_source(f, stats, f"{count} {label}"))
    lines.append(f"\n**Total: {total}** {label} across all files.")
    return "\n".join(lines), sources


def try_metadata_stat_response(
    space_id: str, hints: dict, query: str = "",
) -> tuple[str, list[dict]] | None:
    """Fast path for ingest-stat questions — no LLM."""
    if query and not is_ingest_stat_eligible(query, hints):
        return None
    metric = resolve_stat_metric(hints)
    if not metric:
        return None

    scope = hints.get("count_scope", "document")
    if hints.get("char_target") and scope in ("page", "paragraph"):
        from positioning import build_char_count_sources
        from qdrant_store import get_text_chunks_for_count
        chunks = get_text_chunks_for_count(space_id, hints)
        if chunks:
            return build_char_count_sources(chunks, hints)

    return build_stat_count_response(space_id, hints)
