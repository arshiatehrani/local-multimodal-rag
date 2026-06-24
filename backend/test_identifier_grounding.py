"""Tests for alphanumeric identifier extraction and drift correction."""

from identifiers import (
    extract_grounded_identifiers,
    fix_identifier_drift,
    grounded_identifiers_context,
)


class _Hit:
    def __init__(self, payload):
        self.payload = payload


def test_extract_from_text_and_filename():
    hits = [
        _Hit({"text": "Impact Analysis Proposal for APSC 812 – S26.", "filename": "doc.pdf"}),
    ]
    ids = extract_grounded_identifiers(hits, "missing-space")
    assert "APSC 812" in ids
    assert "APSC812" in ids
    assert "S26" in ids


def test_fix_digit_drift_single_canonical():
    grounded = ["APSC 812", "APSC812", "S26"]
    answer = "Course APSC 912 and also APSC 712 – Ethics."
    fixed = fix_identifier_drift(answer, grounded)
    assert "APSC 912" not in fixed
    assert "APSC 712" not in fixed
    assert "APSC 812" in fixed


def test_no_fix_when_answer_matches():
    grounded = ["APSC 812"]
    answer = "Course APSC 812 – S26."
    assert fix_identifier_drift(answer, grounded) == answer


def test_context_block_nonempty():
    ctx = grounded_identifiers_context(["APSC 812", "S26"])
    assert "EXACT IDENTIFIERS" in ctx
    assert "APSC 812" in ctx


if __name__ == "__main__":
    test_extract_from_text_and_filename()
    test_fix_digit_drift_single_canonical()
    test_no_fix_when_answer_matches()
    test_context_block_nonempty()
    print("ok")
