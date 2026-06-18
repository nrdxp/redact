#!/usr/bin/env nix-shell
#!nix-shell -i python3 -p python3Packages.pymupdf
"""Redact sensitive data from a PDF.

Scans every page and paints an opaque box over each match, removing the
underlying text from the document so it cannot be recovered by copy/paste or
text extraction.

SSNs (always redacted) are matched as the 3-2-4 digit grouping with a dash or
whitespace between groups, e.g. ``123-45-6789`` or ``123 45 6789``. The
whitespace form also catches SSNs entered into separate form input boxes,
which a PDF exposes as distinct words. A separator is always required: a bare
9-digit run is too ambiguous (account numbers, IDs, etc.) to redact safely.

With --bank, account and routing numbers are also redacted, but only when
anchored to a nearby label ("account", "routing", etc.) -- their digits alone
are too ambiguous to match without that context. The value is sought both to
the right of the label and in the column beneath it.

Matching happens within a single visual line (words clustered by vertical
position), so groups are never joined across a line break.
"""
import argparse
import re
import sys
from pathlib import Path

import pymupdf

# 3-2-4 digit grouping separated by dash(es) or spaces. Words on a line are
# rejoined with single spaces before matching, so " " covers the box/spaced
# layout and "-" covers the dashed layout (or any mix of the two).
SSN_REGEX = re.compile(r"\b\d{3}[ -]+\d{2}[ -]+\d{4}\b")

# Bank-info labels. We only redact account/routing numbers that sit next to
# one of these, since the digits alone are too ambiguous to match safely.
# The trailing "no"/"number"/"#" and punctuation are tolerated but optional.
LABEL_REGEX = re.compile(
    r"\b(?P<kind>account|acct|routing|rtn|aba)\b\.?"
    r"(?:\s*(?:number|num|no|#))?\.?",
    re.I,
)

# A run of digits with optional space/dash separators (e.g. "12 3456 789",
# "021-000-021"); the count of digits is checked separately per field type.
VALUE_REGEX = re.compile(r"\d(?:[ -]*\d)*")

# Account numbers have no fixed length; clamp to a plausible range so a stray
# year or line number next to the label isn't mistaken for one.
ACCOUNT_MIN_DIGITS = 4
ACCOUNT_MAX_DIGITS = 17


def _label_kind(text):
    """'routing' for any routing-ish keyword, else 'account'."""
    return "routing" if re.match(r"rout|rtn|aba", text, re.I) else "account"


def _qualifies(kind, digits):
    """Whether a digit string is the right shape for *kind*."""
    if kind == "routing":
        return len(digits) == 9  # ABA routing numbers are always 9 digits
    return ACCOUNT_MIN_DIGITS <= len(digits) <= ACCOUNT_MAX_DIGITS


def _build_lines(page):
    """Return visual lines, each with its words, reconstructed text, and a
    char-offset -> bounding-box map.

    "words" yields (x0, y0, x1, y1, text, block_no, line_no, word_no), but
    PyMuPDF's block/line indices can't be trusted to group a visual line:
    digits typed into separate form boxes share a baseline yet land in
    different blocks/lines. So we cluster purely by vertical overlap, which
    rejoins those boxes (and any same-line text) regardless of structure.
    """
    clusters = []  # each: [y0, y1, [words...]]
    for word in sorted(page.get_text("words"), key=lambda w: w[1]):
        y0, y1 = word[1], word[3]
        for c in clusters:
            overlap = min(y1, c[1]) - max(y0, c[0])
            if overlap > 0.5 * min(y1 - y0, c[1] - c[0]):
                c[0], c[1] = min(c[0], y0), max(c[1], y1)
                c[2].append(word)
                break
        else:
            clusters.append([y0, y1, [word]])

    lines = []
    for y0, y1, words in sorted(clusters, key=lambda c: c[0]):
        words = sorted(words, key=lambda w: w[0])
        spans = []  # (start, end, word)
        parts = []
        pos = 0
        for i, word in enumerate(words):
            if i:
                parts.append(" ")
                pos += 1
            text = word[4]
            spans.append((pos, pos + len(text), word))
            parts.append(text)
            pos += len(text)
        lines.append({"y0": y0, "y1": y1, "words": words, "text": "".join(parts), "spans": spans})
    return lines


