"""Tests for the canonical investment CSV parser (PRD §F7).

* Snapshot test parses a committed **raw Zerodha tradebook** (native headers, no
  renaming) against frozen expected JSON — exercising header-alias resolution, the
  ignored unknown columns (segment/series/auction/trade_id/...), the captured ``isin``
  column, and amount derivation in one shot.
* Unit tests cover the alias config invariant, structural failures (missing/duplicate
  header, bad encoding, no rows), the type vocabulary (accept buy/sip/sell/dividend/
  bonus; reject split/switch/unknown), the currency handling (INR default, USD accepted,
  other currencies + US-class/INR mismatch skipped per-row), amount authoritative-vs-derived,
  decimal precision, and both date formats.

Per-type *magnitude* validation (buy needs a price, etc.) is the import service's
boundary — see ``tests/services/test_investment_import_service.py``.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from app.parsers import CSVParseError, parse_investment_csv
from app.parsers.investment_csv import HEADER_ALIASES

FIXTURES = Path(__file__).parent.parent / "fixtures" / "investment_csv"


def _serialize(rows: list[Any]) -> list[dict[str, Any]]:
    """Render ParsedInvestmentRow rows into the JSON-snapshot shape (decimals as str)."""
    out: list[dict[str, Any]] = []
    for r in rows:
        d = asdict(r)
        d["date"] = r.date.isoformat()
        d["units"] = str(r.units)
        d["price"] = None if r.price is None else str(r.price)
        out.append(d)
    return out


def _csv(*rows: str, header: str = "date,type,symbol,units,price,amount") -> bytes:
    return ("\n".join([header, *rows]) + "\n").encode("utf-8")


def test_zerodha_tradebook_snapshot() -> None:
    raw = (FIXTURES / "zerodha_tradebook.csv").read_bytes()
    expected = json.loads((FIXTURES / "zerodha_tradebook.expected.json").read_text("utf-8"))
    rows, warnings = parse_investment_csv(raw, default_asset_class="indian_equity")
    assert warnings == []
    assert _serialize(rows) == expected


def test_header_aliases_are_disjoint() -> None:
    """No source spelling may resolve to two canonical fields (config invariant)."""
    seen: dict[str, str] = {}
    for field, spellings in HEADER_ALIASES.items():
        for spelling in spellings:
            assert spelling == spelling.lower(), f"{spelling!r} must be lowercase"
            assert spelling not in seen, f"{spelling!r} maps to both {seen[spelling]} and {field}"
            seen[spelling] = field


def test_missing_required_column_raises() -> None:
    csv = b"date,symbol,units,price\n2024-01-01,INFY,10,100\n"  # no 'type'
    with pytest.raises(CSVParseError, match="missing required column"):
        parse_investment_csv(csv, default_asset_class="indian_equity")


def test_duplicate_required_column_raises() -> None:
    csv = _csv("2024-01-01,buy,INFY,10,100,1000", header="date,type,symbol,units,quantity,price")
    with pytest.raises(CSVParseError, match="duplicate column"):
        parse_investment_csv(csv, default_asset_class="indian_equity")


def test_bad_encoding_raises() -> None:
    with pytest.raises(CSVParseError, match="UTF-8"):
        parse_investment_csv(
            b"date,type,symbol,units,price\n\xff\xfe,buy", default_asset_class="other"
        )


def test_no_valid_rows_raises() -> None:
    # The only data row is missing its symbol → skipped → zero importable rows.
    csv = _csv(",buy,,10,100,1000")
    with pytest.raises(CSVParseError, match="no valid investment transactions"):
        parse_investment_csv(csv, default_asset_class="indian_equity")


def test_type_vocabulary_accept_and_reject() -> None:
    rows, warnings = parse_investment_csv(
        _csv(
            "2024-01-01,buy,A,10,100,1000",
            "2024-01-02,sip,B,10,100,1000",
            "2024-01-03,sell,C,10,100,1000",
            "2024-01-04,dividend,D,,,500",
            "2024-01-05,bonus,E,10,,",
            "2024-01-06,split,F,10,100,1000",
            "2024-01-07,switch_in,G,10,100,1000",
            "2024-01-08,switch_out,H,10,100,1000",
            "2024-01-09,frobnicate,I,10,100,1000",
        ),
        default_asset_class="indian_equity",
    )
    assert [r.txn_type for r in rows] == ["buy", "sip", "sell", "dividend", "bonus"]
    # split / switch_in / switch_out / unknown → 4 skip warnings, line-number only.
    assert len(warnings) == 4
    assert all(w.startswith("row ") and "unsupported transaction type" in w for w in warnings)


def test_usd_row_accepted() -> None:
    # USD is one of the two supported currencies now — carried onto the row, not rejected.
    rows, warnings = parse_investment_csv(
        _csv(
            "2024-01-01,buy,AAPL,10,100,1000,USD",
            header="date,type,symbol,units,price,amount,currency",
        ),
        default_asset_class="us_equity",
    )
    assert warnings == []
    assert [(r.symbol, r.currency) for r in rows] == [("AAPL", "USD")]


def test_missing_currency_column_defaults_inr() -> None:
    # No currency column → every row defaults to INR (backward-compatible).
    rows, warnings = parse_investment_csv(
        _csv("2024-01-01,buy,INFY,10,100,1000"), default_asset_class="indian_equity"
    )
    assert warnings == []
    assert rows[0].currency == "INR"


def test_unsupported_currency_skips_row_not_file() -> None:
    # A third currency (EUR) skips just that row with a warning; the rest still imports.
    rows, warnings = parse_investment_csv(
        _csv(
            "2024-01-01,buy,SAP,10,100,1000,EUR",
            "2024-01-02,buy,INFY,10,100,1000,INR",
            header="date,type,symbol,units,price,amount,currency",
        ),
        default_asset_class="indian_equity",
    )
    assert [r.symbol for r in rows] == ["INFY"]
    assert len(warnings) == 1 and "unsupported currency EUR" in warnings[0]
    assert warnings[0].startswith("row ")


def test_us_class_with_inr_currency_skipped() -> None:
    # A us_equity/us_etf row resolving to INR is a mis-pricing contradiction (Yahoo prices
    # it in USD) → skipped with a warning, rest of file imports.
    rows, warnings = parse_investment_csv(
        _csv(
            "2024-01-01,buy,AAPL,10,100,1000,INR,us_equity",
            "2024-01-02,buy,VOO,10,100,1000,,us_etf",
            "2024-01-03,buy,INFY,10,100,1000,INR,indian_equity",
            header="date,type,symbol,units,price,amount,currency,asset_class",
        ),
        default_asset_class="indian_equity",
    )
    assert [r.symbol for r in rows] == ["INFY"]
    assert len(warnings) == 2
    assert all("requires currency=USD" in w and w.startswith("row ") for w in warnings)


def test_amount_authoritative_when_present() -> None:
    rows, _ = parse_investment_csv(
        _csv("2024-01-01,buy,INFY,10,100,999"), default_asset_class="other"
    )
    assert rows[0].amount_native_paise == 99900  # 999.00, NOT units*price (1000.00)


def test_amount_derived_when_absent() -> None:
    csv = _csv("2024-01-01,buy,INFY,10,100", header="date,type,symbol,units,price")
    rows, _ = parse_investment_csv(csv, default_asset_class="other")
    assert rows[0].amount_native_paise == 100000  # 10 * 100 = 1000.00


def test_small_amount_precision_no_float_drift() -> None:
    csv = _csv("2024-01-01,buy,INFY,0.001,1234.5678", header="date,type,symbol,units,price")
    rows, _ = parse_investment_csv(csv, default_asset_class="other")
    assert str(rows[0].units) == "0.00100000"
    # 0.001 * 1234.5678 = 1.2345678 → 123.45678 paise → HALF_EVEN → 123.
    assert rows[0].amount_native_paise == 123


def test_both_date_formats_parse() -> None:
    rows, warnings = parse_investment_csv(
        _csv("15-03-2024,buy,A,10,100,1000", "2024-03-16,buy,B,10,100,1000"),
        default_asset_class="other",
    )
    assert warnings == []
    assert [r.date.isoformat() for r in rows] == ["2024-03-15", "2024-03-16"]


def test_unknown_asset_class_and_exchange_skip_with_warning() -> None:
    rows, warnings = parse_investment_csv(
        _csv(
            "2024-01-01,buy,A,10,100,1000,wat",
            "2024-01-02,buy,B,10,100,1000,gold",
            header="date,type,symbol,units,price,amount,asset_class",
        ),
        default_asset_class="other",
    )
    assert [r.symbol for r in rows] == ["B"]
    assert len(warnings) == 1 and "unknown asset_class" in warnings[0]


def test_leading_blank_lines_tolerated() -> None:
    raw = b"\n\ndate,type,symbol,units,price\n2024-01-01,buy,INFY,10,100\n"
    rows, _ = parse_investment_csv(raw, default_asset_class="other")
    assert [r.symbol for r in rows] == ["INFY"]


def test_isin_captured_uppercased_and_blank_is_none() -> None:
    # Row 1: a mutual-fund ISIN (INF prefix) — the kind the AMFI NAVAll match keys on —
    # captured and upper-cased. Row 2: a present-but-blank isin cell → None.
    csv = _csv(
        "2024-01-01,buy,PPFAS,10,100,1000,inf209k01yv4",
        "2024-01-02,buy,QUANT,10,100,1000,",
        header="date,type,symbol,units,price,amount,isin",
    )
    rows, warnings = parse_investment_csv(csv, default_asset_class="indian_mf")
    assert warnings == []
    assert rows[0].isin == "INF209K01YV4"
    assert rows[1].isin is None


def test_isin_absent_column_is_none() -> None:
    # Default header carries no isin column at all.
    rows, _ = parse_investment_csv(
        _csv("2024-01-01,buy,INFY,10,100,1000"), default_asset_class="indian_equity"
    )
    assert rows[0].isin is None
