"""Invariant tests for :class:`RawTransaction`, plus the shared column detective.

The sign convention (purchase ≤ 0, payment/refund ≥ 0, other unconstrained)
is enforced at construction so a parser bug that flips a sign blows up at
the offending row rather than poisoning downstream reports silently.

``_interpret_row`` is the layout detective every statement parser routes through, so a
mis-picked column is a wrong number on a money path for every issuer at once.
"""

from __future__ import annotations

import re
from datetime import date

import pytest

from app.parsers import RawTransaction
from app.parsers.base import (
    ParserError,
    _extract_text,
    _find_labelled_amount,
    _find_period,
    _interpret_row,
)
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


# --- balance-reconciliation plan Phase 2: _extract_text / _find_labelled_amount / _find_period ---


def test_extract_text_raises_on_corrupt_bytes() -> None:
    with pytest.raises(ParserError, match="failed to extract text"):
        _extract_text(b"this is definitely not a pdf")


def test_extract_text_returns_a_list_of_lines_for_a_textless_pdf() -> None:
    """Mirrors _extract_tables's own contract: a well-formed PDF with nothing
    on the page returns an empty list, never raises and never None."""
    import io

    import pikepdf

    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(612, 792))
    buf = io.BytesIO()
    pdf.save(buf)

    assert _extract_text(buf.getvalue()) == []


def test_find_labelled_amount_finds_the_trailing_value() -> None:
    lines = ["Statement Period: 01/03/2026 to 31/03/2026", "Previous Balance          5,699.00"]
    pattern = re.compile(r"Previous\s+Balance", re.IGNORECASE)

    assert _find_labelled_amount(lines, pattern) == (569900, False)


def test_find_labelled_amount_handles_a_credit_marker_split_by_whitespace() -> None:
    """ "1,234.56 CR" tokenises as two words — the two-word attempt must run
    before the one-word attempt, or the marker gets silently dropped."""
    lines = ["Total Amount Due          1,234.56 CR"]
    pattern = re.compile(r"Total\s+Amount\s+Due", re.IGNORECASE)

    assert _find_labelled_amount(lines, pattern) == (123456, True)


def test_find_labelled_amount_skips_an_unparseable_match_for_a_later_line() -> None:
    lines = ["Previous Balance carried forward", "Previous Balance          100.00"]
    pattern = re.compile(r"Previous\s+Balance", re.IGNORECASE)

    assert _find_labelled_amount(lines, pattern) == (10000, False)


def test_find_labelled_amount_returns_none_when_the_pattern_is_absent() -> None:
    lines = ["Total Amount Due          1,234.56"]
    pattern = re.compile(r"Statement\s+Period", re.IGNORECASE)

    assert _find_labelled_amount(lines, pattern) is None


def test_find_period_extracts_the_first_two_dates_on_the_matched_line() -> None:
    lines = ["Statement Period: 01/03/2026 to 31/03/2026"]
    pattern = re.compile(r"Statement\s+Period", re.IGNORECASE)

    result = _find_period(lines, pattern, ("%d/%m/%Y",))

    assert result == (date(2026, 3, 1), date(2026, 3, 31))


def test_find_period_returns_none_when_the_matched_line_has_only_one_date() -> None:
    lines = ["Payment Due Date 25/03/2026"]
    pattern = re.compile(r"Payment\s+Due\s+Date", re.IGNORECASE)

    result = _find_period(lines, pattern, ("%d/%m/%Y",))

    assert result is None
