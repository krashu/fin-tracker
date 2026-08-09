"""Tests for :class:`IciciCC`.

Same shape as the Axis tests — snapshot against committed sentinel JSON
tables, negative cases for the exception surface, real-PDF smoke that
skips on absence.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

import pytest

from app.parsers import IciciCC, InvalidPasswordError, ParserError


def _serialize(rows: list[Any]) -> list[dict[str, Any]]:
    return [{**asdict(r), "date": r.date.isoformat()} for r in rows]


def test_interpret_tables_snapshot(
    icici_tables: list[list[list[str]]],
    icici_tables_expected: list[dict[str, Any]],
) -> None:
    rows = IciciCC.interpret_tables(icici_tables)
    assert _serialize(rows) == icici_tables_expected


def test_interpret_tables_empty_input_raises() -> None:
    with pytest.raises(ParserError, match="no transactions found"):
        IciciCC.interpret_tables([])


def test_interpret_tables_only_headers_raises() -> None:
    tables = [
        [
            ["Trans Date", "Posting Date", "Description", "Amount"],
            ["", "", "Total Amount Due", "1,234"],
        ]
    ]
    with pytest.raises(ParserError, match="no transactions found"):
        IciciCC.interpret_tables(tables)


def test_parse_corrupt_bytes_raises() -> None:
    with pytest.raises(ParserError):
        IciciCC.parse(b"not a pdf at all", password=None)


def test_parse_empty_bytes_raises() -> None:
    with pytest.raises(ParserError):
        IciciCC.parse(b"", password=None)


def test_parse_wrong_password_raises(encrypted_pdf_bytes: bytes) -> None:
    """Synthetic encrypted PDF; same rationale as the Axis equivalent."""
    with pytest.raises(InvalidPasswordError):
        IciciCC.parse(encrypted_pdf_bytes, password="definitely-not-the-right-password-xyz")


def test_parse_real_pdf_smoke(icici_real_pdf: bytes, icici_real_password: str) -> None:
    rows = IciciCC.parse(icici_real_pdf, password=icici_real_password)
    assert rows, "real ICICI PDF returned zero rows — parser layout assumptions wrong?"
    first = rows[0]
    assert first.merchant_raw.strip()
    assert first.date.year >= 2020, f"implausible date on first row: {first.date}"
