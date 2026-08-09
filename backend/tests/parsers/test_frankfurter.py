"""Parser tests for the frankfurter.app FX-rate parser (PRD §F7 FX layer).

Pure-parser coverage: the single-observation and date-range response shapes, exact-Decimal
rate parsing (no float round-trip), per-row skip warnings (malformed / non-numeric /
non-positive / unparseable date / missing rate), and the file-level failures that raise
``FrankfurterParseError`` (non-JSON, non-object, no rates, zero usable rows).
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from app.parsers.frankfurter import FrankfurterParseError, parse_frankfurter_rates


def _single(rate_date: str, inr: object) -> bytes:
    payload = {"amount": 1.0, "base": "USD", "date": rate_date, "rates": {"INR": inr}}
    return json.dumps(payload).encode("utf-8")


def _range(rates: dict[str, object]) -> bytes:
    payload = {"amount": 1.0, "base": "USD", "start_date": "x", "rates": rates}
    return json.dumps(payload).encode("utf-8")


def test_single_observation() -> None:
    rows, warnings = parse_frankfurter_rates(_single("2026-06-23", 83.5))

    assert warnings == []
    assert len(rows) == 1
    assert rows[0].rate_date == date(2026, 6, 23)
    # Exact Decimal — no float round-trip (json parse_float=Decimal).
    assert rows[0].rate == Decimal("83.5")


def test_range_response() -> None:
    rows, warnings = parse_frankfurter_rates(
        _range({"2026-06-23": {"INR": 83.5123}, "2026-06-24": {"INR": 83.6}})
    )

    assert warnings == []
    by_date = {r.rate_date: r.rate for r in rows}
    assert by_date == {date(2026, 6, 23): Decimal("83.5123"), date(2026, 6, 24): Decimal("83.6")}


def test_non_positive_rate_skipped() -> None:
    rows, warnings = parse_frankfurter_rates(
        _range({"2026-06-23": {"INR": 83.5}, "2026-06-24": {"INR": 0}})
    )

    assert [r.rate_date for r in rows] == [date(2026, 6, 23)]
    assert any("non-positive rate" in w for w in warnings)


def test_non_numeric_rate_skipped() -> None:
    rows, warnings = parse_frankfurter_rates(
        _range({"2026-06-23": {"INR": 83.5}, "2026-06-24": {"INR": "oops"}})
    )

    assert [r.rate_date for r in rows] == [date(2026, 6, 23)]
    assert any("non-numeric rate" in w for w in warnings)


def test_unparseable_date_skipped() -> None:
    rows, warnings = parse_frankfurter_rates(
        _range({"2026-06-23": {"INR": 83.5}, "not-a-date": {"INR": 83.6}})
    )

    assert [r.rate_date for r in rows] == [date(2026, 6, 23)]
    assert any("unparseable date" in w for w in warnings)


def test_missing_rate_key_skipped() -> None:
    # A range day whose dict lacks the requested currency.
    rows, warnings = parse_frankfurter_rates(
        _range({"2026-06-23": {"INR": 83.5}, "2026-06-24": {"EUR": 90.0}})
    )

    assert [r.rate_date for r in rows] == [date(2026, 6, 23)]
    assert any("missing rate" in w for w in warnings)


def test_malformed_range_entry_skipped() -> None:
    rows, warnings = parse_frankfurter_rates(
        _range({"2026-06-23": {"INR": 83.5}, "2026-06-24": "oops"})
    )

    assert [r.rate_date for r in rows] == [date(2026, 6, 23)]
    assert any("malformed rate entry" in w for w in warnings)


def test_not_json_raises() -> None:
    with pytest.raises(FrankfurterParseError, match="not valid JSON"):
        parse_frankfurter_rates(b"<html>nope</html>")


def test_not_object_raises() -> None:
    with pytest.raises(FrankfurterParseError, match="not a JSON object"):
        parse_frankfurter_rates(b"[1, 2, 3]")


def test_no_rates_raises() -> None:
    with pytest.raises(FrankfurterParseError, match="no rates"):
        parse_frankfurter_rates(json.dumps({"amount": 1.0, "base": "USD"}).encode("utf-8"))


def test_zero_usable_rows_raises() -> None:
    # A single response whose only rate is non-positive yields no rows.
    with pytest.raises(FrankfurterParseError, match="no usable rates"):
        parse_frankfurter_rates(_single("2026-06-23", -1))
