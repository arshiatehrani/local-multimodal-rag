"""Query pipeline: embed -> hybrid search -> rerank -> generate (with chat history)."""

import asyncio
import re
from threading import Thread

import numpy as np

import spaces
from document_stats import format_chunk_stats_line, format_stats_context_block
from model_manager import manager
from positioning import (
    format_position_header,
    post_filter_hits,
    boost_hits_by_position,
    word_count_answer,
    tokenize_words,
)
from stat_query import try_metadata_stat_response, resolve_stat_metric
from qdrant_store import hybrid_search, count_points
from rag_context import (
    MAX_CONTEXT_TOKENS,
    estimate_tokens,
    parse_position,
    prepare_chat_history,
    pack_retrieval_chunks,
    context_status,
    context_for_turn,
)

QUERY_INSTRUCTION = "Retrieve relevant documents for the query."
RERANK_INSTRUCTION = "Retrieve images or text relevant to the user's query."
MAX_NEW_TOKENS = 1024
SUMMARY_MAX_NEW_TOKENS = 512
META_MAX_NEW_TOKENS = 256
# Only skip rerank for tiny corpora with simple positional/count queries (not summaries).
SKIP_RERANK_MAX_POINTS = int(__import__("os").environ.get("SKIP_RERANK_MAX_POINTS", "4"))
GEN_KWARGS = {
    "do_sample": False,
    "repetition_penalty": 1.15,
    "no_repeat_ngram_size": 4,
}

# If the query mentions document structure, always use RAG — never treat as meta.
_DOC_TOPIC = re.compile(
    r"\b(paragraph|page|document|assignment|part\s*\d|chapter|section|"
    r"pdf|file|due|submit|rubric|grade|word\s+\d+|second\s+paragraph)\b",
    re.I,
)


_CHITCHAT_WORDS = frozenset({
    "hi", "hii", "hey", "heyy", "heyyy", "hello", "helloo", "yo", "sup", "howdy",
    "greetings", "thanks", "thankyou", "ok", "okay", "cool", "nice", "great",
    "bye", "goodbye", "morning", "evening", "afternoon",
})


def _is_casual_greeting(query: str) -> bool:
    """Match hi/hey/hello variants (heyy, hiii) and other short greetings."""
    q = query.strip().lower()
    q = re.sub(r"[!?.…,]+$", "", q).strip()
    if not q or len(q) > 40:
        return False
    compact = re.sub(r"[\s'\"]+", "", q)
    if re.fullmatch(r"h+i+", compact) or re.fullmatch(r"h+e+y+", compact):
        return True
    if re.fullmatch(r"h+e+l+o+", compact):
        return True
    if compact in _CHITCHAT_WORDS:
        return True
    if re.fullmatch(r"(thanks|thankyou|goodmorning|goodevening|goodafternoon)", compact):
        return True
    return False


def _is_conversational_meta(query: str) -> bool:
    """Greetings / language-capability questions that should not use document RAG."""
    q = query.strip()
    if not q or len(q) > 140:
        return False

    # Document questions always go through retrieval, even if they contain "can you…"
    if _DOC_TOPIC.search(q):
        return False

    lower = q.lower()

    # Explicit language-capability (English)
    if re.search(
        r"(can|do)\s+you\s+(speak|write|read|understand)\s+"
        r"(farsi|persian|arabic|english|french|spanish|german|mandarin|any\s+language)",
        lower,
    ):
        return True
    if re.search(r"(can|do)\s+you\s+(speak|understand)\s+(any\s+)?languages?\b", lower):
        return True
    if re.search(r"what\s+(language|languages)\b", lower):
        return True

    # Short greetings / thanks (incl. heyy, hii, yo)
    if _is_casual_greeting(q):
        return True
    if re.search(r"^(hi|hello|hey|thanks|thank you)\b", lower) and len(q) < 50:
        return True

    # Persian / Farsi capability or greeting
    if re.search(r"\b(farsi|persian|فارسی)\b", q, re.I):
        if "?" in q or "؟" in q or re.search(r"(can|speak|talk|حرف|می)", q, re.I):
            return True

    if re.search(r"[\u0600-\u06FF]", q):
        if re.search(r"(می[\u200c]?تون|می[\u200c]?توانی|بله|سلام|فارسی|زبان|حرف)", q):
            if "?" in q or "؟" in q or len(q) < 70:
                return True
        if re.search(r"^(سلام|درود)\b", q):
            return True

    return False


