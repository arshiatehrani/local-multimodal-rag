"""Tests for word-count query parsing."""

from positioning import parse_position


def test_parse_how_many_words_this_document():
    h = parse_position("how many words this document has")
    assert h.get("wants_word_count") is True
    assert h.get("count_scope") == "document"


def test_parse_how_many_paragraphs():
    h = parse_position("how many paragraphs in this document")
    assert h.get("wants_paragraph_count") is True
    assert h.get("count_scope") == "document"
    assert not h.get("wants_word_count")


def test_parse_paragraph_count_on_page():
    h = parse_position("how many paragraphs on page 2")
    assert h.get("wants_paragraph_count") is True
    assert h.get("count_scope") == "page"
    assert h.get("page") == 2


def test_parse_word_count_on_page():
    h = parse_position("what is the word count on page 3")
    assert h.get("wants_word_count") is True
    assert h.get("count_scope") == "page"
    assert h.get("page") == 3