def _redact_match(page, spans, start, end, redacted, dry_run):
    """Mark every word overlapping the character range [start, end).

    Returns True if at least one not-yet-marked word was hit, so callers don't
    double-count a value already covered by another rule. The *redacted* set is
    updated even in dry-run so dedup/counts stay consistent; only the actual
    annotation is skipped.
    """
    hit = False
    for s, e, word in spans:
        if s < end and e > start:
            key = word[:4]
            if key not in redacted:
                redacted.add(key)
                if not dry_run:
                    page.add_redact_annot(pymupdf.Rect(word[:4]), fill=(0, 0, 0))
                hit = True
    return hit


def _first_value(words, kind, page, redacted, dry_run):
    """Redact the first qualifying value among *words* (reading order).

    Returns the matched text, or None. We stop at the first qualifying run so
    a far-off number in another column doesn't get swept in with the value
    that actually belongs to the label.
    """
    spans = []
    parts = []
    pos = 0
    for i, word in enumerate(words):
        if i:
            parts.append(" ")
            pos += 1
        text = word[4]
        spans.append((pos, pos + len(text), word))
        parts.append(text)
        pos += len(text)
    text = "".join(parts)

    for vm in VALUE_REGEX.finditer(text):
        digits = re.sub(r"\D", "", vm.group())
        if _qualifies(kind, digits) and _redact_match(page, spans, vm.start(), vm.end(), redacted, dry_run):
            return vm.group().strip()
    return None


def _redact_bank(page, lines, redacted, hits, dry_run):
    """Redact account/routing numbers anchored to a nearby label.

    For each label, look for the value both to its right on the same line and
    in the column beneath it (the layout varies by form), redacting the first
    qualifying number found in each place.
    """
    for i, line in enumerate(lines):
        for lm in LABEL_REGEX.finditer(line["text"]):
            kind = _label_kind(lm.group("kind"))
            label_words = [w for s, e, w in line["spans"] if s < lm.end() and e > lm.start()]
            if not label_words:
                continue
            lx0 = min(w[0] for w in label_words)
            lx1 = max(w[2] for w in label_words)

            # To the right, on the same line.
            right = sorted(
                (w for w in line["words"] if w[0] >= lx1 - 1 and w not in label_words),
                key=lambda w: w[0],
            )
            value = _first_value(right, kind, page, redacted, dry_run)
            if value:
                hits.append((kind, value))

            # Below, in roughly the same column (value sits under the label).
            # Scan downward only a couple of lines and stop at the first that
            # yields a value, so an unrelated number further down (a date, an
            # invoice no.) in the same column isn't swept in.
            line_h = max(line["y1"] - line["y0"], 1)
            pad = max(lx1 - lx0, 50)
            for other in lines[i + 1:]:
                if other["y0"] <= line["y1"]:
                    continue
                if other["y0"] > line["y1"] + 2 * line_h:
                    break
                column = sorted(
                    (w for w in other["words"] if lx0 - 10 < w[0] < lx1 + pad),
                    key=lambda w: w[0],
                )
                value = _first_value(column, kind, page, redacted, dry_run)
                if value:
                    hits.append((kind, value))
                    break