def _meta_fast_answer(query: str) -> str | None:
    """Deterministic one-line answers for common meta questions (no RAG, no long generation)."""
    q = query.strip()
    if not _is_conversational_meta(q):
        return None

    has_persian = bool(re.search(r"[\u0600-\u06FF]", q))
    if re.search(r"\b(farsi|persian|فارسی)\b", q, re.I) or (
        has_persian and re.search(r"(می[\u200c]?تون|حرف|زبان|فارسی)", q)
    ):
        return "بله، می‌توانم به فارسی پاسخ دهم." if has_persian else "Yes, I can respond in Farsi/Persian."

    if _is_casual_greeting(q) or re.search(r"^(hi|hello|hey)\b", q, re.I):
        return "Hello! Ask me anything about the files in this space."
    if re.search(r"^(thanks|thank you)\b", q, re.I):
        return "You're welcome!"
    if re.search(r"^(سلام|درود)\b", q):
        return "سلام! هر سوالی دربارهٔ فایل‌های این فضا دارید بپرسید."

    if re.search(r"what\s+(language|languages)", q, re.I):
        return "I reply in whichever language you write in, including English and Persian/Farsi."

    return None


def _top_k_for_space(n_points: int, overview: bool = False) -> tuple[int, int]:
    """Return (retrieve_k, final_k) based on corpus size in this space."""
    if n_points <= 12:
        retrieve, final = 24, min(8, n_points)
        if overview:
            final = min(4, n_points)  # overview: fewer, distinct passages beat many duplicates
        return retrieve, final
    if n_points <= 30:
        return 30, 8
    return 40, 5


def _chunk_dedupe_key(hit) -> tuple:
    pay = hit.payload if hasattr(hit, "payload") else hit
    return (
        pay.get("file_id"),
        pay.get("page"),
        pay.get("paragraph_index"),
        pay.get("chunk_kind"),
        pay.get("doc_word_start", pay.get("word_start")),
    )


def _source_card_key(pay: dict) -> tuple:
    """Unique key per retrieved chunk (including sliding windows)."""
    return (
        pay.get("file_id"),
        pay.get("page"),
        pay.get("paragraph_index"),
        pay.get("chunk_kind"),
        pay.get("doc_word_start", pay.get("word_start")),
    )


_QUERY_STOP = frozenset({
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for", "of", "with",
    "is", "are", "was", "were", "be", "been", "being", "have", "has", "had", "do",
    "does", "did", "will", "would", "could", "should", "may", "might", "must",
    "what", "which", "who", "whom", "this", "that", "these", "those", "it", "its",
    "about", "tell", "give", "show", "find", "from", "into", "your", "you", "me",
    "document", "pdf", "file", "page", "paragraph", "word", "summary", "summarize",
})


