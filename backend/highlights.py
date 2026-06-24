"""Answer-aware highlight phrase computation for source cards.

Extracted from query.py — these helpers pick short, precise spans from retrieved
chunks that align with the generated answer, so the frontend can highlight them
in the PDF viewer and source cards.
"""

import re

from positioning import tokenize_words


def _normalize_text(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[\u200c\u200d]", "", s)
    s = re.sub(r"[^\w\s\u0600-\u06FF]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


_QUERY_STOP = frozenset({
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for", "of", "with",
    "is", "are", "was", "were", "be", "been", "being", "have", "has", "had", "do",
    "does", "did", "will", "would", "could", "should", "may", "might", "must",
    "what", "which", "who", "whom", "this", "that", "these", "those", "it", "its",
    "about", "tell", "give", "show", "find", "from", "into", "your", "you", "me",
    "document", "pdf", "file", "page", "paragraph", "word", "summary", "summarize",
})


def query_terms(query: str) -> list[str]:
    terms = []
    for w in re.findall(r"[\w\u0600-\u06FF]+", query.lower()):
        if len(w) > 2 and w not in _QUERY_STOP:
            terms.append(w)
    return terms


def _extract_numbers(text: str) -> list[str]:
    return re.findall(r"\d[\d,./:-]*\d|\d+", text or "")


def _number_in_chunk(num: str, chunk: str) -> str | None:
    """Return the chunk's literal spelling of a number if present."""
    if not num or not chunk:
        return None
    variants = {num, num.replace(",", ""), num.replace(".", "")}
    for v in variants:
        if not v:
            continue
        m = re.search(re.escape(v), chunk)
        if m:
            return m.group()
    norm_chunk = _normalize_text(chunk)
    norm_num = _normalize_text(num)
    if norm_num and norm_num in norm_chunk.split():
        for tok in chunk.split():
            if _normalize_text(tok).strip(".,;:") == norm_num:
                return tok.strip(".,;:")
    return None


def _find_phrase_in_chunk(chunk: str, norm_phrase: str) -> str | None:
    if not norm_phrase or not chunk:
        return None
    phrase_words = norm_phrase.split()
    if not phrase_words:
        return None
    words = chunk.split()
    norm_words = [_normalize_text(w) for w in words]
    n = len(phrase_words)
    for i in range(len(norm_words) - n + 1):
        if norm_words[i:i + n] == phrase_words:
            return " ".join(words[i:i + n])
    if len(norm_phrase) >= 3 and norm_phrase in _normalize_text(chunk):
        m = re.search(re.escape(norm_phrase), _normalize_text(chunk))
        if m:
            return m.group()
    return None


def _dedupe_phrases(phrases: list[str]) -> list[str]:
    seen: set = set()
    out = []
    for p in phrases:
        p = (p or "").strip()
        if not p:
            continue
        key = _normalize_text(p)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def extract_word_at_hint(pay: dict, hints: dict) -> str | None:
    """Return the exact word at a parsed position, if unambiguous."""
    text = pay.get("text", "")
    words = tokenize_words(text)
    if not words:
        return None
    dws = pay.get("doc_word_start", pay.get("word_start", 1))

    if hints.get("para_word_target") and hints.get("paragraph_index") == pay.get("paragraph_index"):
        idx = hints["para_word_target"] - pay.get("para_word_start", 1)
        if 0 <= idx < len(words):
            return words[idx]

    if hints.get("para_word_target") and hints.get("global_paragraph_index") == pay.get("global_paragraph_index"):
        idx = hints["para_word_target"] - pay.get("para_word_start", 1)
        if 0 <= idx < len(words):
            return words[idx]

    if hints.get("para_word_target") and hints.get("anchor_phrase"):
        phrase = hints["anchor_phrase"].lower()
        if phrase in pay.get("text", "").lower():
            idx = hints["para_word_target"] - pay.get("para_word_start", 1)
            if 0 <= idx < len(words):
                return words[idx]

    for key, target_key in (("doc_word_target", "doc_word_target"), ("word_target", "word_target")):
        if hints.get(target_key):
            idx = hints[target_key] - dws
            if 0 <= idx < len(words):
                return words[idx]

    if hints.get("anchor_word") and hints.get("anchor_direction") == "after":
        anchor = hints["anchor_word"].lower()
        for i, w in enumerate(words):
            if w.lower().strip(".,;:") == anchor and i + 1 < len(words):
                return words[i + 1]
    if hints.get("anchor_word") and hints.get("anchor_direction") == "before":
        anchor = hints["anchor_word"].lower()
        for i, w in enumerate(words):
            if w.lower().strip(".,;:") == anchor and i > 0:
                return words[i - 1]
    return None


def compute_highlight_phrases(
    pay: dict,
    query: str,
    answer: str,
    pos_hints: dict,
    exact_word: str | None = None,
) -> list[str]:
    """Pick short, precise spans to highlight — numbers, dates, answer-aligned phrases."""
    chunk = pay.get("text", "") or ""
    if not chunk.strip():
        return []

    phrases: list[str] = []

    if exact_word:
        found = _find_phrase_in_chunk(chunk, _normalize_text(exact_word)) or exact_word
        if found.lower() in chunk.lower() or _normalize_text(found) in _normalize_text(chunk):
            return [found]

    hinted = extract_word_at_hint(pay, pos_hints)
    if hinted:
        phrases.append(hinted)

    for num in _extract_numbers(answer):
        lit = _number_in_chunk(num, chunk)
        if lit:
            phrases.append(lit)

    for m in re.finditer(
        r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
        r"\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{2,4}",
        answer,
        re.I,
    ):
        lit = m.group()
        if lit.lower() in chunk.lower():
            phrases.append(lit)

    chunk_norm = _normalize_text(chunk)
    for sent in re.split(r"[.!?\n]+", answer):
        sent = sent.strip()
        if len(sent) < 10:
            continue
        sent_norm = _normalize_text(sent)
        words = sent_norm.split()
        matched = False
        for n in range(min(10, len(words)), 1, -1):
            if matched:
                break
            for i in range(len(words) - n + 1):
                sub = " ".join(words[i:i + n])
                if len(sub) < 6:
                    continue
                found = _find_phrase_in_chunk(chunk, sub)
                if found:
                    phrases.append(found)
                    matched = True
                    break

    for term in query_terms(query):
        if term in chunk_norm:
            found = _find_phrase_in_chunk(chunk, term)
            if found:
                phrases.append(found)

    if not phrases and pay.get("leading_words"):
        lead = pay["leading_words"][:80].strip()
        if lead:
            phrases.append(lead)

    return _dedupe_phrases(phrases)[:12]
