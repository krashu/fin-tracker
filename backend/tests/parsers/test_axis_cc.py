"""Tests for :class:`AxisCC`.

* ``interpret_tables`` snapshot test against committed sentinel JSON tables
  exercises the row-interpretation logic in CI without binary PDFs.
* Negative tests cover the public exception surface.
* Real-PDF smoke test runs only when the local fixture + env var are
  present; on CI it skips cleanly.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

import pytest

from app.parsers import AxisCC, InvalidPasswordError, ParserError
from app.parsers.base import StatementSummary


def _serialize(rows: list[Any]) -> list[dict[str, Any]]:
    """Convert ``RawTransaction`` rows into the JSON-snapshot shape."""
    return [{**asdict(r), "date": r.date.isoformat()} for r in rows]


def test_interpret_tables_snapshot(
    axis_tables: list[list[list[str]]],
    axis_tables_expected: list[dict[str, Any]],
) -> None:
    rows = AxisCC.interpret_tables(axis_tables)
    assert _serialize(rows) == axis_tables_expected


def test_interpret_tables_flipkart_snapshot(
    axis_flipkart_tables: list[list[list[str]]],
    axis_flipkart_tables_expected: list[dict[str, Any]],
) -> None:
    """Flipkart co-branded 5-column layout: the MERCHANT CATEGORY column and the
    per-row CASHBACK EARNED value must be stripped from merchant_raw, the AMOUNT
    column's Dr/Cr must still drive the sign, and every row must survive."""
    rows = AxisCC.interpret_tables(axis_flipkart_tables)
    assert _serialize(rows) == axis_flipkart_tables_expected


def test_interpret_tables_flipkart_detected_without_header() -> None:
    """Structural fallback: a Flipkart row is recognised by its two amount
    columns (AMOUNT + CASHBACK EARNED) even when the header row didn't extract
    with the tell-tale labels, and the MERCHANT CATEGORY column is still dropped."""
    tables = [
        [
            ["24/04/2026", "SENTINEL RETAIL,BLR", "DEPT STORES", "792.00 Dr", "59.00 Cr"],
            ["", "Total Outstanding", "", "792.00", ""],
        ]
    ]
    rows = AxisCC.interpret_tables(tables)
    assert _serialize(rows) == [
        {
            "date": "2026-04-24",
            "amount_paise": -79200,
            "merchant_raw": "SENTINEL RETAIL,BLR",
            "txn_type": "purchase",
        }
    ]


def test_interpret_tables_empty_input_raises() -> None:
    with pytest.raises(ParserError, match="no transactions found"):
        AxisCC.interpret_tables([])


def test_interpret_tables_only_headers_raises() -> None:
    """Tables with only headers / summaries (no parseable date) must raise."""
    tables = [
        [
            ["Date", "Description", "Amount"],
            ["", "Total Outstanding", "1,234"],
            ["Available Credit", "", "84,808.72"],
        ]
    ]
    with pytest.raises(ParserError, match="no transactions found"):
        AxisCC.interpret_tables(tables)


def test_parse_corrupt_bytes_raises() -> None:
    """Random bytes are not a parseable PDF — must surface as ParserError."""
    with pytest.raises(ParserError):
        AxisCC.parse(b"this is definitely not a pdf", password=None)


def test_parse_empty_bytes_raises() -> None:
    with pytest.raises(ParserError):
        AxisCC.parse(b"", password=None)


def test_parse_wrong_password_raises(encrypted_pdf_bytes: bytes) -> None:
    """Wrong password on a user-password-protected PDF must raise InvalidPasswordError.

    Synthetic PDF (built in conftest) so this branch is always covered in CI,
    regardless of how the user's real bank PDFs happen to be encrypted.
    """
    with pytest.raises(InvalidPasswordError):
        AxisCC.parse(encrypted_pdf_bytes, password="definitely-not-the-right-password-xyz")


def test_parse_real_pdf_smoke(axis_real_pdf: bytes, axis_real_password: str) -> None:
    """End-to-end: decrypt + extract + interpret. Lightweight sanity, not a snapshot."""
    parsed = AxisCC.parse(axis_real_pdf, password=axis_real_password)
    rows = parsed.rows
    assert rows, "real Axis PDF returned zero rows — parser layout assumptions wrong?"
    first = rows[0]
    assert first.merchant_raw.strip(), "first row has empty merchant"
    assert first.date.year >= 2020, f"implausible date on first row: {first.date}"
    # Balance-reconciliation plan Phase 0 spike confirmed "Total Amount Due" /
    # "Total Payment Due" always prints on a real Axis statement; opening
    # balance and period are not asserted here — Phase 0 found the opening
    # figure sits inside a formula-legend line on the real layout with no
    # clean trailing value, so StatementSummary legitimately reports it None.
    assert parsed.summary.closing_balance_paise is not None, (
        "real Axis PDF returned no closing balance — label wording changed?"
    )


def test_interpret_summary_snapshot(
    axis_summary_lines: list[str],
    axis_summary_expected: dict[str, Any],
) -> None:
    summary = AxisCC.interpret_summary(axis_summary_lines)
    assert summary.period_start is not None
    assert summary.period_end is not None
    actual = {
        **asdict(summary),
        "period_start": summary.period_start.isoformat(),
        "period_end": summary.period_end.isoformat(),
    }
    assert actual == axis_summary_expected


def test_interpret_summary_cc_sign_credit_marker_is_positive() -> None:
    """CC sign convention (negative = owed): a bare "Total Amount Due" is a
    debit → negative (pinned by the snapshot fixture above). A CR-suffixed
    closing balance (the card is in credit / overpaid) must flip positive —
    the one branch the snapshot fixture, which carries no CR, can't reach."""
    lines = ["Total Amount Due          500.00 CR"]

    summary = AxisCC.interpret_summary(lines)

    assert summary.closing_balance_paise == 50000


def test_interpret_summary_no_labels_returns_all_none() -> None:
    """The Flipkart layout's real closing text — no summary block at all.
    Must degrade to an all-None StatementSummary, never a ParserError."""
    lines = ["**** End of Statement ****"]

    summary = AxisCC.interpret_summary(lines)

    assert summary == StatementSummary()
