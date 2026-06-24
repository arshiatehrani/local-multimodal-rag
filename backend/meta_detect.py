"""Chitchat / greeting / meta-question detection.

Extracted from query.py to keep the main pipeline focused on retrieval + generation.
"""

import re

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


def is_casual_greeting(query: str) -> bool:
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


def is_conversational_meta(query: str) -> bool:
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
    if is_casual_greeting(q):
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


def meta_fast_answer(query: str) -> str | None:
    """Deterministic one-line answers for common meta questions (no RAG, no long generation)."""
    q = query.strip()
    if not is_conversational_meta(q):
        return None

    has_persian = bool(re.search(r"[\u0600-\u06FF]", q))
    if re.search(r"\b(farsi|persian|فارسی)\b", q, re.I) or (
        has_persian and re.search(r"(می[\u200c]?تون|حرف|زبان|فارسی)", q)
    ):
        return "بله، می‌توانم به فارسی پاسخ دهم." if has_persian else "Yes, I can respond in Farsi/Persian."

    if is_casual_greeting(q) or re.search(r"^(hi|hello|hey)\b", q, re.I):
        return "Hello! Ask me anything about the files in this space."
    if re.search(r"^(thanks|thank you)\b", q, re.I):
        return "You're welcome!"
    if re.search(r"^(سلام|درود)\b", q):
        return "سلام! هر سوالی دربارهٔ فایل‌های این فضا دارید بپرسید."

    if re.search(r"what\s+(language|languages)", q, re.I):
        return "I reply in whichever language you write in, including English and Persian/Farsi."

    return None
