"""Tests for :func:`app.services.merchant.normalize_merchant`.

v1 contract: lowercase, Unicode NFC, whitespace collapse, idempotent. F3's later
extension will add regex stripping; that extension will get its own tests
and an ADR before it lands (PRD §F3 + fingerprint-stability concerns).
"""

from __future__ import annotations

import pytest

from app.services.merchant import normalize_merchant


def test_lowercase_and_whitespace_collapse() -> None:
    assert normalize_merchant("  Swiggy   BLR ") == "swiggy blr"


def test_idempotent() -> None:
    once = normalize_merchant("AMAZON   IN  ")
    assert normalize_merchant(once) == once


def test_empty() -> None:
    assert normalize_merchant("") == ""


def test_canonically_equivalent_spellings_normalize_alike() -> None:
    assert (
        normalize_merchant("CAFE\u0301 BLR")
        == normalize_merchant("CAF\u00c9 BLR")
        == "caf\u00e9 blr"
    )
    assert normalize_merchant("\u0958") == normalize_merchant("\u0915\u093c") == "\u0915\u093c"


def test_nfc_runs_after_lowercasing() -> None:
    # "J" + combining caron is NFC already; lower-casing it leaves "j" + caron, which only
    # composes to U+01F0 if NFC runs after lower().
    assert normalize_merchant("J\u030c") == "\u01f0"


@pytest.mark.parametrize(
    "text", ["swiggy blr", "caf\u00e9", "\u0938\u094d\u0935\u093f\u0917\u0940"]
)
def test_already_nfc_text_is_unchanged(text: str) -> None:
    assert normalize_merchant(text) == text
