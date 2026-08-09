"""Tests for :func:`app.services.merchant.normalize_merchant`.

v1 contract: lowercase + whitespace collapse, idempotent. F3's later
extension will add regex stripping; that extension will get its own tests
and an ADR before it lands (PRD §F3 + fingerprint-stability concerns).
"""

from __future__ import annotations

from app.services.merchant import normalize_merchant


def test_lowercase_and_whitespace_collapse() -> None:
    assert normalize_merchant("  Swiggy   BLR ") == "swiggy blr"


def test_idempotent() -> None:
    once = normalize_merchant("AMAZON   IN  ")
    assert normalize_merchant(once) == once


def test_empty() -> None:
    assert normalize_merchant("") == ""
