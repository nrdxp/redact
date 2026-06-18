# redact-ssn

A small, self-contained CLI that redacts U.S. Social Security Numbers — and,
optionally, bank account/routing numbers or any values you name — from a PDF. It
removes the underlying text, not just drawing a box over it, so the content can't
be recovered by copy/paste or text extraction.

## Usage

The script is self-contained: the `nix-shell` shebang pulls in its dependencies
(`pymupdf`, plus `tesseract` for `--ocr`) on first run, so there's nothing to
install.

```sh
./redact-ssn.py return.pdf                 # -> return.redacted.pdf (won't clobber the original)
./redact-ssn.py return.pdf --bank          # also redact account + routing numbers
./redact-ssn.py return.pdf -r "Jane Doe" -r 555123456   # also redact these exact values
./redact-ssn.py return.pdf -o clean.pdf    # explicit output path
./redact-ssn.py return.pdf --in-place      # overwrite the original (written via a temp file first)
./redact-ssn.py return.pdf --dry-run -v    # list what would be redacted, write nothing
./redact-ssn.py return.pdf --force         # overwrite an existing output file
./redact-ssn.py return.pdf --dump          # print the text the matcher sees, then exit
```

`--dump` prints, per page, each reconstructed visual line (the exact text both
passes match against) plus the underlying word boxes and their x-positions. Use
it to diagnose a value that isn't being caught — it shows whether the digits
land on one line or get split across several — and it's safe to share once you
X out the actual digits.

## Unreadable pages (`--ocr`)

Some PDFs — tax-software worksheets are a common offender — contain pages whose
font has no usable character map. The glyphs render fine (so it looks like text
and copies in a viewer), but the *extracted* text is scrambled gibberish, so the
matcher never sees the real digits and can't redact them. Scanned pages have the
same problem with no text layer at all.

`--ocr` handles both: it detects pages whose text is garbled or missing and
**OCRs them** (renders the page and recognizes the glyphs) to find where the
numbers are. Because redaction blanks a *rectangle*, this removes the real
rendered digits regardless of the broken encoding underneath.

OCR is **strictly additive**: it never replaces a page's real text layer, it
only adds coverage where that text is unreadable. So normal text matching keeps
working everywhere, even on pages that also get OCR'd. Pages with nothing to
redact are written back untouched.

How an OCR'd page is redacted matters: its font is broken (that's why we OCR'd),
and PyMuPDF's normal in-place redaction rewrites the content stream, which
mangles that broken text and can drop unrelated content. So an OCR'd page that
needs redacting is instead **flattened to an image** — rendered exactly as it
looks, with the black boxes painted in, and no text layer at all. The page looks
identical, nothing on it is copyable, and nothing else is disturbed. (Normal
pages keep their selectable text and are redacted in place as before.)

```sh
./redact-ssn.py return.pdf --bank --ocr            # OCR only the unreadable pages
./redact-ssn.py return.pdf --bank --ocr-all        # OCR every page (slower; if --ocr misses one)
./redact-ssn.py return.pdf --ocr --dump            # see what OCR reads, for diagnosis
```

