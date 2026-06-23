"""Tests for character-count query parsing and counting."""

from positioning import (
    parse_position,
    count_character_matches,
    build_char_count_sources,
    filter_chunks_for_char_count,
)


def test_parse_commas_query():
    h = parse_position("tell me the number of commas in the text")
    assert h.get("wants_char_count") is True
    assert h.get("char_target") == ","
    assert h.get("count_scope") == "document"


def test_parse_letter_s_query():
    h = parse_position("how many letter s are in the document")
    assert h.get("wants_char_count") is True
    assert h.get("char_target") == "s"
    assert h.get("char_case_insensitive") is True


def test_count_commas():
    text = "a,b, c,,"
    assert count_character_matches(text, ",", False) == 4


def test_count_letter_s_insensitive():
    text = "Sisyphus sees seas"
    assert count_character_matches(text, "s", True) == 7


def test_build_sources_per_page():
    chunks = [
        {
            "file_id": "f1",
            "filename": "a.pdf",
            "page": 1,
            "total_pages": 2,
            "modality": "text",
            "chunk_kind": "page_full",
            "text": "a,b,",
        },
        {
            "file_id": "f1",
            "filename": "a.pdf",
            "page": 2,
            "total_pages": 2,
            "modality": "text",
            "chunk_kind": "page_full",
            "text": ",",
        },
    ]
    hints = {"char_target": ",", "char_case_insensitive": False, "count_scope": "document"}
    filtered = filter_chunks_for_char_count(chunks, hints)
    answer, sources = build_char_count_sources(filtered, hints)
    assert "**3**" in answer
    assert len(sources) == 2
    assert sources[0]["highlight_mode"] == "chars"
    assert sources[0]["char_match_count"] == 2
