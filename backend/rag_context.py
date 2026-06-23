"""Context window budgeting, chat-history trimming, and positional query parsing."""

from positioning import parse_position  # noqa: F401 — re-exported for callers

# Conservative budget for Qwen3-VL-2B on a 6 GB GPU (tokens, not chars).
MAX_CONTEXT_TOKENS = int(__import__("os").environ.get("MAX_CONTEXT_TOKENS", "8192"))
RESERVED_FOR_RESPONSE = 1024
MAX_HISTORY_TURNS = int(__import__("os").environ.get("MAX_HISTORY_TURNS", "8"))
HISTORY_BUDGET_RATIO = 0.35  # at most 35% of window for chat history


def estimate_tokens(text: str) -> int:
    """Rough token count (~4 chars per token for English)."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def _summarize_messages(messages: list) -> str:
    """Compact summary of older turns (no extra model call)."""
    parts = []
    for m in messages:
        role = "User" if m["role"] == "user" else "Assistant"
        text = (m.get("content") or "").strip().replace("\n", " ")
        if len(text) > 120:
            text = text[:120] + "..."
        parts.append(f"{role}: {text}")
    return "Earlier conversation summary:\n" + "\n".join(parts)


def prepare_chat_history(messages: list, budget_tokens: int) -> tuple[list, int, bool]:
    """Return (history_for_prompt, tokens_used, was_summarized)."""
    if not messages:
        return [], 0, False

    history_budget = int(budget_tokens * HISTORY_BUDGET_RATIO)
    recent = messages[-MAX_HISTORY_TURNS * 2:]
    kept, used = [], 0
    for m in recent:
        t = estimate_tokens(m.get("content", ""))
        if used + t <= history_budget:
            kept.append(m)
            used += t
        else:
            break

    if len(kept) < len(messages) and len(messages) > 2:
        older = messages[: max(0, len(messages) - len(kept))]
        summary = _summarize_messages(older[-6:])
        st = estimate_tokens(summary)
        if st <= history_budget:
            return [{"role": "system", "content": summary}] + kept, used + st, True

    return kept, used, False


def pack_retrieval_chunks(hits: list, budget_tokens: int, already_used: int) -> tuple[list, int]:
    """Greedy pack reranked hits until retrieval budget is full."""
    remaining = budget_tokens - already_used - RESERVED_FOR_RESPONSE
    packed, used = [], 0
    for hit in hits:
        pay = hit.payload if hasattr(hit, "payload") else hit
        text = pay.get("text", "") if pay.get("modality") == "text" else ""
        chunk_text = text or f"[{pay.get('modality', 'image')}] {pay.get('filename', '')} p.{pay.get('page', '')}"
        t = estimate_tokens(chunk_text) + 20
        if used + t > remaining and packed:
            break
        packed.append(hit)
        used += t
    return packed, used


def context_status(used: int, budget: int = MAX_CONTEXT_TOKENS, summarized: bool = False) -> dict:
    pct = min(100, round(100 * used / budget)) if budget else 0
    return {
        "used_tokens": used,
        "budget_tokens": budget,
        "remaining_tokens": max(0, budget - used),
        "pct": pct,
        "summarized": summarized,
    }
