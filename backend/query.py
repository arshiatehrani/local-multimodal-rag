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
from qdrant_store import hybrid_search, count_points, fetch_overview_chunks
from rag_context import (
    MAX_CONTEXT_TOKENS,
    estimate_tokens,
    parse_position,
    prepare_chat_history,
    pack_retrieval_chunks,
    context_status,
    context_for_turn,
)
from meta_detect import is_conversational_meta, meta_fast_answer
from highlights import compute_highlight_phrases, extract_word_at_hint
from identifiers import (
    extract_grounded_identifiers,
    grounded_identifiers_context,
    fix_identifier_drift,
)

QUERY_INSTRUCTION = "Retrieve relevant documents for the query."
RERANK_INSTRUCTION = "Retrieve images or text relevant to the user's query."
MAX_NEW_TOKENS = 1024
SUMMARY_MAX_NEW_TOKENS = 512
META_MAX_NEW_TOKENS = 256
HYDE_MAX_TOKENS = 64  # short hypothetical answer for query expansion
# Only skip rerank for tiny corpora with simple positional/count queries (not summaries).
SKIP_RERANK_MAX_POINTS = int(__import__("os").environ.get("SKIP_RERANK_MAX_POINTS", "4"))
# Reranker confidence: if the best reranker score is below this, prepend a disclaimer.
RERANK_CONFIDENCE_THRESHOLD = float(__import__("os").environ.get("RERANK_CONFIDENCE_THRESHOLD", "0.15"))
GEN_KWARGS = {
    "do_sample": False,
    "repetition_penalty": 1.15,
    "no_repeat_ngram_size": 4,
    "cache_implementation": "quantized",  # INT8 KV cache: halves VRAM during generation
    "cache_config": {"nbits": 8, "backend": "quanto"},
}




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
        highlights = compute_highlight_phrases(pay, query, answer, pos_hints, exact_word)
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
        return True
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


