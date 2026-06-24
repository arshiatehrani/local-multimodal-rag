"""Grounded identifier extraction and drift correction.

Alphanumeric codes (course numbers, project IDs, term codes) are extracted from
filenames and retrieved chunks, then used to:
  1. Inject an EXACT IDENTIFIERS context block so the model copies them verbatim.
  2. Post-correct digit drift (e.g. the model writes "912" when the source says "812").

Extracted from query.py for modularity.
"""

import re

import spaces

# Letter-prefix + digit codes (course numbers, project IDs, etc.) from source text.
_IDENTIFIER_RE = re.compile(r"\b([A-Z]{2,6})[\s\-\u2013\u2014]*(\d{2,4}[A-Z]?)\b")
_TERM_CODE_RE = re.compile(r"\b([SWF]\d{2})\b", re.I)


def _normalize_for_identifier_scan(text: str) -> str:
    return (text or "").replace("\u2013", "-").replace("\u2014", "-")


def _collect_identifiers_from_text(text: str, seen: set[str], out: list[str]) -> None:
    text = _normalize_for_identifier_scan(text)
    for m in _IDENTIFIER_RE.finditer(text):
        spaced = f"{m.group(1)} {m.group(2)}"
        compact = f"{m.group(1)}{m.group(2)}"
        for token in (spaced, compact):
            if token not in seen:
                seen.add(token)
                out.append(token)
    for m in _TERM_CODE_RE.finditer(text):
        token = m.group(1).upper()
        if token not in seen:
            seen.add(token)
            out.append(token)


def extract_grounded_identifiers(hits: list, space_id: str) -> list[str]:
    """Alphanumeric labels present in filenames and retrieved chunks (not invented)."""
    seen: set[str] = set()
    out: list[str] = []
    try:
        data = spaces.get_space(space_id)
        for f in data.get("files", []):
            _collect_identifiers_from_text(f.get("original_name", ""), seen, out)
    except Exception:
        pass
    for hit in hits:
        pay = hit.payload or {}
        _collect_identifiers_from_text(pay.get("text", ""), seen, out)
        _collect_identifiers_from_text(pay.get("filename", ""), seen, out)
    return out


def grounded_identifiers_context(identifiers: list[str]) -> str:
    if not identifiers:
        return ""
    lines = [
        "[EXACT IDENTIFIERS — alphanumeric codes and labels from the documents; "
        "copy spelling and digits verbatim; never substitute other numbers]",
    ]
    for token in identifiers[:40]:
        lines.append(f"- {token}")
    return "\n".join(lines)


def _identifier_numbers_by_prefix(identifiers: list[str]) -> dict[str, set[str]]:
    by_prefix: dict[str, set[str]] = {}
    for token in identifiers:
        m = _IDENTIFIER_RE.search(_normalize_for_identifier_scan(token))
        if not m:
            continue
        by_prefix.setdefault(m.group(1), set()).add(m.group(2))
    return by_prefix


def fix_identifier_drift(answer: str, identifiers: list[str]) -> str:
    """When the model swaps digits on a known prefix (812→912), restore source forms."""
    if not answer or not identifiers:
        return answer
    by_prefix = _identifier_numbers_by_prefix(identifiers)
    if not by_prefix:
        return answer

    def repl(match: re.Match) -> str:
        prefix, num = match.group(1), match.group(2)
        valid = by_prefix.get(prefix)
        if not valid or num in valid:
            return match.group(0)
        if len(valid) != 1:
            return match.group(0)
        correct = next(iter(valid))
        # Safely slice to replace group(2) with correct digits, preserving original separator.
        start = match.start(2) - match.start(0)
        return match.group(0)[:start] + correct

    return _IDENTIFIER_RE.sub(repl, answer)