Sometimes even OCR can't read the digits — tax forms often have you enter the
routing/account number one digit per *drawn box*, and OCR can't read digits
inside cells (and may even mangle the heading, e.g. `Routing` → `Routini`). For
this, on an OCR'd page, when a recognizable field heading (`Routing Number`,
`Account Number`, …) has no readable value, the tool falls back to a
**positional region**: it covers the area from the heading across full width and
down a fixed band, so the boxed digits are caught whether they sit to the right
of the heading or beneath it — even though it never read them. Heading matching
is fuzzy so OCR typos still count, the routing/account pairing still applies, and
the band is bounded by the next field heading (capped at `BAND_MAX_PT` points;
the one knob to turn if a field's boxes are taller).

OCR needs the `tesseract` engine, which the `nix-shell` shebang pulls in
automatically. Caveats: OCR is much slower than text extraction (seconds per
page); it can occasionally misread a digit; and the region fallback is
deliberately broad (it covers the full width beneath a heading). So on OCR'd
pages especially, eyeball the result and use `--dry-run -v` to review what was
found.

## What it matches

The 3-2-4 digit grouping with a dash **or** whitespace between groups:

- `123-45-6789` (dashed)
- `123 45 6789` (spaced)
- digits typed into three separate form boxes (`123` `45` `6789`)

A separator is always required. A bare 9-digit run (`123456789`) is *not*
redacted: with no separator it's ambiguous (account numbers, IDs, etc.) and
there's no safe way to tell it apart. Phone numbers (3-3-4) and other digit
shapes are left alone.

Matching is done per **visual line** (words are clustered by their vertical
position), which is what lets it rejoin SSNs split across separate form boxes.
A consequence: three numbers in adjacent table columns that happen to form a
3-2-4 shape can match. That's rare, and something shaped exactly like an SSN
is usually worth redacting anyway — but always sanity-check with `--dry-run -v`
before sharing a document.

## Bank account & routing numbers (`--bank`)

Account and routing numbers can't be matched by shape alone — their digits are
indistinguishable from any other number — so `--bank` only redacts a number
when it's **anchored to a label**: `account`, `acct`, `routing`, `rtn`, or
`aba` (with an optional trailing `no.` / `number` / `#`). For each label it
takes the first qualifying number found either to the **right** of the label or
in the **column directly beneath** it, since forms place the value in either
spot. Routing numbers must be exactly 9 digits; account numbers are accepted
between 4 and 17 digits.

To suppress false positives, account numbers are **paired with routing
numbers**: since an account number is meaningless without a routing number and
the two always appear together on a form (routing first), an `account` label is
only honored when an as-yet-unpaired routing number precedes it. This rejects
the word "account" in prose — e.g. *"questions about your account, call
800-448-2424"* — which has no routing number before it.

Because this leans on layout heuristics (which column, how far below), it's the
part most worth checking with `--dry-run -v` against your specific forms. The
window for "beneath the label" is intentionally tight (about two lines) to
avoid sweeping in an unrelated number from further down the page; if a value
on your form sits lower than that, the window is easy to widen.

## Ad-hoc values (`-r` / `--redact`)

For the one-off things the heuristics can't know about — a particular name, an
address, a confirmation number — pass `--redact VALUE`. The flag is repeatable,
and each value is redacted everywhere it appears, seeded into the same sweep that
propagates the auto-detected values:

```sh
./redact-ssn.py return.pdf -r "Jane Q. Public" -r 9988776655 -r "123 Main St"
```

Matching adapts to the value:

- **Numeric** (digits and separators only) is matched like a learned number —
  its digits in order, any separators ignored — so `9988776655` also catches
  `9988-776-655` or `99 88 77 66 55`.
- **Text** is matched case-insensitively and whitespace-flexibly, word-bounded on
  its edges so `Smith` doesn't catch `Smithsonian`.

Matches are reported under a `custom` count. Two caveats: a very short value
(e.g. `42`) will match broadly, and an ad-hoc value hidden in unreadable drawn
boxes on an OCR'd page won't be found (only the label-anchored bank fallback
covers those). As always, `--dry-run -v` shows exactly what was hit.

## Document-wide consistency

Redaction runs in two passes. The first applies the contextual rules above (an
SSN's 3-2-4 shape, a bank number's label) and **redacts each value directly**,
using the exact boxes the rule matched — so nothing the rules find can be
dropped, even in awkward layouts (e.g. one digit per box) — while recording its
digit-sequence. The second **sweeps** the whole document and redacts every
*other* occurrence of those sequences — ignoring separators — even where an
occurrence doesn't match any rule on its own.

So once a number is identified anywhere, it stays redacted everywhere:

- an account number shown with its label on one page is still redacted where it
  reappears unlabeled on another;
- an SSN learned from its dashed form is also redacted where it appears as a
  bare 9-digit run elsewhere (which, with no prior context, would be too
  ambiguous to catch on its own).

A practical caveat: the sweep matches on the digit-sequence alone, so a short
learned value (e.g. a 4-digit account number) could in principle collide with
an unrelated number elsewhere. `--dry-run -v` lists every hit so you can check.

## Tests

```sh
./tests/run-tests.py
```

Self-contained like the tool (the shebang pulls in `pymupdf` and `tesseract`).
It generates PDF fixtures in a temp dir at runtime — no PDFs are committed — and
covers each matching rule, the false-positive guards, the consistency sweep, and
the OCR paths (garble detection, recovering an image-only page, the typo-tolerant
labels, the region fallback, and flattening an OCR'd page).

## A note on trust

Redaction is only as good as the match. Eyeball the output of anything
high-stakes before sending it anywhere; `--dry-run -v` gives you an audit list
of every hit first.