def _compress_chunk_text(text: str, query: str, max_sentences: int = 6) -> str:
    """Contextual compression: keep only sentences most relevant to the query.

    Splits the chunk into sentences, scores each by keyword overlap with the
    query, and keeps the top `max_sentences`. This lets more diverse chunks
    fit inside the context window.
    """
    if not text or not query:
        return text
    # Split on sentence boundaries (period/question/exclamation + space or end)
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    if len(sentences) <= max_sentences:
        return text
    query_words = set(query.lower().split())
    scored = []
    for i, sent in enumerate(sentences):
        sent_words = set(sent.lower().split())
        overlap = len(query_words & sent_words)
        # Small positional bonus for first/last sentences (often contain key info)
        pos_bonus = 0.5 if i == 0 or i == len(sentences) - 1 else 0
        scored.append((overlap + pos_bonus, i, sent))
    scored.sort(key=lambda x: x[0], reverse=True)
    kept = sorted(scored[:max_sentences], key=lambda x: x[1])  # restore order
    return " ".join(s for _, _, s in kept)





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
    if is_conversational_meta(user_query):
        yield {"type": "sources", "sources": []}
        fast = meta_fast_answer(user_query)
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

    # ── HyDE query expansion (#5): embed a hypothetical answer for better retrieval ──
    hyde_enabled = __import__("os").environ.get("ENABLE_HYDE", "0") == "1"
    hyde_text = None
    if hyde_enabled and not pos_hints and not overview:
        try:
            async with await manager.generator() as gen:
                hyde_prompt = (
                    "Write a short factual answer (1-2 sentences) to this question "
                    "as if you had the source document: " + user_query
                )
                hyde_messages = [{"role": "user", "content": hyde_prompt}]
                parts = []
                async for tok in _async_stream_generate(
                    gen["model"], gen["processor"], hyde_messages,
                    HYDE_MAX_TOKENS, cancel=cancel,
                ):
                    parts.append(tok)
                hyde_text = "".join(parts).strip()
        except Exception:
            hyde_text = None

    async with await manager.embedder() as embedder:
        q_vec = await asyncio.to_thread(
            embedder.encode,
            [user_query],
            prompt=QUERY_INSTRUCTION,
            normalize_embeddings=True,
        )
        q_vec = q_vec[0]

        # HyDE: blend hypothetical answer embedding with original query vector
        if hyde_text:
            hyde_vec = await asyncio.to_thread(
                embedder.encode,
                [hyde_text],
                prompt=QUERY_INSTRUCTION,
                normalize_embeddings=True,
            )
            hyde_vec = hyde_vec[0]
            # Weighted blend: 70% original query, 30% hypothetical
            q_vec = 0.7 * q_vec + 0.3 * hyde_vec
            # Re-normalize
            norm = np.linalg.norm(q_vec)
            if norm > 0:
                q_vec = q_vec / norm

    overview = _is_overview_query(user_query)
    if overview:
        yield _status("search", "Fetching document overview…")
        hits = fetch_overview_chunks(space_id, top_k=top_k_retrieve)
    else:
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

    best_rerank_score = None
    if _should_skip_rerank(n_points, user_query, pos_hints):
        yield _status("rank", "Selecting matches…")
        reranked_hits = hits[:top_k_final]
    else:
        yield _status("rank", "Ranking matches…")
        async with await manager.reranker() as reranker:
            docs = [_doc_text_for_rerank(h.payload) for h in hits]
            pairs = [(user_query, d) for d in docs]
            rank_task = asyncio.create_task(
                asyncio.to_thread(
                    reranker.predict, pairs, prompt=RERANK_INSTRUCTION,
                )
            )
            while not rank_task.done():
                yield _status("rank", "Ranking matches (processing)…")
                await asyncio.sleep(2)
            scores = rank_task.result()
            scores = np.asarray(scores, dtype=float)
            ranked = np.argsort(scores)[::-1][:top_k_final]
            best_rerank_score = float(scores[ranked[0]]) if len(ranked) > 0 else None
        reranked_hits = [hits[i] for i in ranked]

    used = hist_tokens + estimate_tokens(user_query) + 200
    packed_hits, retr_tokens = pack_retrieval_chunks(reranked_hits, MAX_CONTEXT_TOKENS, used)
    used += retr_tokens

    yield {"type": "context", **context_status(used, summarized=summarized)}

    context_parts = []
    count_fact = word_count_answer(reranked_hits, pos_hints, space_id=space_id)
    exact_word = extract_word_at_hint(reranked_hits[0].payload, pos_hints) if reranked_hits else None

    prefix_lines = []
    filenames_context = _space_filenames_context(space_id)
    if filenames_context:
        prefix_lines.append(filenames_context)
    grounded_ids = extract_grounded_identifiers(packed_hits, space_id)
    ids_context = grounded_identifiers_context(grounded_ids)
    if ids_context:
        prefix_lines.append(ids_context)
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
            chunk_text = pay.get('text', '')
            # Contextual compression (#6): extract most relevant sentences
            chunk_text = _compress_chunk_text(chunk_text, user_query)
            context_parts.append(f"[SOURCE: {header}]\n{facts}\n{chunk_text}")
        else:
            context_parts.append(f"[SOURCE: {header}]")
    context_str = "\n\n---\n\n".join(context_parts)
    
    # ── Confidence scoring (#15): low-confidence disclaimer ──
    low_confidence = (
        best_rerank_score is not None
        and best_rerank_score < RERANK_CONFIDENCE_THRESHOLD
    )
    
    if low_confidence:
        context_str = (
            "[LOW CONFIDENCE WARNING: The retrieved documents may not be highly "
            "relevant to this query. If the context does not contain a clear answer, "
            "state that you could not find strong evidence in the uploaded documents.]\n\n"
            + context_str
        )
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
            "For course codes, catalog numbers, and similar alphanumeric labels, use ONLY forms "
            "listed under EXACT IDENTIFIERS — never change digits or letters (e.g. do not swap 812 for 912). "
            "If the answer is not in the context, say so clearly. "
            "Never invent dates, deadlines, word limits, or requirements — copy them exactly from the context. "
            "When the user asks about a limit or length scoped to a specific part of the assignment "
            "(e.g. 'word count of the …'), read the requirement from the document text — "
            "do not substitute total document statistics. "
            "Reply in the same language the user writes in when appropriate. "
            "Keep answers concise; do not repeat the same sentence. "
            "When referencing information from the context, cite the source inline "
            "using the format [Source: filename, p.X] so the user can verify. "
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
        full_answer = fix_identifier_drift(full_answer, grounded_ids)
        if full_answer != "".join(answer_parts):
            yield {"type": "replace", "text": full_answer}
        sources = _collect_sources(
            packed_hits, user_query, full_answer, pos_hints, exact_word,
        )
        yield {"type": "sources", "sources": sources}
        if not _cancelled(cancel):
            try:
                yield _status("generate", "Suggesting follow-ups…")
                followup_prompt = (
                    "Based on the context and your answer above, suggest exactly 3 short follow-up questions "
                    "the user could ask to explore this topic further. Output ONLY the 3 questions, one per line, starting with '- '."
                )
                follow_msgs = messages.copy()
                follow_msgs.append({"role": "assistant", "content": full_answer})
                follow_msgs.append({"role": "user", "content": followup_prompt})
                
                followup_parts = []
                async for tok in _async_stream_generate(
                    gen["model"], gen["processor"], follow_msgs, 80, cancel=cancel,
                ):
                    followup_parts.append(tok)
                
                follow_ups = []
                for line in "".join(followup_parts).split('\n'):
                    line = line.strip()
                    if line.startswith("- "):
                        follow_ups.append(line[2:].strip())
                    elif line and "?" in line:
                        follow_ups.append(line.lstrip("1234567890. ").strip())
                
                if follow_ups:
                    yield {"type": "follow_ups", "questions": follow_ups[:3]}
            except Exception:
                pass
                
            yield {"type": "done"}
