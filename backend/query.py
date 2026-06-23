"""Query pipeline: embed -> vector search -> rerank -> generate.

``run_query`` is an async generator yielding structured event dicts
(``{"type": "sources"|"token"|"done", ...}``). The HTTP layer (main.py) turns
these into SSE and also persists the assistant message.
"""

from threading import Thread

import numpy as np

import spaces
from model_manager import manager
from qdrant_store import search, count_points

QUERY_INSTRUCTION = "Retrieve relevant documents for the query."
RERANK_INSTRUCTION = "Retrieve images or text relevant to the user's query."
TOP_K_RETRIEVE = 20
TOP_K_FINAL = 5
MAX_NEW_TOKENS = 1024


def _doc_text_for_rerank(pay: dict) -> str:
    if pay.get("modality") == "text":
        return pay.get("text", "")
    return f"[{pay.get('modality', 'image')}] {pay.get('filename', '')} page {pay.get('page', '')}"


async def run_query(user_query: str, space_id: str):
    """Async generator yielding event dicts for the chat stream, scoped to a space."""

    # 0) Fast path: if this space has nothing ingested, answer instantly WITHOUT
    # loading the embedder (a model load can take tens of seconds on first use).
    if count_points(space_id) == 0:
        yield {"type": "sources", "sources": []}
        yield {"type": "token", "text": "This space has no ingested documents yet. Add files in the Ingest tab first."}
        yield {"type": "done"}
        return

    # 1) Embed the query.
    async with await manager.embedder() as embedder:
        q_vec = embedder.encode(
            [user_query],
            prompt=QUERY_INSTRUCTION,
            normalize_embeddings=True,
        )[0]

    # 2) Vector search (within this space only).
    hits = search(q_vec, space_id=space_id, top_k=TOP_K_RETRIEVE)

    if not hits:
        yield {"type": "sources", "sources": []}
        yield {"type": "token", "text": "I couldn't find anything relevant in this space."}
        yield {"type": "done"}
        return

    # 3) Rerank.
    async with await manager.reranker() as reranker:
        docs_for_rerank = [_doc_text_for_rerank(h.payload) for h in hits]
        pairs = [(user_query, doc) for doc in docs_for_rerank]
        scores = reranker.predict(pairs, prompt=RERANK_INSTRUCTION)
        scores = np.asarray(scores, dtype=float)
        ranked_indices = np.argsort(scores)[::-1][:TOP_K_FINAL]

    top_hits = [hits[i] for i in ranked_indices]

    # 4) Build context + source list.
    context_parts, sources = [], []
    for hit in top_hits:
        pay = hit.payload
        if pay.get("modality") == "text":
            context_parts.append(f"[SOURCE: {pay.get('filename')} p.{pay.get('page')}]\n{pay.get('text', '')}")
        else:
            context_parts.append(
                f"[SOURCE: {pay.get('filename')} p.{pay.get('page')} - {pay.get('modality')}]"
            )
        sources.append({
            "file_id": pay.get("file_id", ""),
            "filename": pay.get("filename", ""),
            "page": pay.get("page", ""),
            "modality": pay.get("modality", ""),
            "thumbnail": pay.get("thumbnail_b64", ""),
            "text": pay.get("text", ""),
        })
    context_str = "\n\n---\n\n".join(context_parts)

    # 5) Generate (streaming).
    async with await manager.generator() as gen:
        model = gen["model"]
        processor = gen["processor"]

        system_prompt = (
            "You are a precise research assistant. "
            "Answer ONLY based on the provided context. "
            "If the answer is not in the context, say so clearly. "
            "Cite sources by filename and page number."
        )
        # Append this space's custom instructions (output format, persona, etc).
        try:
            custom = (spaces.get_space(space_id).get("system_prompt") or "").strip()
        except Exception:
            custom = ""
        if custom:
            system_prompt += "\n\nAdditional instructions for this space:\n" + custom
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Context:\n{context_str}\n\nQuestion: {user_query}"},
        ]

        text_input = processor.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )
        inputs = processor(text=[text_input], return_tensors="pt").to(model.device)

        from transformers import TextIteratorStreamer

        streamer = TextIteratorStreamer(
            processor.tokenizer,
            skip_prompt=True,
            skip_special_tokens=True,
        )
        gen_kwargs = dict(
            **inputs,
            streamer=streamer,
            max_new_tokens=MAX_NEW_TOKENS,
            do_sample=False,
        )
        thread = Thread(target=model.generate, kwargs=gen_kwargs)
        thread.start()

        # Send sources first.
        yield {"type": "sources", "sources": sources}

        for token in streamer:
            if token:
                yield {"type": "token", "text": token}

        thread.join()
        yield {"type": "done"}
