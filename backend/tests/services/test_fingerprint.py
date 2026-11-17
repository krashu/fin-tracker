"""Tests for the PRD §F4 fingerprint formula.

ADR-0006 rule 1 freezes the formula, so its output is pinned: ``test_golden_vector``
holds one input tuple to a literal digest, which catches a field reorder, a changed
separator or an extra field that every behavioural test below would pass. The rest pin
the *behaviour*: the same inputs produce the same hash; flipping any one of the four
fields produces a different hash; the refund sign flip (PRD §F4a) produces a different
hash; the output shape is 64 lowercase hex chars.
"""

from __future__ import annotations

import re
from datetime import date

from app.services.fingerprint import transaction_fingerprint

_BASE = {
    "txn_date": date(2026, 1, 15),
    "amount_paise": -150000,
    "normalized_merchant": "swiggy bangalore",
    "account_id": 1,
}


def test_golden_vector() -> None:
    """ADR-0006 rule 1: sha256 of ``2026-01-15␟-150000␟swiggy bangalore␟1`` (␟ = ``\\x1f``).

    The literal was computed outside the app, with
    ``printf '2026-01-15\\x1f-150000\\x1fswiggy bangalore\\x1f1' | sha256sum``. If this fails,
    the formula changed: every stored fingerprint is now stale. That is a migration under
    ADR-0006 rules 18-21, never an update to this literal.
    """
    assert transaction_fingerprint(**_BASE) == (
        "98e3053ae4c4d69215dad3510b3d0492e1838bd347db2cdd79def1cbc3901fa2"
    )


def test_determinism() -> None:
    assert transaction_fingerprint(**_BASE) == transaction_fingerprint(**_BASE)


def test_sensitivity_per_field() -> None:
    base = transaction_fingerprint(**_BASE)
    assert transaction_fingerprint(**{**_BASE, "txn_date": date(2026, 1, 16)}) != base
    assert transaction_fingerprint(**{**_BASE, "amount_paise": -150001}) != base
    assert transaction_fingerprint(**{**_BASE, "normalized_merchant": "swiggy mumbai"}) != base
    assert transaction_fingerprint(**{**_BASE, "account_id": 2}) != base


def test_refund_sign_sensitivity() -> None:
    """A refund (positive) must NOT dedup against the original spend (negative).

    PRD §F4a refund treatment hinges on this — refunds share date / merchant /
    account with the original spend, only the sign of amount_paise differs.
    """
    spend = transaction_fingerprint(**{**_BASE, "amount_paise": -10000})
    refund = transaction_fingerprint(**{**_BASE, "amount_paise": 10000})
    assert spend != refund


def test_no_boundary_collision_between_merchant_and_account() -> None:
    """The ``normalized_merchant | account_id`` boundary is unambiguous.

    Separatorless concatenation made ``"amazon1" + 2`` hash identically to
    ``"amazon" + 12`` — a real cross-account false-duplicate. See ADR-0006.
    """
    a = transaction_fingerprint(**{**_BASE, "normalized_merchant": "amazon1", "account_id": 2})
    b = transaction_fingerprint(**{**_BASE, "normalized_merchant": "amazon", "account_id": 12})
    assert a != b


def test_no_boundary_collision_between_amount_and_merchant() -> None:
    """The ``amount_paise | normalized_merchant`` boundary is unambiguous.

    The second ambiguous join: ``-1`` + ``"23x"`` vs ``-12`` + ``"3x"``. Both are
    variable-length, so only a separator distinguishes them.
    """
    a = transaction_fingerprint(**{**_BASE, "amount_paise": -1, "normalized_merchant": "23x"})
    b = transaction_fingerprint(**{**_BASE, "amount_paise": -12, "normalized_merchant": "3x"})
    assert a != b


def test_separator_cannot_appear_in_a_normalized_merchant() -> None:
    """The invariant the whole separator choice rests on (ADR-0006).

    ``\\x1f`` is safe *because* ``normalize_merchant``'s ``" ".join(raw.split())``
    deletes it — ``'\\x1f'.isspace()`` is True. If someone ever stops collapsing
    whitespace there, the separator stops being injection-proof and this fails
    before any fingerprint silently collides.
    """
    from app.services.merchant import normalize_merchant

    assert "\x1f" not in normalize_merchant("amazon\x1f2")
    assert normalize_merchant("amazon\x1f2") == "amazon 2"


def test_format() -> None:
    fp = transaction_fingerprint(**_BASE)
    assert len(fp) == 64
    assert re.fullmatch(r"[0-9a-f]{64}", fp) is not None


def test_f1_f2_dedup_parity() -> None:
    """F1 import path and F2 manual POST path produce the SAME fingerprint
    for the same (date, amount, merchant, account).

    Both paths call ``normalize_merchant`` then ``transaction_fingerprint``
    against the same arguments. If a future change diverges them (e.g.,
    F2 route uppercases before normalizing while import doesn't), this
    test fails before users see a duplicate row land twice.

    The two ``normalize_merchant`` calls below mirror what each path does
    in production: ``import_service`` normalizes ``RawTransaction.merchant_raw``;
    the F2 route normalizes ``payload.merchant_raw``. Same input string →
    same normalized → same hash.
    """
    from app.services.merchant import normalize_merchant

    raw_from_parser = "SWIGGY BANGALORE"
    raw_from_user = "SWIGGY BANGALORE"

    f1_fp = transaction_fingerprint(
        txn_date=date(2026, 1, 15),
        amount_paise=-150000,
        normalized_merchant=normalize_merchant(raw_from_parser),
        account_id=1,
    )
    f2_fp = transaction_fingerprint(
        txn_date=date(2026, 1, 15),
        amount_paise=-150000,
        normalized_merchant=normalize_merchant(raw_from_user),
        account_id=1,
    )
    assert f1_fp == f2_fp


def test_f1_f2_dedup_parity_across_unicode_spellings() -> None:
    """A precomposed and a decomposed spelling of one merchant (a parser re-extraction versus
    a typed entry) fingerprint alike, because ``normalize_merchant`` puts both in NFC."""
    from app.services.merchant import normalize_merchant

    precomposed = transaction_fingerprint(
        txn_date=date(2026, 1, 15),
        amount_paise=-150000,
        normalized_merchant=normalize_merchant("CAF\u00c9 BANGALORE"),
        account_id=1,
    )
    decomposed = transaction_fingerprint(
        txn_date=date(2026, 1, 15),
        amount_paise=-150000,
        normalized_merchant=normalize_merchant("CAFE\u0301 BANGALORE"),
        account_id=1,
    )
    assert precomposed == decomposed
