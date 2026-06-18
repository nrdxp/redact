#!/usr/bin/env nix-shell
#!nix-shell -i python3 -p python3Packages.pymupdf
"""Test suite for redact-ssn.py.

Self-contained like the tool itself: run it directly and the nix-shell shebang
pulls in pymupdf. Fixtures are generated on the fly in a temp dir, so no PDFs
(which could carry the very data we redact) are ever committed.

    ./tests/run-tests.py
"""
import importlib.util
import re
import sys
import tempfile
from pathlib import Path

import pymupdf

# Load the hyphenated script as an importable module.
ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("redact_ssn", ROOT / "redact-ssn.py")
redact_ssn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(redact_ssn)


# --- helpers ---------------------------------------------------------------

def digits(text):
    """Just the digits of a string, separators/whitespace stripped."""
    return re.sub(r"\D", "", text)


def make_pdf(path, pages):
    """Write a PDF. *pages* is a list of pages; each page a list of
    (x, y, text) text placements -- enough to fake labels, dashed values,
    and digits split across separate form boxes (separate placements)."""
    doc = pymupdf.open()
    for placements in pages:
        page = doc.new_page()
        for x, y, text in placements:
            page.insert_text((x, y), text)
    doc.save(str(path))
    doc.close()


def run(tmp, pages, **kwargs):
    """Build a fixture, redact it, and return (counts, [page texts])."""
    src, out = Path(tmp) / "in.pdf", Path(tmp) / "out.pdf"
    make_pdf(src, pages)
    counts = redact_ssn.redact_pdf(src, out, **kwargs)
    result = out if out.exists() else src  # no output written when nothing matched
    doc = pymupdf.open(str(result))
    texts = [page.get_text() for page in doc]
    doc.close()
    return counts, texts


_TESTS = []


def test(fn):
    _TESTS.append(fn)
    return fn


# --- SSN ------------------------------------------------------------------

@test
def ssn_dashed_spaced_and_boxed():
    with tempfile.TemporaryDirectory() as tmp:
        counts, texts = run(tmp, [[
            (72, 60, "Dashed: 123-45-6789"),
            (72, 90, "Spaced: 222 33 4444"),
            (72, 120, "Boxed:"),
            (150, 120, "555"), (190, 120, "66"), (220, 120, "7777"),
        ]])
        t = digits(texts[0])
        assert counts["ssn"] == 3, counts
        assert "123456789" not in t
        assert "222334444" not in t
        assert "5556677777" not in t


@test
def ssn_on_a_line_with_other_numbers():
    # A dashed SSN sharing a line with other numbers (a table row) must be
    # redacted without swallowing -- or being defeated by -- its neighbours.
    with tempfile.TemporaryDirectory() as tmp:
        counts, texts = run(tmp, [[
            (72, 60, "Smith, John   123-45-6789   2023   50000"),
        ]])
        t = texts[0]
        assert counts["ssn"] == 1, counts
        assert "123456789" not in digits(t)
        assert "2023" in t and "50000" in t


@test
def ssn_leaves_lookalikes_alone():
    with tempfile.TemporaryDirectory() as tmp:
        counts, texts = run(tmp, [[
            (72, 60, "Phone: 555 123 4567"),   # 3-3-4, not an SSN
            (72, 90, "Bare: 123456789"),       # no separators -> ambiguous
            (72, 120, "Ref 1234 56 7890"),     # 4-2-4
        ]])
        t = texts[0]
        assert counts["ssn"] == 0, counts
        assert "555 123 4567" in t
        assert "123456789" in digits(t)
        assert "7890" in t


# --- bank: routing / account ----------------------------------------------

@test
def bank_value_right_of_and_below_label():
    with tempfile.TemporaryDirectory() as tmp:
        counts, texts = run(tmp, [[
            (72, 60, "Routing number: 021000021"),   # value to the right
            (72, 95, "Account number"),              # value below, in boxes
            (80, 113, "98"), (112, 113, "7654"), (150, 113, "3210"),
        ]], redact_bank=True)
        t = digits(texts[0])
        assert counts["routing"] == 1 and counts["account"] == 1, counts
        assert "021000021" not in t
        assert "9876543210" not in t


@test
def bank_off_by_default():
    with tempfile.TemporaryDirectory() as tmp:
        counts, texts = run(tmp, [[
            (72, 60, "Routing number: 021000021"),
            (72, 90, "Account number: 1234567890"),
        ]])  # no redact_bank
        assert counts["routing"] == 0 and counts["account"] == 0, counts
        assert "021000021" in digits(texts[0])
        assert "1234567890" in digits(texts[0])


