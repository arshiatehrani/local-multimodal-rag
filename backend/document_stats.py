"""Precise document-level text statistics computed once at ingest."""

from __future__ import annotations

import json
import re
from typing import Any

# Punctuation keys stored as {prefix}_comma_count, etc.
PUNCTUATION_KEYS = (
    ("comma", ","),
    ("period", "."),
    ("semicolon", ";"),
    ("colon", ":"),
    ("question_mark", "?"),
    ("exclamation", "!"),
    ("apostrophe", "'"),
    ("hyphen", "-"),
    ("quote_double", '"'),
    ("quote_single", "'"),
    ("left_paren", "("),
    ("right_paren", ")"),
)


def compute_text_stats(text: str) -> dict[str, Any]:
    """Authoritative counts for a text blob (same tokenizer as positional word queries)."""
    from positioning import tokenize_words

    if not text:
        return _empty_stats()

    words = tokenize_words(text)
    char_count = len(text)
    whitespace_count = sum(1 for c in text if c.isspace())
    char_count_no_space = char_count - whitespace_count

    stripped = text.strip()
    paragraph_count = len([p for p in re.split(r"\n\s*\n+", stripped) if p.strip()]) if stripped else 0
    line_count = len(text.splitlines()) if text else 0
    if stripped and line_count == 0:
        line_count = 1

    punctuation: dict[str, int] = {}
    for key, ch in PUNCTUATION_KEYS:
        if key in ("quote_single", "apostrophe"):
            continue
        if key == "quote_double":
            punctuation[key] = text.count('"') + text.count("\u201c") + text.count("\u201d")
        else:
            punctuation[key] = text.count(ch)
    punctuation["apostrophe"] = text.count("'") + text.count("\u2019")
    punctuation["quote_single"] = punctuation["apostrophe"]

    letter_counts: dict[str, int] = {}
    for c in text:
        if c.isalpha():
            letter_counts[c.lower()] = letter_counts.get(c.lower(), 0) + 1

    digit_count = sum(1 for c in text if c.isdigit())

    return {
        "word_count": len(words),
        "char_count": char_count,
        "char_count_no_space": char_count_no_space,
        "whitespace_count": whitespace_count,
        "line_count": line_count,
        "paragraph_count": paragraph_count,
        "digit_count": digit_count,
        "punctuation": punctuation,
        "letter_counts": letter_counts,
    }


def _empty_stats() -> dict[str, Any]:
    return {
        "word_count": 0,
        "char_count": 0,
        "char_count_no_space": 0,
        "whitespace_count": 0,
        "line_count": 0,
        "paragraph_count": 0,
        "digit_count": 0,
        "punctuation": {k: 0 for k, _ in PUNCTUATION_KEYS},
        "letter_counts": {},
    }


def flatten_stats(prefix: str, stats: dict[str, Any]) -> dict[str, int]:
    """Flatten stats dict into Qdrant-friendly integer payload fields."""
    out: dict[str, int] = {
        f"{prefix}_word_count": int(stats.get("word_count", 0)),
        f"{prefix}_char_count": int(stats.get("char_count", 0)),
        f"{prefix}_char_count_no_space": int(stats.get("char_count_no_space", 0)),
        f"{prefix}_whitespace_count": int(stats.get("whitespace_count", 0)),
        f"{prefix}_line_count": int(stats.get("line_count", 0)),
        f"{prefix}_paragraph_count": int(stats.get("paragraph_count", 0)),
        f"{prefix}_digit_count": int(stats.get("digit_count", 0)),
    }
    for key, _ in PUNCTUATION_KEYS:
        out[f"{prefix}_{key}_count"] = int(stats.get("punctuation", {}).get(key, 0))
    letters = stats.get("letter_counts") or {}
    for ch in "abcdefghijklmnopqrstuvwxyz":
        out[f"{prefix}_letter_{ch}_count"] = int(letters.get(ch, 0))
    return out


