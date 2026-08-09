"""Tests for the AMFI NAVAll parser (PRD §F7 NAV snapshot).

* Snapshot test parses a committed trimmed NAVAll excerpt (header + category/AMC
  headers + blank lines + scheme rows) against frozen expected JSON — exercising the
  data-row filter, both ISIN columns, and date/NAV parsing in one shot.
* Unit tests cover header/AMC/blank skipping, the reinvest column, the N.A.-NAV and
  bad-date skip-with-warning paths, bad encoding, and the empty-body guard.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from app.parsers import AmfiParseError, parse_navall

FIXTURES = Path(__file__).parent.parent / "fixtures" / "amfi_navall"

_HEADER = (
    "Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;"
    "Scheme Name;Net Asset Value;Date"
)


def _serialize(rows: list[Any]) -> list[dict[str, Any]]:
    """Render AmfiNavRow rows into the JSON-snapshot shape (Decimal/ date as str)."""
    out: list[dict[str, Any]] = []
    for r in rows:
        d = asdict(r)
        d["nav"] = str(r.nav)
        d["nav_date"] = r.nav_date.isoformat()
        out.append(d)
    return out


def _navall(*data_rows: str) -> bytes:
    return ("\n".join([_HEADER, *data_rows]) + "\n").encode("utf-8")


def test_navall_snapshot() -> None:
    raw = (FIXTURES / "navall_sample.txt").read_bytes()
    expected = json.loads((FIXTURES / "navall_sample.expected.json").read_text("utf-8"))
    rows, warnings = parse_navall(raw)
    assert warnings == []
    assert _serialize(rows) == expected


def test_skips_header_category_amc_and_blank_lines() -> None:
    raw = (
        f"{_HEADER}\n"
        "\n"
        "Open Ended Schemes(Equity Scheme - Index Funds)\n"
        "\n"
        "Some AMC Mutual Fund\n"
        "\n"
        "120503;INF179KC1979;-;HDFC Index Fund;245.6789;20-Jun-2026\n"
    ).encode()
    rows, warnings = parse_navall(raw)
    assert warnings == []
    assert [r.scheme_code for r in rows] == ["120503"]
    assert rows[0].isin_growth == "INF179KC1979"
    assert rows[0].isin_reinvest is None  # '-' → None


def test_both_isin_columns_captured() -> None:
    rows, _ = parse_navall(
        _navall("119551;INF209KA12Z1;INF209KA13Z9;ABSL Fund;105.9219;20-Jun-2026")
    )
    assert rows[0].isin_growth == "INF209KA12Z1"
    assert rows[0].isin_reinvest == "INF209KA13Z9"


def test_non_numeric_nav_skipped_with_warning() -> None:
    rows, warnings = parse_navall(
        _navall(
            "120503;INF179KC1979;-;HDFC Index Fund;245.6789;20-Jun-2026",
            "999999;INF000000001;-;No NAV Fund;N.A.;20-Jun-2026",
        )
    )
    assert [r.scheme_code for r in rows] == ["120503"]
    assert warnings == ["scheme 999999: non-numeric NAV"]


def test_unparseable_date_skipped_with_warning() -> None:
    rows, warnings = parse_navall(
        _navall(
            "120503;INF179KC1979;-;HDFC Index Fund;245.6789;20-Jun-2026",
            "888888;INF000000002;-;Bad Date Fund;100.0;2026-06-20",  # ISO, not DD-Mon-YYYY
        )
    )
    assert [r.scheme_code for r in rows] == ["120503"]
    assert warnings == ["scheme 888888: unparseable date"]


def test_bad_encoding_raises() -> None:
    with pytest.raises(AmfiParseError, match="UTF-8"):
        parse_navall(b"\xff\xfe;not;utf8;bytes;here;now")


def test_empty_body_raises() -> None:
    with pytest.raises(AmfiParseError, match="no NAV rows"):
        parse_navall(f"{_HEADER}\n\nSome AMC Mutual Fund\n".encode())
