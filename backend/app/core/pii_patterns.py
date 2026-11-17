"""Canonical PII regex patterns shared across the backend.

Source-of-truth for :func:`app.core.log_config.mask_pii` (structured-log
scrubbing) and :mod:`tests.test_fixture_redaction` (fixture-commit
guard).

A third caller, ``scripts/redact_fixture.py``, holds its own copies of
these patterns to preserve its stdlib-only invariant; that script's
``PII_PATTERNS`` block points back here as the canonical source, and
``tests.test_fixture_redaction.test_redact_script_pattern_parity``
asserts the two agree — so the copies cannot drift silently.

Scope notes:

* PAN is uppercase by Indian govt spec; the regex is intentionally
  case-sensitive. Lowercase variants are out of scope (matches the
  fixture-redaction script's stance).
* 13-digit (older Visa) and 15-digit (Amex) card numbers are NOT
  covered. Same stance as the canonical fixture-redaction script. To
  widen, edit BOTH this module and ``scripts/redact_fixture.py``'s
  ``PII_PATTERNS`` — the card + PAN halves are pinned equal by
  ``tests/test_fixture_redaction.test_redact_script_pattern_parity``,
  so a one-sided edit fails CI rather than drifting silently.
* 12-digit Aadhaar numbers (first digit 2-9, optional space / hyphen
  groups of four) are masked in logs only (ADR-0003 rule 17). They are
  NOT part of the fixture-redaction guard or ``scripts/redact_fixture.py``;
  :func:`app.core.log_config.mask_pii` applies them after the card rule, so
  a 16-digit card is masked whole before the 12-digit pattern sees it.
"""

from __future__ import annotations

import re

PAN_RE = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
CARD_RE = re.compile(r"\b\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}\b")
AADHAAR_RE = re.compile(r"\b[2-9]\d{3}[-\s]?\d{4}[-\s]?\d{4}\b")
