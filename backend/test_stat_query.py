"""Tests for unified ingest-stat query parsing (words, chars, lines, etc.)."""

from positioning import parse_position
from stat_query import parse_stat_count_hints, resolve_stat_metric


STAT_PHRASES = [
    ("how many words this document has", "word", "document"),
    ("what is the word count on page 3", "word", "page"),
    ("give me the number of words in this pdf", "word", "document"),
    ("total number of words in the file", "word", "document"),
    ("how many paragraphs in this document", "paragraph", "document"),
    ("paragraph count for the text", "paragraph", "document"),
    ("how many paragraphs on page 2", "paragraph", "page"),
    ("what's the count of lines in the document", "line", "document"),
    ("line count", "line", "document"),
    ("how many characters in this document", "char", "document"),
    ("character count", "char", "document"),
    ("how many chars excluding whitespace", "char_no_space", "document"),
    ("number of non-whitespace characters", "char_no_space", "document"),
    ("how much whitespace", "whitespace", "document"),
    ("tell me the number of spaces in the text", "whitespace", "document"),
    ("how many digits in the document", "digit", "document"),
    ("count the semicolons", "punct:semicolon", "document"),
    ("tell me the number of commas in the text", "punct:comma", "document"),
    ("how many question marks", "punct:question_mark", "document"),
    ("how many letter s are in the document", "letter:s", "document"),
    ("number of s's", "letter:s", "document"),
    ("document statistics for this pdf", "summary", "document"),
]


def test_parse_stat_variations():
    for query, metric, scope in STAT_PHRASES:
        h = parse_position(query)
        assert resolve_stat_metric(h) == metric, f"{query!r} -> {resolve_stat_metric(h)!r}"
        assert h.get("count_scope") == scope, f"{query!r} scope -> {h.get('count_scope')!r}"


def test_positional_word_query_not_stat_count():
    h = parse_position("what is the 5th word in the document")
    assert h.get("word_target") == 5
    assert resolve_stat_metric(h) is None


def test_third_word_in_second_paragraph_not_stat():
    h = parse_position("third word in second paragraph")
    assert h.get("para_word_target") == 3
    assert resolve_stat_metric(h) is None


def test_parse_stat_count_hints_direct():
    hints: dict = {"page": 4}
    parse_stat_count_hints("how many lines on page 4", hints)
    assert hints.get("stat_metric") == "line"
    assert hints.get("count_scope") == "page"


if __name__ == "__main__":
    test_parse_stat_variations()
    test_positional_word_query_not_stat_count()
    test_third_word_in_second_paragraph_not_stat()
    test_parse_stat_count_hints_direct()
    print("all stat_query tests passed")
