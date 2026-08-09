#!/usr/bin/env python3
"""Redact obvious PII from text-based statement fixtures before commit.

Usage:
    python scripts/redact_fixture.py <file>             # write redacted output to stdout
    python scripts/redact_fixture.py --inplace <file>   # overwrite the file

Patterns redacted (stdlib-only, no deps so any Python can run it):
    - 16-digit card numbers (with optional space/hyphen separators)
    - PAN  (5 letters + 4 digits + 1 letter)
    - Email addresses
    - Indian phone numbers with explicit +91 prefix

Out of scope (still need eyeball-before-commit per CLAUDE.md):
    - Names, addresses, free-text identifiers
    - Bank account numbers (overlap with transaction amounts)
    - Aadhaar (overlap with account-number-shaped strings)
    - Binary PDFs (extract text first, then redact)

The pytest guard at backend/tests/test_fixture_redaction.py fails CI if any
of the above patterns appears in a fixture, so this script is a convenience
producer and the test is the safety net.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Canonical source for PAN + card regexes is ``backend/app/core/pii_patterns.py``.
# This script keeps its own copies to preserve its stdlib-only invariant (no
# imports from ``backend/app/``). Those two patterns, and every replacement
# below, are pinned equal to the canonical ones by
# ``test_redact_script_pattern_parity`` — edit one side and CI fails, so this is
# a gate rather than a hand-sync request.
PII_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    (
        "card-number",
        re.compile(r"\b\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}\b"),
        "0000-0000-0000-0000",
    ),
    (
        "pan",
        re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"),
        "AAAAA0000A",
    ),
    (
        "email",
        re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
        "user@example.com",
    ),
    (
        "phone-in",
        re.compile(r"\+91[-\s]?\d{10}\b"),
        "+91-0000000000",
    ),
]


def redact(text: str) -> tuple[str, dict[str, int]]:
    counts: dict[str, int] = {}
    for name, pattern, replacement in PII_PATTERNS:
        text, n = pattern.subn(replacement, text)
        if n:
            counts[name] = n
    return text, counts


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("file", type=Path, help="text file to redact")
    parser.add_argument("--inplace", action="store_true", help="overwrite the file in place")
    args = parser.parse_args(argv)

    try:
        text = args.file.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        print(
            f"error: {args.file} is not UTF-8 text. Extract text from PDFs first (pdfplumber).",
            file=sys.stderr,
        )
        return 2

    redacted, counts = redact(text)

    if args.inplace:
        args.file.write_text(redacted, encoding="utf-8")
        print(f"redacted {args.file}: {counts or 'no changes'}", file=sys.stderr)
    else:
        sys.stdout.write(redacted)
        print(f"redactions: {counts or 'no changes'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
