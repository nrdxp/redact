#!/usr/bin/env nix-shell
#!nix-shell -i python3 -p python3Packages.pymupdf
"""Redact U.S. Social Security Numbers from a PDF.

Scans every page for SSN-formatted text and paints an opaque box over each
match, removing the underlying text from the document so it cannot be
recovered by copy/paste or text extraction.

The pattern is the 3-2-4 digit grouping with a dash or whitespace between
groups, e.g. ``123-45-6789`` or ``123 45 6789``. The whitespace form also
catches SSNs entered into separate form input boxes, which a PDF exposes as
distinct words. A separator is always required: a bare 9-digit run is too
ambiguous (account numbers, IDs, etc.) to redact safely. Matching happens
within a single line, so groups are never joined across a line break.
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


def _iter_lines(page):
    """Yield each visual line's words, ordered left-to-right.

    "words" yields (x0, y0, x1, y1, text, block_no, line_no, word_no), but
    PyMuPDF's block/line indices can't be trusted to group a visual line:
    SSN digits typed into separate form boxes share a baseline yet land in
    different blocks/lines. So we cluster purely by vertical overlap, which
    rejoins those boxes (and any same-line text) regardless of structure.
    """
    lines = []  # each: [y0, y1, [words...]]
    for word in sorted(page.get_text("words"), key=lambda w: w[1]):
        y0, y1 = word[1], word[3]
        for line in lines:
            overlap = min(y1, line[1]) - max(y0, line[0])
            if overlap > 0.5 * min(y1 - y0, line[1] - line[0]):
                line[0], line[1] = min(line[0], y0), max(line[1], y1)
                line[2].append(word)
                break
        else:
            lines.append([y0, y1, [word]])
    for line in sorted(lines, key=lambda l: l[0]):
        yield sorted(line[2], key=lambda w: w[0])


def redact_ssns(input_pdf, output_pdf, *, dry_run=False, verbose=False):
    """Redact SSNs in *input_pdf*, writing the result to *output_pdf*.

    Returns the number of SSN occurrences found.
    """
    doc = pymupdf.open(input_pdf)
    found = 0

    for page_index, page in enumerate(doc, start=1):
        for words in _iter_lines(page):
            # Rebuild the line's text, tracking each word's character span so
            # a match can be mapped back to the bounding boxes it covers.
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
            line = "".join(parts)

            for match in SSN_REGEX.finditer(line):
                found += 1
                if verbose:
                    print(f"  page {page_index}: {match.group()}", file=sys.stderr)
                if dry_run:
                    continue
                start, end = match.span()
                for s, e, word in spans:
                    if s < end and e > start:  # word overlaps the match
                        page.add_redact_annot(pymupdf.Rect(word[:4]), fill=(0, 0, 0))

        if not dry_run:
            page.apply_redactions()

    if not dry_run and found:
        doc.save(output_pdf, garbage=4, deflate=True)
    doc.close()
    return found


def default_output(input_path):
    """foo.pdf -> foo.redacted.pdf"""
    return input_path.with_suffix(".redacted" + input_path.suffix)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Redact U.S. Social Security Numbers (NNN-NN-NNNN) from a PDF.",
    )
    parser.add_argument("input", type=Path, help="path to the source PDF")

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
        help="report how many SSNs would be redacted without writing a file",
    )
    parser.add_argument(
        "-f", "--force", action="store_true",
        help="overwrite the output file if it already exists",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="print each matched SSN and its page",
    )
    return parser.parse_args(argv)


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
            found = redact_ssns(args.input, tmp, verbose=args.verbose)
            if found:
                tmp.replace(args.input)
            elif tmp.exists():
                tmp.unlink()
        else:
            found = redact_ssns(
                args.input, output, dry_run=args.dry_run, verbose=args.verbose,
            )
    except Exception as exc:  # pymupdf raises a variety of error types
        sys.exit(f"error: failed to process {args.input}: {exc}")

    if args.dry_run:
        print(f"{found} SSN(s) found in {args.input} (dry run, nothing written)")
    elif found:
        print(f"Redacted {found} SSN(s) -> {output}")
    else:
        print(f"No SSNs found in {args.input}; no output written")


if __name__ == "__main__":
    main()
