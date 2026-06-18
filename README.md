# redact-ssn

A small, self-contained CLI that redacts U.S. Social Security Numbers from a
PDF — removing the underlying text, not just drawing a box over it, so the
digits can't be recovered by copy/paste or text extraction.

## Usage

The script is self-contained: the `nix-shell` shebang pulls in its only
dependency (`pymupdf`) on first run, so there's nothing to install.

```sh
./redact-ssn.py return.pdf                 # -> return.redacted.pdf (won't clobber the original)
./redact-ssn.py return.pdf -o clean.pdf    # explicit output path
./redact-ssn.py return.pdf --in-place      # overwrite the original (written via a temp file first)
./redact-ssn.py return.pdf --dry-run -v    # list what would be redacted, write nothing
./redact-ssn.py return.pdf --force         # overwrite an existing output file
```

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

## A note on trust

Redaction is only as good as the match. Eyeball the output of anything
high-stakes before sending it anywhere; `--dry-run -v` gives you an audit list
of every hit first.
