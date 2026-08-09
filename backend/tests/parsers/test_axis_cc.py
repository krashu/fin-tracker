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
    rows = AxisCC.parse(axis_real_pdf, password=axis_real_password)
    assert rows, "real Axis PDF returned zero rows — parser layout assumptions wrong?"
    first = rows[0]
    assert first.merchant_raw.strip(), "first row has empty merchant"
    assert first.date.year >= 2020, f"implausible date on first row: {first.date}"