@test
def bank_value_followed_by_other_numbers():
    # Account number followed by a date on the same line: redact the account,
    # keep the date.
    with tempfile.TemporaryDirectory() as tmp:
        counts, texts = run(tmp, [[
            (72, 60, "Routing number: 021000021"),
            (72, 90, "Account: 1234567890   opened 12 31 2020"),
        ]], redact_bank=True)
        t = texts[0]
        assert counts["account"] == 1, counts
        assert "1234567890" not in digits(t)
        assert "12 31 2020" in t


@test
def account_in_prose_without_routing_is_ignored():
    with tempfile.TemporaryDirectory() as tmp:
        counts, texts = run(tmp, [[
            (72, 60, "If you have questions about your account, call 800-448-2424"),
        ]], redact_bank=True)
        assert counts["account"] == 0, counts
        assert "800-448-2424" in texts[0]


@test
def account_requires_a_preceding_routing():
    # Same prose account, but now a real routing block precedes it: the real
    # account is redacted, the prose phone after it is not (surplus consumed).
    with tempfile.TemporaryDirectory() as tmp:
        counts, texts = run(tmp, [[
            (72, 60, "Routing number: 021000021"),
            (72, 90, "Account number: 1234567890"),
            (72, 140, "Questions about your account? Call 800-448-2424"),
        ]], redact_bank=True)
        t = texts[0]
        assert counts["routing"] == 1 and counts["account"] == 1, counts
        assert "1234567890" not in digits(t)
        assert "800-448-2424" in t


# --- consistency sweep ----------------------------------------------------

@test
def learned_account_redacted_everywhere_even_unlabeled():
    with tempfile.TemporaryDirectory() as tmp:
        counts, texts = run(tmp, [
            [(72, 60, "Routing number: 021000021"),
             (72, 90, "Account number: 1234567890")],
            [(72, 60, "See reference 1234567890 on file")],  # no label here
        ], redact_bank=True)
        assert counts["account"] == 2, counts  # both occurrences
        assert "1234567890" not in digits(texts[1])


@test
def learned_value_matched_despite_different_formatting():
    # An account learned at its label must still be caught elsewhere when it's
    # punctuated differently or abuts letters -- but NOT when it's merely part
    # of a longer number.
    with tempfile.TemporaryDirectory() as tmp:
        counts, texts = run(tmp, [
            [(72, 60, "Routing number: 021000021"),
             (72, 90, "Account number: 1234567890")],
            [(72, 60, "Ref. 1.234.567.890 on file")],   # dot separators
            [(72, 60, "Code Acct1234567890 end")],       # glued to letters
            [(72, 60, "Order 12345678901234 total")],    # longer number, contains it
        ], redact_bank=True)
        assert counts["account"] == 3, counts  # pages 1-3, not page 4
        assert "1234567890" not in digits(texts[1])          # dot-separated
        assert "1234567890" not in digits(texts[2])          # glued to letters
        assert "end" in texts[2]                             # rest of line kept
        assert "12345678901234" in digits(texts[3])          # longer number untouched


@test
def learned_ssn_redacted_even_when_bare_elsewhere():
    with tempfile.TemporaryDirectory() as tmp:
        counts, texts = run(tmp, [
            [(72, 60, "Taxpayer SSN: 123-45-6789")],     # learned here (dashed)
            [(72, 60, "Internal id 123456789 reference")],  # bare 9 digits
        ])
        assert counts["ssn"] == 2, counts
        assert "123456789" not in digits(texts[1])


# --- cosmetics ------------------------------------------------------------

@test
def value_split_across_boxes_redacts_as_single_strip():
    doc = pymupdf.open()
    page = doc.new_page()
    for x, word in ((80, "98"), (112, "7654"), (150, "3210")):
        page.insert_text((x, 100), word)
    lines = redact_ssn._build_lines(page)
    redacted = set()
    line = lines[0]
    assert redact_ssn._redact_match(page, line["spans"], 0, len(line["text"]), redacted, False)
    redactions = sum(1 for a in page.annots(types=[pymupdf.PDF_ANNOT_REDACT]))
    doc.close()
    assert redactions == 1, f"expected one strip, got {redactions}"


# --- runner ---------------------------------------------------------------

def main():
    failures = 0
    for fn in _TESTS:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - report any failure uniformly
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
        else:
            print(f"PASS {fn.__name__}")
    print(f"\n{len(_TESTS) - failures}/{len(_TESTS)} passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
