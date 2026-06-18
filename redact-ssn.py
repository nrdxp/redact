#!/usr/bin/env nix-shell
#!nix-shell -i python3 -p python3Packages.pymupdf tesseract
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
import glob
import os
import re
import shutil
import sys
from pathlib import Path

import pymupdf

# 3-2-4 digit grouping separated by dash(es) or spaces. Words on a line are
# rejoined with single spaces before matching, so " " covers the box/spaced
# layout and "-" covers the dashed layout (or any mix of the two).
SSN_REGEX = re.compile(r"\b\d{3}[ -]+\d{2}[ -]+\d{4}\b")

# Bank-info labels. We only redact account/routing numbers that sit next to
# one of these, since the digits alone are too ambiguous to match safely. The
# keyword stems are matched loosely (rout\w*, acco\w*) so OCR slips like
# "Routini" for "Routing" are still recognized. The trailing
# "no"/"number"/"#" suffix is optional but, when present, marks a field
# heading (used to gate the more aggressive region fallback below).
LABEL_REGEX = re.compile(
    r"\b(?P<kind>rout\w*|rtn|aba|acco\w*|acct)\b\.?"
    r"(?P<suffix>\s*(?:number|num|no|#))?\.?",
    re.I,
)

# Layout constants for the OCR region fallback (in PDF points, so they don't
# get distorted by OCR's inflated line heights).
BAND_MAX_PT = 28      # how far below a heading to redact when its value is unreadable
MIN_BAND_PT = 12      # ... but always at least this far (never an empty band)
REGION_MARGIN = 36    # right-side page margin left untouched by a region redaction
FLATTEN_DPI = 200     # resolution when replacing an OCR'd page with a redacted image

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


OCR_DPI = 300  # render resolution for OCR; high enough for small digits


def _find_tessdata():
    """Locate Tesseract's tessdata dir (env var, PATH, or the nix store)."""
    env = os.environ.get("TESSDATA_PREFIX")
    if env and os.path.exists(os.path.join(env, "eng.traineddata")):
        return env
    exe = shutil.which("tesseract")
    if exe:
        cand = os.path.normpath(os.path.join(os.path.dirname(exe), "..", "share", "tessdata"))
        if os.path.exists(os.path.join(cand, "eng.traineddata")):
            return cand
    for cand in glob.glob("/nix/store/*tesseract*/share/tessdata"):
        if os.path.exists(os.path.join(cand, "eng.traineddata")):
            return cand
    return None


def _looks_garbled(text):
    """True if *text* is mostly unreadable -- a sign the page's font has no
    usable ToUnicode map, so extraction yields scrambled characters even
    though the glyphs render fine. Control and C1 characters (rare in real
    text) are the tell.
    """
    if not text:
        return False
    bad = sum(
        1 for c in text
        if (ord(c) < 0x20 and c not in "\t\n\r\f") or 0x7F <= ord(c) <= 0x9F
    )
    return bad / len(text) > 0.01


def _should_ocr(normal_words, ocr):
    """Whether to add OCR coverage for a page.

    *ocr* is None (never), "auto" (only when the embedded text is empty or
    garbled), or "all" (always). OCR is additive -- it never replaces the
    embedded text layer, only supplements it where that text is unreadable.
    """
    if ocr == "all":
        return True
    if ocr == "auto":
        text = "".join(w[4] for w in normal_words)
        return not text.strip() or _looks_garbled(text)
    return False


def _ocr_words(page, tessdata):
    """Words recognized by rendering the page and OCRing it. Used only to learn
    *where* content is (for redaction); the page itself is never modified."""
    tp = page.get_textpage_ocr(flags=0, dpi=OCR_DPI, full=True, tessdata=tessdata)
    return page.get_text("words", textpage=tp)


def _build_lines(page, words=None):
    """Return visual lines, each with its words, reconstructed text, and a
    char-offset -> bounding-box map.

    "words" yields (x0, y0, x1, y1, text, block_no, line_no, word_no), but
    PyMuPDF's block/line indices can't be trusted to group a visual line:
    digits typed into separate form boxes share a baseline yet land in
    different blocks/lines. So we cluster purely by vertical overlap, which
    rejoins those boxes (and any same-line text) regardless of structure.
    """
    if words is None:
        words = page.get_text("words")
    clusters = []  # each: [y0, y1, [words...]]
    for word in sorted(words, key=lambda w: w[1]):
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


