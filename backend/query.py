"""Query pipeline: embed -> hybrid search -> rerank -> generate (with chat history)."""

import asyncio
import re
from threading import Thread

import numpy as np

import spaces
from model_manager import manager
from positioning import (
    format_position_header,
    post_filter_hits,
    boost_hits_by_position,
    word_count_answer,
    tokenize_words,
)
from qdrant_store import hybrid_search, count_points
from rag_context import (
    MAX_CONTEXT_TOKENS,
    estimate_tokens,
    parse_position,
    prepare_chat_history,
    pack_retrieval_chunks,
    context_status,
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

    # Short greetings / thanks only
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

    if re.search(r"^(hi|hello|hey)\b", q, re.I):
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
    if pos_hints.get("wants_word_count"):
        return True
    if pos_hints.get("paragraph_index") is not None or pos_hints.get("word_target"):
        return True
    return False


def _select_overview_hits(hits: list, top_k: int) -> list:
    """Prefer one whole-page chunk plus top distinct paragraphs."""
    hits = _dedupe_hits(hits)
    full = [h for h in hits if (h.payload or {}).get("chunk_kind") == "page_full"]
    rest = [h for h in hits if h not in full]
    if full:
        return full[:1] + rest[: max(1, top_k - 1)]
    return rest[:top_k]


def _is_overview_query(query: str) -> bool:
    q = query.lower().strip()
    return bool(re.search(
        r"what('s| is)\s+(this|the|it)\s+(document|pdf|file|paper|assignment|text)\s+about|"
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


def _position_facts_for_chunk(pay: dict) -> str:
    """Compact positional facts injected into generator context."""
    lines = [format_position_header(pay)]
    if pay.get("doc_word_count"):
        lines.append(f"Document total: {pay['doc_word_count']} words")
    if pay.get("page_word_count"):
        lines.append(f"Page total: {pay['page_word_count']} words")
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


def _stream_generate(model, processor, messages, max_new_tokens: int):
    """Run model.generate in a thread; yield decoded token strings."""
    from transformers import TextIteratorStreamer

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
    thread = Thread(target=model.generate, kwargs=gen_kwargs)
    thread.start()
    for token in streamer:
        if token:
            yield token
    thread.join()


async def run_query(user_query: str, space_id: str, chat_id: str):
    """Async generator yielding event dicts for the chat stream."""

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
        used = hist_tokens + estimate_tokens(user_query) + 100
        yield {"type": "sources", "sources": []}
        yield {"type": "context", **context_status(used, summarized=summarized)}

        fast = _meta_fast_answer(user_query)
        if fast:
            yield {"type": "token", "text": fast}
            yield {"type": "done"}
            return

        system_prompt = (
            "You are a helpful assistant. Answer briefly and directly in one or two sentences. "
            "Reply in the same language the user writes in. "
            "Do not reference any documents, assignments, or PDFs. "
            "Do not invent document content or start translations unless asked."
        )
        messages = _build_generator_messages(system_prompt, hist_msgs, user_query)
        async with await manager.generator() as gen:
            for token in _stream_generate(
                gen["model"], gen["processor"], messages, META_MAX_NEW_TOKENS,
            ):
                yield {"type": "token", "text": token}
        yield {"type": "done"}
        return

    if count_points(space_id) == 0:
        yield {"type": "sources", "sources": []}
        yield {"type": "context", **context_status(estimate_tokens(user_query))}
        yield {"type": "token", "text": "This space has no ingested documents yet. Add files in the Ingest tab first."}
        yield {"type": "done"}
        return

    pos_hints = parse_position(user_query)
    n_points = count_points(space_id)
    overview = _is_overview_query(user_query)
    top_k_retrieve, top_k_final = _top_k_for_space(n_points, overview)

    yield {"type": "status", "text": "Embedding your question…"}

    async with await manager.embedder() as embedder:
        q_vec = await asyncio.to_thread(
            embedder.encode,
            [user_query],
            prompt=QUERY_INSTRUCTION,
            normalize_embeddings=True,
        )
        q_vec = q_vec[0]

    yield {"type": "status", "text": "Searching documents…"}

    hits = hybrid_search(q_vec, user_query, space_id, top_k=top_k_retrieve, pos_hints=pos_hints)
    hits = post_filter_hits(hits, pos_hints)
    hits = boost_hits_by_position(hits, pos_hints)
    hits = _dedupe_hits(hits)

    if not hits:
        yield {"type": "sources", "sources": []}
        yield {"type": "context", **context_status(estimate_tokens(user_query))}
        yield {"type": "token", "text": "I couldn't find anything relevant in this space."}
        yield {"type": "done"}
        return

    if _should_skip_rerank(n_points, user_query, pos_hints):
        yield {"type": "status", "text": "Selecting passages…"}
        reranked_hits = hits[:top_k_final]
    else:
        yield {"type": "status", "text": "Reranking passages…"}
        async with await manager.reranker() as reranker:
            docs = [_doc_text_for_rerank(h.payload) for h in hits]
            pairs = [(user_query, d) for d in docs]
            scores = await asyncio.to_thread(
                reranker.predict, pairs, prompt=RERANK_INSTRUCTION,
            )
            scores = np.asarray(scores, dtype=float)
            ranked = np.argsort(scores)[::-1][:top_k_final]
        reranked_hits = [hits[i] for i in ranked]

    if overview:
        reranked_hits = _select_overview_hits(reranked_hits, top_k_final)

    used = hist_tokens + estimate_tokens(user_query) + 200
    packed_hits, retr_tokens = pack_retrieval_chunks(reranked_hits, MAX_CONTEXT_TOKENS, used)
    used += retr_tokens

    yield {"type": "context", **context_status(used, summarized=summarized)}

    context_parts, sources = [], []
    seen_sources: set = set()
    count_fact = word_count_answer(reranked_hits, pos_hints)
    exact_word = _extract_word_at_hint(reranked_hits[0].payload, pos_hints) if reranked_hits else None

    prefix_lines = []
    if count_fact:
        prefix_lines.append(f"[PRECISE COUNT] {count_fact}")
    if exact_word:
        prefix_lines.append(f"[EXACT WORD AT POSITION] '{exact_word}'")

    for hit in packed_hits:
        pay = hit.payload
        src_key = _chunk_dedupe_key(hit)
        header = _format_source_header(pay)
        facts = _position_facts_for_chunk(pay)
        if pay.get("modality") == "text":
            context_parts.append(f"[SOURCE: {header}]\n{facts}\n{pay.get('text', '')}")
        else:
            context_parts.append(f"[SOURCE: {header}]")
        if src_key in seen_sources:
            continue
        seen_sources.add(src_key)
        sources.append({
            "file_id": pay.get("file_id", ""),
            "filename": pay.get("filename", ""),
            "page": pay.get("page", ""),
            "paragraph_index": pay.get("paragraph_index"),
            "word_start": pay.get("doc_word_start", pay.get("word_start")),
            "word_end": pay.get("doc_word_end", pay.get("word_end")),
            "page_word_start": pay.get("page_word_start"),
            "page_word_end": pay.get("page_word_end"),
            "para_word_start": pay.get("para_word_start"),
            "para_word_end": pay.get("para_word_end"),
            "doc_word_count": pay.get("doc_word_count"),
            "region": pay.get("region", ""),
            "chunk_kind": pay.get("chunk_kind", ""),
            "modality": pay.get("modality", ""),
            "thumbnail": pay.get("thumbnail_b64", ""),
            "text": pay.get("text", ""),
        })
    context_str = "\n\n---\n\n".join(context_parts)
    if prefix_lines:
        context_str = "\n".join(prefix_lines) + "\n\n---\n\n" + context_str

    async with await manager.generator() as gen:
        system_prompt = (
            "You are a precise research assistant. "
            "Answer ONLY based on the provided context and conversation history. "
            "Each source includes precise positional metadata: "
            "doc_words (absolute 1-indexed word range in the full document), "
            "page_words (1-indexed within the page), "
            "para_words (1-indexed within the paragraph), "
            "region (header/body/footer), and exact word counts. "
            "Use PRECISE COUNT and EXACT WORD AT POSITION facts when present. "
            "If the answer is not in the context, say so clearly. "
            "Never invent dates, deadlines, word limits, or requirements — copy them exactly from the context. "
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
        yield {"type": "status", "text": "Writing answer…"}
        yield {"type": "sources", "sources": sources}
        for token in _stream_generate(gen["model"], gen["processor"], messages, max_tokens):
            yield {"type": "token", "text": token}
        yield {"type": "done"}
