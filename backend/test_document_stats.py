"""Tests for ingest-time document statistics."""

from document_stats import compute_text_stats, flatten_stats, build_stats_summary_text


def test_compute_text_stats():
    text = "Hello, world.\nSecond line, here."
    stats = compute_text_stats(text)
    assert stats["word_count"] == 5
    assert stats["char_count"] == len(text)
    assert stats["punctuation"]["comma"] == 2
    assert stats["punctuation"]["period"] == 2
    assert stats["letter_counts"]["h"] >= 1
    assert stats["letter_counts"]["e"] >= 2


def test_flatten_stats():
    stats = compute_text_stats("a, b")
    flat = flatten_stats("doc", stats)
    assert flat["doc_word_count"] == 2
    assert flat["doc_comma_count"] == 1
    assert flat["doc_letter_a_count"] == 1


def test_stats_summary():
    stats = compute_text_stats("one, two, three.")
    summary = build_stats_summary_text("test.pdf", stats, total_pages=1, paragraph_count_doc=1)
    assert "DOCUMENT STATISTICS" in summary
    assert "Words: 3" in summary
    assert "commas=2" in summary