def _redact_words(page, words, redacted, dry_run):
    """Redact *words* with a single rectangle spanning them all -- one flat
    strip rather than a box per word, so a value split across boxes reads the
    same as a contiguous one.

    Returns True if at least one not-yet-marked word was hit, so callers don't
    double-count a value already covered by another rule. The *redacted* set is
    updated even in dry-run so dedup/counts stay consistent; only the actual
    annotation is skipped.
    """
    if not words or not any(word[:4] not in redacted for word in words):
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


def _redact_match(page, spans, start, end, redacted, dry_run):
    """Redact the words overlapping the character range [start, end)."""
    words = [word for s, e, word in spans if s < end and e > start]
    return _redact_words(page, words, redacted, dry_run)


def _redact_region(page, rect, redacted, dry_run):
    """Redact a geometric rectangle (used when a value's digits can't be read,
    so we cover the area they occupy). De-duplicated by rounded coordinates."""
    key = ("region", round(rect.x0), round(rect.y0), round(rect.x1), round(rect.y1))
    if key in redacted:
        return False
    redacted.add(key)
    if not dry_run:
        page.add_redact_annot(rect, fill=(0, 0, 0))
    return True


def _flatten_page_redactions(page):
    """Apply this page's pending redaction annotations by replacing the whole
    page with a rendered image that has the redactions painted in.

    Used for OCR'd pages: their fonts are broken (that's why we OCR'd), and
    PyMuPDF's normal apply_redactions rewrites the content stream, which mangles
    that broken text and drops unrelated content. Rendering to an image instead
    keeps the page looking exactly as it did, drops the text layer entirely (so
    nothing -- redacted or not -- is copyable), and bakes the black boxes in.
    """
    boxes = [annot.rect for annot in page.annots(types=[pymupdf.PDF_ANNOT_REDACT])]
    if not boxes:
        return
    for annot in list(page.annots(types=[pymupdf.PDF_ANNOT_REDACT])):
        page.delete_annot(annot)
    shape = page.new_shape()
    for rect in boxes:
        shape.draw_rect(rect)
    shape.finish(fill=(0, 0, 0), color=(0, 0, 0))
    shape.commit()
    pix = page.get_pixmap(dpi=FLATTEN_DPI)
    # Wipe all existing content (text/vector/images), then lay the render back as
    # the page's only content -- an image, with no recoverable text.
    page.add_redact_annot(page.rect)
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_REMOVE)
    page.insert_image(page.rect, pixmap=pix)