def _normalize_text(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[\u200c\u200d]", "", s)
    s = re.sub(r"[^\w\s\u0600-\u06FF]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _query_terms(query: str) -> list[str]:
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


def _compute_highlight_phrases(
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

    hinted = _extract_word_at_hint(pay, pos_hints)
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

    for term in _query_terms(query):
        if term in chunk_norm:
            found = _find_phrase_in_chunk(chunk, term)
            if found:
                phrases.append(found)

    if not phrases and pay.get("leading_words"):
        lead = pay["leading_words"][:80].strip()
        if lead:
            phrases.append(lead)

    return _dedupe_phrases(phrases)[:12]


def _payload_to_source(pay: dict, highlight_phrases: list[str] | None = None) -> dict:
    return {
        "file_id": pay.get("file_id", ""),
        "filename": pay.get("filename", ""),
        "page": pay.get("page", ""),
        "total_pages": pay.get("total_pages"),
        "paragraph_index": pay.get("paragraph_index"),
        "global_paragraph_index": pay.get("global_paragraph_index"),
        "paragraph_count_page": pay.get("paragraph_count_page"),
        "paragraph_count_doc": pay.get("paragraph_count_doc"),
        "para_position_on_page": pay.get("para_position_on_page", ""),
        "word_start": pay.get("doc_word_start", pay.get("word_start")),
        "word_end": pay.get("doc_word_end", pay.get("word_end")),
        "page_word_start": pay.get("page_word_start"),
        "page_word_end": pay.get("page_word_end"),
        "para_word_start": pay.get("para_word_start"),
        "para_word_end": pay.get("para_word_end"),
        "doc_word_count": pay.get("doc_word_count"),
        "page_word_count": pay.get("page_word_count"),
        "para_word_count": pay.get("para_word_count"),
        "region": pay.get("region", ""),
        "chunk_kind": pay.get("chunk_kind", ""),
        "modality": pay.get("modality", ""),
        "thumbnail": pay.get("thumbnail_b64", ""),
        "text": pay.get("text", ""),
        "highlight_phrases": highlight_phrases or [],
        "highlight_mode": pay.get("highlight_mode", ""),
        "highlight_chars": pay.get("highlight_chars") or [],
        "char_case_insensitive": bool(pay.get("char_case_insensitive")),
        "char_match_count": pay.get("char_match_count"),
        "char_target_label": pay.get("char_target_label", ""),
    }


def _collect_sources(
    packed_hits: list,
    query: str,
    answer: str,
    pos_hints: dict,
    exact_word: str | None = None,
) -> list:
    """One source card per chunk sent to the generator, with answer-aware highlights."""
    seen: set = set()
    sources = []
    for hit in packed_hits:
        key = _chunk_dedupe_key(hit)
        if key in seen:
            continue
        seen.add(key)
        pay = hit.payload if hasattr(hit, "payload") else hit
        highlights = _compute_highlight_phrases(pay, query, answer, pos_hints, exact_word)
        sources.append(_payload_to_source(pay, highlights))
    return sources


def _dedupe_hits(hits: list) -> list:
    """Drop near-duplicate chunks (same paragraph/window) — keeps context clean."""
    seen: set = set()
    out = []
    for h in hits:
        key = _chunk_dedupe_key(h)
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
    return out


def _should_skip_rerank(n_points: int, query: str, pos_hints: dict) -> bool:
    """Skip rerank only for tiny corpora + simple positional/count queries."""
    if _is_overview_query(query):
        return False
    if n_points > SKIP_RERANK_MAX_POINTS:
        return False
    if resolve_stat_metric(pos_hints):
        return True
    if pos_hints.get("wants_char_count"):
        return True
    if (
        pos_hints.get("paragraph_index") is not None
        or pos_hints.get("global_paragraph_index") is not None
        or pos_hints.get("word_target")
    ):
        return True
    if pos_hints.get("anchor_phrase") or pos_hints.get("anchor_word"):
        return True
    return False


def _is_overview_query(query: str) -> bool:
    q = query.lower().strip()
    return bool(re.search(
        r"what('s| is)?\s+(this|the|it)\s+(document|pdf|file|paper|assignment|text)\s+(is\s+)?about|"
        r"what\s+(this|the)\s+(document|pdf|file|paper|assignment|text)\s+(is\s+)?about|"
        r"what\s+is\s+(this|the)\s+about|"
        r"summarize|summary|overview|main\s+(idea|topic|point|theme)|"
        r"what\s+does\s+(this|the|it)\s+(say|discuss|cover)|"
        r"briefly\s+describe",
        q,
    ))


def _doc_text_for_rerank(pay: dict) -> str:
    if pay.get("modality") == "text":
        return pay.get("text", "")
    return f"[{pay.get('modality', 'image')}] {pay.get('filename', '')} page {pay.get('page', '')}"


def _format_source_header(pay: dict) -> str:
    return format_position_header(pay)


def _space_filenames_context(space_id: str) -> str:
    """Exact filenames so the model does not hallucinate course codes or titles."""
    try:
        data = spaces.get_space(space_id)
    except Exception:
        return ""
    names = [f.get("original_name", "") for f in data.get("files", []) if f.get("original_name")]
    if not names:
        return ""
    lines = [
        "[EXACT FILENAMES — copy verbatim from this list when naming a file; do not paraphrase or alter spelling]",
    ]
    for name in names:
        lines.append(f"- {name}")
    return "\n".join(lines)


def _space_stats_context(space_id: str) -> str:
    """Authoritative per-file statistics for the generator (from space.json at ingest)."""
    try:
        data = spaces.get_space(space_id)
    except Exception:
        return ""
    lines = []
    for f in data.get("files", []):
        stats = f.get("text_stats")
        if not stats:
            continue
        lines.append(format_stats_context_block(f.get("original_name", "file"), stats))
    if not lines:
        return ""
    return (
        "[DOCUMENT STATISTICS — precise counts computed at ingest; "
        "these numbers are authoritative]\n"
        + "\n".join(lines)
    )


def _position_facts_for_chunk(pay: dict) -> str:
    """Compact positional + statistical facts injected into generator context."""
    lines = [format_position_header(pay)]
    stats_line = format_chunk_stats_line(pay)
    if stats_line:
        lines.append(stats_line)
    if pay.get("first_word") and pay.get("last_word"):
        lines.append(f"Chunk spans '{pay['first_word']}' … '{pay['last_word']}'")
    if pay.get("leading_words"):
        lines.append(f"Starts: {pay['leading_words'][:120]}")
    return " | ".join(lines)


def _extract_word_at_hint(pay: dict, hints: dict) -> str | None:
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


def _build_generator_messages(
    system_prompt: str,
    history: list,
    query: str,
    context_str: str | None = None,
) -> list:
    messages = [{"role": "system", "content": system_prompt}]
    for m in history:
        if m["role"] in ("user", "assistant"):
            messages.append({"role": m["role"], "content": m["content"]})
    if context_str:
        messages.append({
            "role": "user",
            "content": f"Context from documents:\n{context_str}\n\nQuestion: {query}",
        })
    else:
        messages.append({"role": "user", "content": query})
    return messages


def _status(step: str, text: str) -> dict:
    return {"type": "status", "step": step, "text": text}


def _cancelled(cancel: asyncio.Event | None) -> bool:
    return cancel is not None and cancel.is_set()


def _stream_generate(model, processor, messages, max_new_tokens: int, cancel: asyncio.Event | None = None):
    """Run model.generate in a thread; yield decoded token strings."""
    from transformers import StoppingCriteria, StoppingCriteriaList, TextIteratorStreamer

    class _CancelCriteria(StoppingCriteria):
        def __call__(self, input_ids, scores, **kwargs) -> bool:
            return _cancelled(cancel)

    text_input = processor.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True,
    )
    inputs = processor(text=[text_input], return_tensors="pt").to(model.device)
    streamer = TextIteratorStreamer(
        processor.tokenizer, skip_prompt=True, skip_special_tokens=True,
    )
    gen_kwargs = dict(
        **inputs,
        streamer=streamer,
        max_new_tokens=max_new_tokens,
        **GEN_KWARGS,
    )
    if cancel is not None:
        gen_kwargs["stopping_criteria"] = StoppingCriteriaList([_CancelCriteria()])
    thread = Thread(target=model.generate, kwargs=gen_kwargs)
    thread.start()
    for token in streamer:
        if _cancelled(cancel):
            break
        if token:
            yield token
    thread.join(timeout=0.1)


async def _async_stream_generate(
    model, processor, messages, max_new_tokens: int, cancel: asyncio.Event | None = None,
):
    """Yield tokens without blocking the event loop so SSE flushes incrementally."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[str | None] = asyncio.Queue()

    def _producer():
        try:
            for token in _stream_generate(model, processor, messages, max_new_tokens, cancel=cancel):
                if _cancelled(cancel):
                    break
                loop.call_soon_threadsafe(queue.put_nowait, token)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    Thread(target=_producer, daemon=True).start()
    while True:
        if _cancelled(cancel):
            break
        token = await queue.get()
        if token is None:
            break
        yield token
        await asyncio.sleep(0)


async def _yield_text_stream(text: str, cancel: asyncio.Event | None = None):
    """Stream a fixed string word-by-word (meta fast path)."""
    if not text:
        return
    parts = re.findall(r"\S+\s*", text)
    if not parts:
        parts = [text]
    for part in parts:
        if _cancelled(cancel):
            return
        yield part
        await asyncio.sleep(0)


async def run_query(
    user_query: str,
    space_id: str,
    chat_id: str,
    cancel: asyncio.Event | None = None,
):
    """Async generator yielding event dicts for the chat stream."""
    if _cancelled(cancel):
        return

    try:
        chat = spaces.get_chat(space_id, chat_id)
        history = list(chat.get("messages", []))
    except KeyError:
        history = []
    if history and history[-1].get("role") == "user" and history[-1].get("content") == user_query:
        history = history[:-1]

    hist_msgs, hist_tokens, summarized = prepare_chat_history(history, MAX_CONTEXT_TOKENS)

    # Meta / conversational questions: skip document retrieval.
    if _is_conversational_meta(user_query):
        yield {"type": "sources", "sources": []}
        fast = _meta_fast_answer(user_query)
        if fast:
            yield {"type": "context", **context_for_turn(
                hist_tokens, user_query, answer=fast, summarized=summarized,
            )}
            yield _status("prepare", "Preparing response…")
            yield _status("generate", "Generating response…")
            async for part in _yield_text_stream(fast, cancel=cancel):
                yield {"type": "token", "text": part}
            if not _cancelled(cancel):
                yield {"type": "done"}
            return

        yield {"type": "context", **context_for_turn(
            hist_tokens, user_query, extra=100, summarized=summarized,
        )}
        yield _status("prepare", "Preparing response…")
        system_prompt = (
            "You are a helpful assistant. Answer briefly and directly in one or two sentences. "
            "Reply in the same language the user writes in. "
            "Do not reference any documents, assignments, or PDFs. "
            "Do not invent document content or start translations unless asked."
        )
        messages = _build_generator_messages(system_prompt, hist_msgs, user_query)
        async with await manager.generator() as gen:
            yield _status("generate", "Generating response…")
            async for token in _async_stream_generate(
                gen["model"], gen["processor"], messages, META_MAX_NEW_TOKENS, cancel=cancel,
            ):
                yield {"type": "token", "text": token}
        if not _cancelled(cancel):
            yield {"type": "done"}
        return

    if count_points(space_id) == 0:
        yield {"type": "sources", "sources": []}
        empty_msg = "This space has no ingested documents yet. Add files in the Ingest tab first."
        yield {"type": "context", **context_for_turn(
            hist_tokens, user_query, answer=empty_msg, summarized=summarized,
        )}
        async for part in _yield_text_stream(empty_msg, cancel=cancel):
            yield {"type": "token", "text": part}
        if not _cancelled(cancel):
            yield {"type": "done"}
        return

    pos_hints = parse_position(user_query)
    n_points = count_points(space_id)

    meta_result = try_metadata_stat_response(space_id, pos_hints, user_query)
    if meta_result is not None:
        yield _status("search", "Searching content…")
        answer, meta_sources = meta_result
        yield {"type": "context", **context_for_turn(
            hist_tokens, user_query, answer=answer, summarized=summarized,
        )}
        yield _status("prepare", "Preparing response…")
        yield _status("generate", "Generating response…")
        async for part in _yield_text_stream(answer, cancel=cancel):
            yield {"type": "token", "text": part}
        yield {"type": "sources", "sources": meta_sources}
        if not _cancelled(cancel):
            yield {"type": "done"}
        return

    overview = _is_overview_query(user_query)
    top_k_retrieve, top_k_final = _top_k_for_space(n_points, overview)

    yield _status("embed", "Embedding query…")
    if _cancelled(cancel):
        return

    async with await manager.embedder() as embedder:
        q_vec = await asyncio.to_thread(
            embedder.encode,
            [user_query],
            prompt=QUERY_INSTRUCTION,
            normalize_embeddings=True,
        )
        q_vec = q_vec[0]

    yield _status("search", "Searching content…")

    hits = hybrid_search(q_vec, user_query, space_id, top_k=top_k_retrieve, pos_hints=pos_hints)
    hits = post_filter_hits(hits, pos_hints)
    hits = boost_hits_by_position(hits, pos_hints)
    hits = _dedupe_hits(hits)

    if not hits:
        yield {"type": "sources", "sources": []}
        miss_msg = "I couldn't find anything relevant in this space."
        yield {"type": "context", **context_for_turn(
            hist_tokens, user_query, answer=miss_msg, summarized=summarized,
        )}
        async for part in _yield_text_stream(miss_msg, cancel=cancel):
            yield {"type": "token", "text": part}
        if not _cancelled(cancel):
            yield {"type": "done"}
        return

    if _should_skip_rerank(n_points, user_query, pos_hints):
        yield _status("rank", "Selecting matches…")
        reranked_hits = hits[:top_k_final]
    else:
        yield _status("rank", "Ranking matches…")
        async with await manager.reranker() as reranker:
            docs = [_doc_text_for_rerank(h.payload) for h in hits]
            pairs = [(user_query, d) for d in docs]
            scores = await asyncio.to_thread(
                reranker.predict, pairs, prompt=RERANK_INSTRUCTION,
            )
            scores = np.asarray(scores, dtype=float)
            ranked = np.argsort(scores)[::-1][:top_k_final]
        reranked_hits = [hits[i] for i in ranked]

    used = hist_tokens + estimate_tokens(user_query) + 200
    packed_hits, retr_tokens = pack_retrieval_chunks(reranked_hits, MAX_CONTEXT_TOKENS, used)
    used += retr_tokens

    yield {"type": "context", **context_status(used, summarized=summarized)}

    context_parts = []
    count_fact = word_count_answer(reranked_hits, pos_hints, space_id=space_id)
    exact_word = _extract_word_at_hint(reranked_hits[0].payload, pos_hints) if reranked_hits else None

    prefix_lines = []
    filenames_context = _space_filenames_context(space_id)
    if filenames_context:
        prefix_lines.append(filenames_context)
    stats_context = _space_stats_context(space_id)
    if stats_context:
        prefix_lines.append(stats_context)
    if count_fact:
        prefix_lines.append(f"[PRECISE COUNT] {count_fact}")
    if exact_word:
        prefix_lines.append(f"[EXACT WORD AT POSITION] '{exact_word}'")

    for hit in packed_hits:
        pay = hit.payload
        header = _format_source_header(pay)
        facts = _position_facts_for_chunk(pay)
        if pay.get("modality") == "text":
            context_parts.append(f"[SOURCE: {header}]\n{facts}\n{pay.get('text', '')}")
        else:
            context_parts.append(f"[SOURCE: {header}]")
    context_str = "\n\n---\n\n".join(context_parts)
    if prefix_lines:
        context_str = "\n".join(prefix_lines) + "\n\n---\n\n" + context_str

    yield _status("prepare", "Preparing response…")

    async with await manager.generator() as gen:
        system_prompt = (
            "You are a precise research assistant. "
            "Answer ONLY based on the provided context and conversation history. "
            "Each source includes precise positional metadata and DOCUMENT STATISTICS "
            "(word counts, character counts, punctuation counts — all computed at ingest). "
            "doc_words (absolute 1-indexed word range in the full document), "
            "page_words (1-indexed within the page), "
            "para_words (1-indexed within the paragraph), "
            "region (header/body/footer), and exact word counts. "
            "Always prefer DOCUMENT STATISTICS and PRECISE COUNT lines over guessing. "
            "When citing a file, use the EXACT filename from EXACT FILENAMES or SOURCE headers. "
            "If the answer is not in the context, say so clearly. "
            "Never invent dates, deadlines, word limits, or requirements — copy them exactly from the context. "
            "When the user asks about a limit or length scoped to a specific part of the assignment "
            "(e.g. 'word count of the …'), read the requirement from the document text — "
            "do not substitute total document statistics. "
            "Reply in the same language the user writes in when appropriate. "
            "Keep answers concise; do not repeat the same sentence. "
            "Cite sources using filename, page, paragraph, and word positions."
        )
        try:
            custom = (spaces.get_space(space_id).get("system_prompt") or "").strip()
        except Exception:
            custom = ""
        if custom:
            system_prompt += "\n\nAdditional instructions for this space:\n" + custom

        messages = _build_generator_messages(system_prompt, hist_msgs, user_query, context_str)

        max_tokens = SUMMARY_MAX_NEW_TOKENS if overview else MAX_NEW_TOKENS
        yield _status("generate", "Generating response…")
        answer_parts: list[str] = []
        async for token in _async_stream_generate(
            gen["model"], gen["processor"], messages, max_tokens, cancel=cancel,
        ):
            if _cancelled(cancel):
                break
            answer_parts.append(token)
            yield {"type": "token", "text": token}
        full_answer = "".join(answer_parts)
        sources = _collect_sources(
            packed_hits, user_query, full_answer, pos_hints, exact_word,
        )
        yield {"type": "sources", "sources": sources}
        if not _cancelled(cancel):
            yield {"type": "done"}
