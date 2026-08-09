"""Invariant tests for :class:`RawTransaction`, plus the shared column detective.

The sign convention (purchase ≤ 0, payment/refund ≥ 0, other unconstrained)
is enforced at construction so a parser bug that flips a sign blows up at
the offending row rather than poisoning downstream reports silently.

``_interpret_row`` is the layout detective every statement parser routes through, so a
mis-picked column is a wrong number on a money path for every issuer at once.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.parsers import RawTransaction
from app.parsers.base import _interpret_row
from app.parsers.icici_cc import IciciCC, _classify


def _tx(amount_paise: int, txn_type: str) -> RawTransaction:
    # a strict checker won't accept str → TxnType at the test layer; the tests
    # exist to prove the runtime invariant so str is fine here.
    return RawTransaction(
        date=date(2026, 3, 15),
        amount_paise=amount_paise,
        merchant_raw="SENTINEL",
        txn_type=txn_type,  # type: ignore[arg-type]
    )


def test_purchase_negative_paise_ok() -> None:
    assert _tx(-100, "purchase").amount_paise == -100


def test_purchase_zero_paise_ok() -> None:
    assert _tx(0, "purchase").amount_paise == 0


def test_purchase_positive_paise_raises() -> None:
    with pytest.raises(ValueError, match=r"purchase must have amount_paise <= 0"):
        _tx(100, "purchase")


def test_payment_positive_paise_ok() -> None:
    assert _tx(100, "payment").amount_paise == 100


def test_payment_zero_paise_ok() -> None:
    assert _tx(0, "payment").amount_paise == 0


def test_payment_negative_paise_raises() -> None:
    with pytest.raises(ValueError, match=r"payment must have amount_paise >= 0"):
        _tx(-100, "payment")


def test_refund_positive_paise_ok() -> None:
    assert _tx(100, "refund").amount_paise == 100


def test_refund_negative_paise_raises() -> None:
    with pytest.raises(ValueError, match=r"refund must have amount_paise >= 0"):
        _tx(-100, "refund")


def test_other_accepts_negative() -> None:
    assert _tx(-100, "other").amount_paise == -100


def test_other_accepts_positive() -> None:
    assert _tx(100, "other").amount_paise == 100


def test_leading_serial_column_is_not_read_as_the_amount() -> None:
    """B#18: the amount scan started at index 0, skipping only the date cell, so a leading
    serial-number column won it — ``_AMOUNT_RE`` matches a bare ``1``.

    Every row of such a statement imported as a 1-rupee purchase with the real amount joined
    into ``merchant_raw``, which poisons the F4 fingerprint, the F3 tag-map key and every
    spend total at once. Driven through the ICICI formats/classifier because that is the
    issuer whose statements carry the serial column.
    """
    txn = _interpret_row(
        ["1", "10/04/2026", "11/04/2026", "SENTINEL GROCERY MUM", "1,234.50"],
        IciciCC.DATE_FORMATS,
        _classify,
    )

    assert txn is not None
    assert txn.amount_paise == -123450  # debit, no CR marker → purchase, negative
    assert txn.date == date(2026, 4, 10)
    assert "1,234.50" not in txn.merchant_raw
    assert txn.merchant_raw == "1 SENTINEL GROCERY MUM"


def test_amount_after_date_is_still_found_without_a_serial_column() -> None:
    """The ordinary 4-column ICICI shape, pinned alongside the fix so the narrowed scan
    cannot pass by refusing to find an amount at all."""
    txn = _interpret_row(
        ["10/04/2026", "11/04/2026", "SENTINEL GROCERY MUM", "1,234.50"],
        IciciCC.DATE_FORMATS,
        _classify,
    )

    assert txn is not None
    assert txn.amount_paise == -123450
    assert txn.merchant_raw == "SENTINEL GROCERY MUM"
