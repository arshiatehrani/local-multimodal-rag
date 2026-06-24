"""Tests for meta-question detection (skip RAG path). Run: python test_meta_query.py"""

from meta_detect import is_conversational_meta, meta_fast_answer

META = [
    "\u0641\u0627\u0631\u0633\u06cc \u0647\u0645 \u0645\u06cc\u062a\u0648\u0646\u06cc \u062d\u0631\u0641 \u0628\u0632\u0646\u06cc\u061f",
    "Can you speak Farsi?",
    "Do you understand Persian?",
    "hello",
    "Heyy",
    "hii",
    "\u0633\u0644\u0627\u0645",
    "what languages do you speak?",
]

NOT_META = [
    "what does the second paragraph say",
    "can you explain part 2 of the assignment",
    "can you speak about the assignment",
    "can you help me with part 1",
    "how many words in the document",
    "what is the due date",
]

def test_meta_detection():
    fails = 0
    for q in META:
        if not is_conversational_meta(q):
            print("FAIL should be meta:", q[:40])
            fails += 1
    for q in NOT_META:
        if is_conversational_meta(q):
            print("FAIL should NOT be meta:", q)
            fails += 1

    fa = meta_fast_answer("\u0641\u0627\u0631\u0633\u06cc \u0647\u0645 \u0645\u06cc\u062a\u0648\u0646\u06cc \u062d\u0631\u0641 \u0628\u0632\u0646\u06cc\u061f")
    if not fa or "\u0641\u0627\u0631\u0633\u06cc" not in fa:
        print("FAIL fast answer for Persian capability question")
        fails += 1

    assert fails == 0

if __name__ == "__main__":
    test_meta_detection()
    print("ok")