def redact_pdf(input_pdf, output_pdf, *, redact_bank=False, dry_run=False, verbose=False):
    """Redact sensitive data in *input_pdf*, writing to *output_pdf*.

    Always redacts SSNs; also redacts routing/account numbers when
    *redact_bank* is set. Returns a dict of per-category counts.
    """
    doc = pymupdf.open(input_pdf)
    counts = {"ssn": 0, "routing": 0, "account": 0}

    for page_index, page in enumerate(doc, start=1):
        lines = _build_lines(page)
        redacted = set()  # word boxes already marked on this page

        for line in lines:
            for match in SSN_REGEX.finditer(line["text"]):
                counts["ssn"] += 1
                if verbose:
                    print(f"  page {page_index}: SSN {match.group()}", file=sys.stderr)
                _redact_match(page, line["spans"], match.start(), match.end(), redacted, dry_run)

        if redact_bank:
            hits = []
            _redact_bank(page, lines, redacted, hits, dry_run)
            for kind, value in hits:
                counts[kind] += 1
                if verbose:
                    print(f"  page {page_index}: {kind} {value}", file=sys.stderr)

        if not dry_run:
            page.apply_redactions()

    if not dry_run and sum(counts.values()):
        doc.save(output_pdf, garbage=4, deflate=True)
    doc.close()
    return counts


def default_output(input_path):
    """foo.pdf -> foo.redacted.pdf"""
    return input_path.with_suffix(".redacted" + input_path.suffix)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Redact SSNs (and optionally bank account/routing numbers) from a PDF.",
    )
    parser.add_argument("input", type=Path, help="path to the source PDF")

    parser.add_argument(
        "--bank", action="store_true",
        help="also redact account and routing numbers found next to a label",
    )

    dest = parser.add_mutually_exclusive_group()
    dest.add_argument(
        "-o", "--output", type=Path,
        help="output path (default: <input>.redacted.pdf)",
    )
    dest.add_argument(
        "--in-place", action="store_true",
        help="overwrite the input file with the redacted version",
    )

    parser.add_argument(
        "-n", "--dry-run", action="store_true",
        help="report what would be redacted without writing a file",
    )
    parser.add_argument(
        "-f", "--force", action="store_true",
        help="overwrite the output file if it already exists",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="print each matched item and its page",
    )
    return parser.parse_args(argv)


def _summary(counts):
    """Render per-category counts, e.g. '3 SSN, 1 routing, 2 account'."""
    labels = [("ssn", "SSN"), ("routing", "routing"), ("account", "account")]
    parts = [f"{counts[key]} {name}" for key, name in labels if counts[key]]
    return ", ".join(parts) if parts else "nothing"


def main(argv=None):
    args = parse_args(argv)

    if not args.input.is_file():
        sys.exit(f"error: input file not found: {args.input}")

    if args.dry_run:
        output = None
    elif args.in_place:
        output = args.input
    elif args.output:
        output = args.output
    else:
        output = default_output(args.input)

    # Don't clobber an existing distinct file unless asked to.
    if output and not args.force and output != args.input and output.exists():
        sys.exit(f"error: output already exists (use --force to overwrite): {output}")

    try:
        if args.in_place and not args.dry_run:
            # Redact to a temp file first so a failure can't corrupt the original.
            tmp = args.input.with_suffix(args.input.suffix + ".tmp")
            counts = redact_pdf(args.input, tmp, redact_bank=args.bank, verbose=args.verbose)
            if sum(counts.values()):
                tmp.replace(args.input)
            elif tmp.exists():
                tmp.unlink()
        else:
            counts = redact_pdf(
                args.input, output, redact_bank=args.bank,
                dry_run=args.dry_run, verbose=args.verbose,
            )
    except Exception as exc:  # pymupdf raises a variety of error types
        sys.exit(f"error: failed to process {args.input}: {exc}")

    summary = _summary(counts)
    if args.dry_run:
        print(f"Found {summary} in {args.input} (dry run, nothing written)")
    elif sum(counts.values()):
        print(f"Redacted {summary} -> {output}")
    else:
        print(f"No sensitive data found in {args.input}; no output written")


if __name__ == "__main__":
    main()