def _find_value(words, kind):
    """First qualifying value among *words* (reading order), or None.

    Returns (digits, matched_words) where *digits* is the value with
    separators stripped and *matched_words* are the exact boxes it covers (so
    the caller can redact them directly). We stop at the first qualifying run
    so a far-off number in another column isn't taken in place of the value
    belonging to the label.
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
        if _qualifies(kind, digits):
            matched = [w for s, e, w in spans if s < vm.end() and e > vm.start()]
            return digits, matched
    return None


def _iter_bank(lines, page_rect, surplus, positional):
    """Yield bank redactions for each account/routing label, in document order.

    Each item is ("value", kind, digits, words) when the value's digits could
    be read (the caller learns the digits and redacts those boxes), or
    ("region", kind, None, rect) when they couldn't but *positional* is set --
    the latter covers the area beneath a field heading whose digits are
    unreadable (e.g. entered into drawn boxes on an OCR'd page).

    For each label, the value lives either to its right on the same line or in
    the column beneath it (forms vary), so we take the first qualifying number
    found in those two places, in that order. Account labels are only honored
    when an unpaired routing number precedes them; *surplus* is the running
    count of such routing matches and is mutated in place (a one-element list).
    """
    # Only real field headings (label + a "Number"/"#" suffix) bound a region
    # band. Bounding by any label-ish match would let stray "rout"/"acco"
    # substrings in garbled OCR text truncate the band before it reaches the
    # boxes.
    heading_ys = sorted(
        line["y0"] for line in lines
        if any(lm.group("suffix") for lm in LABEL_REGEX.finditer(line["text"]))
    )
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
            line_h = max(line["y1"] - line["y0"], 1)
            if not found:
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
                digits, words = found
                yield "value", kind, digits, words
            elif positional and lm.group("suffix"):
                # Couldn't read the digits, but this is a field heading ("...
                # Number") on a page we had to OCR -- the value is almost
                # certainly in drawn boxes OCR can't read. Cover from the heading
                # down a fixed band (capped at the next heading), full width, so
                # the boxed digits are covered whether they sit to the right of
                # the heading or beneath it -- even though we never read them.
                lower = [y for y in heading_ys if y > line["y0"] + 1]
                y_bot = min([line["y1"] + BAND_MAX_PT] + lower[:1])
                y_bot = max(y_bot, line["y1"] + MIN_BAND_PT)  # never an empty band
                rect = pymupdf.Rect(lx0, line["y0"], page_rect.width - REGION_MARGIN, y_bot)
                surplus[0] += 1 if kind == "routing" else -1
                yield "region", kind, None, rect


def redact_pdf(input_pdf, output_pdf, *, redact_bank=False, ocr=None, dry_run=False, verbose=False):
    """Redact sensitive data in *input_pdf*, writing to *output_pdf*.

    Works in two passes. The first applies the contextual rules (SSN pattern,
    bank labels) and redacts each value it finds *directly*, using the exact
    boxes the rule matched -- so nothing the rules identify can be lost, even in
    layouts the text sweep can't reconstruct -- while recording its digit-
    sequence. The second sweeps the whole document and redacts every *other*
    occurrence of those sequences, ignoring separators, so a given number stays
    redacted consistently throughout even where it lacks identifying context.

    *ocr* (None / "auto" / "all") controls OCR for pages whose embedded text is
    unreadable (broken font encoding) or absent (scanned). Returns a dict of
    per-category counts.
    """
    doc = pymupdf.open(input_pdf)
    tessdata = _find_tessdata() if ocr else None

    # Per page, build one or more "views" to match against. The embedded text
    # is always a view; on pages whose text is unreadable, OCR is added as a
    # second view. OCR is strictly additive -- it never replaces the text layer,
    # so normal matching keeps working everywhere. Each view carries whether its
    # boxes came from OCR (which enables the region fallback for that view).
    page_views = []  # page_index -> list of (lines, from_ocr)
    page_was_ocr = []  # whether OCR was added for each page
    for page_index, page in enumerate(doc):
        normal = page.get_text("words")
        views = [(_build_lines(page, normal), False)]
        used_ocr = bool(ocr and _should_ocr(normal, ocr))
        if used_ocr:
            if verbose:
                print(f"  page {page_index + 1}: adding OCR coverage", file=sys.stderr)
            views.append((_build_lines(page, _ocr_words(page, tessdata)), True))
        page_views.append(views)
        page_was_ocr.append(used_ocr)

    counts = {"ssn": 0, "routing": 0, "account": 0}
    page_redacted = [set() for _ in page_views]  # boxes marked, per page

    def record(page_index, kind, ok, label):
        if ok:
            counts[kind] += 1
            if verbose:
                print(f"  page {page_index + 1}: {kind} {label}", file=sys.stderr)

    # Pass 1: contextual rules, redacting exactly what they find.
    known = {}  # normalized digits -> kind
    surplus = [0]  # unpaired routing matches so far, in document order
    for page_index, views in enumerate(page_views):
        page = doc[page_index]
        redacted = page_redacted[page_index]
        for lines, from_ocr in views:
            for line in lines:
                for m in SSN_REGEX.finditer(line["text"]):
                    known.setdefault(re.sub(r"\D", "", m.group()), "ssn")
                    ok = _redact_match(page, line["spans"], m.start(), m.end(), redacted, dry_run)
                    record(page_index, "ssn", ok, m.group().strip())
            if redact_bank:
                for typ, kind, digits, payload in _iter_bank(
                    lines, page.rect, surplus, positional=from_ocr
                ):
                    if typ == "value":
                        known.setdefault(digits, kind)
                        ok = _redact_words(page, payload, redacted, dry_run)
                        record(page_index, kind, ok, digits)
                    else:  # region: digits unreadable, cover the heading's value area
                        ok = _redact_region(page, payload, redacted, dry_run)
                        record(page_index, kind, ok, f"[unreadable region under {kind} heading]")

    # Pass 2: sweep every other occurrence of a learned sequence. One matcher
    # per sequence: its digits in order, tolerant of any separators between them
    # (space, dash, dot, comma, etc. -- ignored), so the same number is caught
    # however it's punctuated. Bounds are digit-only lookarounds rather than \b:
    # that still prevents matching as part of a longer *number*, but -- unlike
    # \b -- still matches when the number abuts a letter (e.g. "Acct1234567890").
    sep = r"[^0-9A-Za-z]*"
    patterns = [
        (re.compile(r"(?<!\d)" + sep.join(seq) + r"(?!\d)"), kind)
        for seq, kind in known.items()
    ]
    for page_index, views in enumerate(page_views):
        page = doc[page_index]
        redacted = page_redacted[page_index]
        for lines, from_ocr in views:
            for line in lines:
                for pattern, kind in patterns:
                    for m in pattern.finditer(line["text"]):
                        ok = _redact_match(page, line["spans"], m.start(), m.end(), redacted, dry_run)
                        record(page_index, kind, ok, m.group().strip())

    # Apply only on pages that actually have a redaction, so untouched pages are
    # left exactly as they were. OCR'd pages (broken fonts) are flattened to an
    # image so the content-stream rewrite can't mangle their text; other pages
    # use a normal in-place redaction, which keeps their text selectable. Either
    # way the underlying content is removed (not copyable), not just hidden.
    if not dry_run:
        for page_index in range(len(page_views)):
            page = doc[page_index]
            if next(page.annots(types=[pymupdf.PDF_ANNOT_REDACT]), None) is None:
                continue
            if page_was_ocr[page_index]:
                _flatten_page_redactions(page)
            else:
                page.apply_redactions(
                    text=pymupdf.PDF_REDACT_TEXT_REMOVE,
                    images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
                )

    if not dry_run and sum(counts.values()):
        doc.save(output_pdf, garbage=4, deflate=True)
    doc.close()
    return counts


def dump_lines(input_pdf, file=sys.stdout, ocr=None):
    """Print the exact text the matcher sees: each page's visual lines (the
    reconstructed text both passes run against) plus the underlying word boxes
    and their x-positions. Use this to see how a stubborn value is laid out --
    e.g. whether its digits land on one line or get split across several -- and
    share it safely by X-ing out the actual digits. Pass *ocr* to dump what OCR
    sees on pages whose embedded text is unreadable.
    """
    doc = pymupdf.open(input_pdf)
    tessdata = _find_tessdata() if ocr else None
    for page_index, page in enumerate(doc, start=1):
        normal = page.get_text("words")
        views = [("text", _build_lines(page, normal))]
        if ocr and _should_ocr(normal, ocr):
            views.append(("OCR", _build_lines(page, _ocr_words(page, tessdata))))
        for tag, lines in views:
            print(f"=== page {page_index} [{tag}] ({len(lines)} lines) ===", file=file)
            for ln in lines:
                print(f"  y={ln['y0']:7.1f}  text: {ln['text']!r}", file=file)
                boxes = "  ".join(f"{w[4]}@{w[0]:.0f}" for w in ln["words"])
                print(f"             boxes: {boxes}", file=file)
    doc.close()


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
    parser.add_argument(
        "--ocr", action="store_true",
        help="OCR pages whose text layer is unreadable (broken font encoding) or absent (scanned)",
    )
    parser.add_argument(
        "--ocr-all", action="store_true",
        help="OCR every page, ignoring the embedded text (slow; use if --ocr misses a page)",
    )
    parser.add_argument(
        "--dump", action="store_true",
        help="print the reconstructed text the matcher sees, then exit (for diagnosing misses)",
    )
    return parser.parse_args(argv)


def _ocr_mode(args):
    return "all" if args.ocr_all else "auto" if args.ocr else None


def _summary(counts):
    """Render per-category counts, e.g. '3 SSN, 1 routing, 2 account'."""
    labels = [("ssn", "SSN"), ("routing", "routing"), ("account", "account")]
    parts = [f"{counts[key]} {name}" for key, name in labels if counts[key]]
    return ", ".join(parts) if parts else "nothing"


def main(argv=None):
    args = parse_args(argv)

    if not args.input.is_file():
        sys.exit(f"error: input file not found: {args.input}")

    ocr = _ocr_mode(args)

    if args.dump:
        dump_lines(args.input, ocr=ocr)
        return

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
            counts = redact_pdf(args.input, tmp, redact_bank=args.bank, ocr=ocr, verbose=args.verbose)
            if sum(counts.values()):
                tmp.replace(args.input)
            elif tmp.exists():
                tmp.unlink()
        else:
            counts = redact_pdf(
                args.input, output, redact_bank=args.bank, ocr=ocr,
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
