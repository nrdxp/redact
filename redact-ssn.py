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

# An account number is meaningless without a routing number, and on a form the
# two appear together (routing first). So we pair them: each routing number
# redacted adds to a running surplus, and an account number is only redacted
# when an unpaired routing precedes it. This rejects the word "account" in
# prose (e.g. "questions about your account, call 800-..."), which has no
# routing match before it.


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
    """Redact every word overlapping the character range [start, end) with a
    single rectangle spanning them all -- one flat strip rather than a box per
    word, so a value split across boxes reads the same as a contiguous one.

    Returns True if at least one not-yet-marked word was hit, so callers don't
    double-count a value already covered by another rule. The *redacted* set is
    updated even in dry-run so dedup/counts stay consistent; only the actual
    annotation is skipped.
    """
    words = [word for s, e, word in spans if s < end and e > start]
    if not any(word[:4] not in redacted for word in words):
        return False
    for word in words:
        redacted.add(word[:4])
    if not dry_run:
        strip = pymupdf.Rect(
            min(w[0] for w in words), min(w[1] for w in words),
            max(w[2] for w in words), max(w[3] for w in words),
        )
        page.add_redact_annot(strip, fill=(0, 0, 0))
    return True


def _find_value(words, kind):
    """First qualifying value among *words* (reading order), or None.

    Returns (text, digits) where *digits* is the value with separators
    stripped. We stop at the first qualifying run so a far-off number in
    another column isn't taken in place of the value belonging to the label.
    """
    text = " ".join(w[4] for w in words)
    for vm in VALUE_REGEX.finditer(text):
        digits = re.sub(r"\D", "", vm.group())
        if _qualifies(kind, digits):
            return vm.group().strip(), digits
    return None


def _iter_bank_values(lines, surplus):
    """Yield (kind, digits) for each account/routing value anchored to a label.

    For each label, the value lives either to its right on the same line or in
    the column beneath it (forms vary), so we take the first qualifying number
    found in those two places, in that order. Account labels are only honored
    when an unpaired routing number precedes them; *surplus* is the running
    count of such routing matches and is mutated in place (a one-element list).
    """
    for i, line in enumerate(lines):
        for lm in LABEL_REGEX.finditer(line["text"]):
            kind = _label_kind(lm.group("kind"))
            label_words = [w for s, e, w in line["spans"] if s < lm.end() and e > lm.start()]
            if not label_words:
                continue
            # An account with no preceding routing is almost certainly the word
            # "account" in prose, not a real account number -- skip it.
            if kind == "account" and surplus[0] <= 0:
                continue
            lx0 = min(w[0] for w in label_words)
            lx1 = max(w[2] for w in label_words)

            # To the right, on the same line.
            right = sorted(
                (w for w in line["words"] if w[0] >= lx1 - 1 and w not in label_words),
                key=lambda w: w[0],
            )
            found = _find_value(right, kind)

            # Otherwise, in roughly the same column beneath the label. Scan only
            # a couple of lines down and stop at the first value, so an
            # unrelated number further down (a date, an invoice no.) isn't swept
            # in.
            if not found:
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
                    found = _find_value(column, kind)
                    if found:
                        break

            if found:
                surplus[0] += 1 if kind == "routing" else -1
                yield kind, found[1]


def learn_sequences(page_lines, redact_bank):
    """Collect the sensitive digit-sequences in a document via context rules.

    Returns a dict of normalized digits -> kind ('ssn'/'routing'/'account').
    SSNs are learned from their 3-2-4 pattern; account/routing numbers from a
    nearby label. These are the sequences the sweep then redacts everywhere.
    """
    known = {}
    surplus = [0]  # unpaired routing matches so far, in document order
    for lines in page_lines:
        for line in lines:
            for match in SSN_REGEX.finditer(line["text"]):
                known.setdefault(re.sub(r"\D", "", match.group()), "ssn")
        if redact_bank:
            for kind, digits in _iter_bank_values(lines, surplus):
                known.setdefault(digits, kind)
    return known


def redact_pdf(input_pdf, output_pdf, *, redact_bank=False, dry_run=False, verbose=False):
    """Redact sensitive data in *input_pdf*, writing to *output_pdf*.

    Works in two passes: first learn each sensitive number from the contextual
    rules (SSN pattern, bank labels), then redact every occurrence of those
    digit-sequences anywhere in the document -- ignoring separators, and
    regardless of whether that occurrence matches a rule. This keeps a given
    number redacted consistently throughout, even where it lacks the context
    that first identified it. Returns a dict of per-category counts.
    """
    doc = pymupdf.open(input_pdf)
    page_lines = [_build_lines(page) for page in doc]
    known = learn_sequences(page_lines, redact_bank)

    # One matcher per learned sequence: its digits in order, tolerant of any
    # separators between them (space, dash, dot, slash, comma, etc. -- ignored),
    # so the same number is caught however it's punctuated elsewhere. Bounds are
    # digit-only lookarounds rather than \b: that still prevents matching as
    # part of a longer *number*, but -- unlike \b -- still matches when the
    # number abuts a letter (e.g. "Acct1234567890"). Searching for the sequence
    # itself (rather than scanning for maximal digit-runs and comparing) also
    # lets a value sitting next to other numbers match exactly, without its
    # neighbours being merged in and changing the digits.
    sep = r"[^0-9A-Za-z]*"
    patterns = [
        (re.compile(r"(?<!\d)" + sep.join(seq) + r"(?!\d)"), kind)
        for seq, kind in known.items()
    ]

    counts = {"ssn": 0, "routing": 0, "account": 0}
    for page_index, lines in enumerate(page_lines):
        page = doc[page_index]
        redacted = set()  # word boxes already marked on this page
        for line in lines:
            for pattern, kind in patterns:
                for m in pattern.finditer(line["text"]):
                    if _redact_match(page, line["spans"], m.start(), m.end(), redacted, dry_run):
                        counts[kind] += 1
                        if verbose:
                            print(f"  page {page_index + 1}: {kind} {m.group().strip()}", file=sys.stderr)
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