def attach_stats_to_meta(
    meta: dict[str, Any],
    doc_stats: dict[str, Any] | None,
    page_stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge document/page statistics into a chunk payload."""
    if doc_stats:
        meta.update(flatten_stats("doc", doc_stats))
        meta["doc_letter_counts_json"] = json.dumps(
            doc_stats.get("letter_counts") or {}, separators=(",", ":"),
        )
    if page_stats:
        meta.update(flatten_stats("page", page_stats))
    return meta


def build_stats_summary_text(
    filename: str,
    doc_stats: dict[str, Any],
    *,
    total_pages: int = 0,
    paragraph_count_doc: int = 0,
) -> str:
    """Human-readable stats block embedded as its own retrieval chunk."""
    p = doc_stats.get("punctuation") or {}
    letters = doc_stats.get("letter_counts") or {}
    top_letters = sorted(letters.items(), key=lambda x: -x[1])[:8]
    letter_line = ", ".join(f"{k}={v}" for k, v in top_letters) if top_letters else "n/a"

    lines = [
        f"DOCUMENT STATISTICS for {filename} (authoritative counts computed at ingest):",
        f"- Words: {doc_stats.get('word_count', 0)}",
        f"- Characters: {doc_stats.get('char_count', 0)} "
        f"({doc_stats.get('char_count_no_space', 0)} excluding whitespace, "
        f"{doc_stats.get('whitespace_count', 0)} whitespace)",
        f"- Lines: {doc_stats.get('line_count', 0)}; "
        f"paragraphs: {paragraph_count_doc or doc_stats.get('paragraph_count', 0)}",
    ]
    if total_pages:
        lines.append(f"- Pages: {total_pages}")
    lines.append(
        "- Punctuation: "
        f"commas={p.get('comma', 0)}, periods={p.get('period', 0)}, "
        f"semicolons={p.get('semicolon', 0)}, colons={p.get('colon', 0)}, "
        f"question_marks={p.get('question_mark', 0)}, exclamation_marks={p.get('exclamation', 0)}, "
        f"hyphens={p.get('hyphen', 0)}, apostrophes={p.get('apostrophe', 0)}"
    )
    lines.append(f"- Top letters (frequency): {letter_line}")
    lines.append(
        "Use these exact numbers for word counts, character counts, punctuation counts, "
        "and letter-frequency questions."
    )
    return "\n".join(lines)


def format_stats_context_block(filename: str, stats: dict[str, Any]) -> str:
    """Compact stats block injected into every RAG prompt."""
    p = stats.get("punctuation") or {}
    return (
        f"{filename}: "
        f"{stats.get('word_count', 0)} words, "
        f"{stats.get('char_count', 0)} characters "
        f"({stats.get('char_count_no_space', 0)} non-whitespace), "
        f"{stats.get('line_count', 0)} lines, "
        f"{stats.get('paragraph_count', 0)} paragraphs; "
        f"commas={p.get('comma', 0)}, periods={p.get('period', 0)}, "
        f"question_marks={p.get('question_mark', 0)}, "
        f"letter_s={stats.get('letter_counts', {}).get('s', 0)}"
    )


def format_chunk_stats_line(pay: dict) -> str:
    """One-line stats reminder attached to each retrieved chunk."""
    parts = []
    if pay.get("doc_word_count") is not None:
        parts.append(f"doc_words={pay['doc_word_count']}")
    if pay.get("doc_char_count") is not None:
        parts.append(f"doc_chars={pay['doc_char_count']}")
    if pay.get("page_word_count") is not None:
        parts.append(f"page_words={pay['page_word_count']}")
    if pay.get("page_char_count") is not None:
        parts.append(f"page_chars={pay['page_char_count']}")
    if pay.get("doc_comma_count") is not None:
        parts.append(f"doc_commas={pay['doc_comma_count']}")
    if pay.get("doc_letter_s_count") is not None:
        parts.append(f"doc_letter_s={pay['doc_letter_s_count']}")
    return "Stats: " + ", ".join(parts) if parts else ""


def stats_from_payload(pay: dict, scope: str = "doc") -> dict[str, Any]:
    """Reconstruct a stats dict from flattened payload fields."""
    prefix = scope
    stats = {
        "word_count": pay.get(f"{prefix}_word_count", 0),
        "char_count": pay.get(f"{prefix}_char_count", 0),
        "char_count_no_space": pay.get(f"{prefix}_char_count_no_space", 0),
        "whitespace_count": pay.get(f"{prefix}_whitespace_count", 0),
        "line_count": pay.get(f"{prefix}_line_count", 0),
        "paragraph_count": pay.get(f"{prefix}_paragraph_count", 0),
        "digit_count": pay.get(f"{prefix}_digit_count", 0),
        "punctuation": {},
        "letter_counts": {},
    }
    for key, _ in PUNCTUATION_KEYS:
        stats["punctuation"][key] = pay.get(f"{prefix}_{key}_count", 0)
    raw = pay.get(f"{prefix}_letter_counts_json")
    if raw:
        try:
            stats["letter_counts"] = json.loads(raw)
        except json.JSONDecodeError:
            pass
    if not stats["letter_counts"]:
        stats["letter_counts"] = {
            ch: int(pay.get(f"{prefix}_letter_{ch}_count", 0))
            for ch in "abcdefghijklmnopqrstuvwxyz"
            if pay.get(f"{prefix}_letter_{ch}_count")
        }
    return stats
