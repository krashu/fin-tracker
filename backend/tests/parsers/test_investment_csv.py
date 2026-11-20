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
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.parsers import CSVParseError, UnsupportedFileFormatError, parse_investment_csv
from app.parsers.investment_csv import FALLBACK_HEADER_ALIASES, HEADER_ALIASES

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
    for field, spellings in FALLBACK_HEADER_ALIASES.items():
        for spelling in spellings:
            assert spelling == spelling.lower(), f"{spelling!r} must be lowercase"
            assert spelling not in seen, f"fallback {spelling!r} already mapped in {seen[spelling]}"
            seen[spelling] = f"fallback:{field}"


def test_drivewealth_us_transactions_csv() -> None:
    # INDmoney / DriveWealth US stock trade report export
    csv_bytes = (
        b"Account Details,,,,,,,,,\n"
        b"Broker Name,DriveWealth,,,,,,,,\n"
        b"Broker Account,,,,,,,,,\n"
        b"Period From,2022-01-01,,,,,,,,\n"
        b"Period To,2026-11-20,,,,,,,,\n"
        b"Total Buy Amount ($),100.00,,,,,,,,\n"
        b"Total Sell Amount ($),50.00,,,,,,,,\n"
        b",,,,,,,,,\n"
        b",,,,,,,,,\n"
        b"Stock Name,Stock Symbol,Order Placed Time,Order Execution Time,"
        b"Broker Reference Id,Transaction Type,Order Type,Quantity,Price ($),"
        b"Order Amount ($),Brokerage ($)\n"
        b'"Invesco QQQ Trust, Series 1",QQQ,"07 Dec 2021, 12:40 AM",'
        b'"26 Jan 2022, 09:29 AM",IL.123,BUY,market,0.05164355,387.27,20,0\n'
        b'Apple Inc. Common Stock,AAPL,"14 Feb 2022, 10:42 PM",'
        b'"14 Feb 2022, 10:42 PM",JB.456,BUY,market,0.05911213,169.17,10,0.01\n'
        b'Apple Inc. Common Stock,AAPL,"07 Oct 2026, 08:26 PM",'
        b'"07 Oct 2026, 08:26 PM",NJ.789,SELL,market,0.05,300.00,15,0.05\n'
        b",,,,,,,,,\n"
        b"Disclaimer:-,,,,,,,,,\n"
        b"* This Profit and Loss report is for informational purposes only.,,,,,,,,,\n"
        b"* INDmoney does not make any warranty.,,,,,,,,,\n"
    )
    rows, warnings = parse_investment_csv(csv_bytes, default_asset_class="us_equity")
    assert warnings == []
    assert len(rows) == 3
    assert rows[0].symbol == "QQQ"
    assert rows[0].date == date(2022, 1, 26)
    assert rows[0].txn_type == "buy"
    assert rows[0].currency == "USD"
    assert rows[0].amount_native_paise == 2000
    assert rows[0].fees_native_paise == 0

    assert rows[1].symbol == "AAPL"
    assert rows[1].date == date(2022, 2, 14)
    assert rows[1].txn_type == "buy"
    assert rows[1].currency == "USD"
    assert rows[1].amount_native_paise == 1000
    assert rows[1].fees_native_paise == 1

    assert rows[2].symbol == "AAPL"
    assert rows[2].date == date(2026, 10, 7)
    assert rows[2].txn_type == "sell"
    assert rows[2].currency == "USD"
    assert rows[2].amount_native_paise == 1500
    assert rows[2].fees_native_paise == 5


def test_us_stock_split_adjustment() -> None:
    # GOOG split 20:1 on 2022-07-18
    # NVDA split 4:1 on 2021-07-20 and 10:1 on 2024-06-10
    csv_text = (
        "date,type,symbol,units,price,amount,currency,asset_class\n"
        # Pre-split GOOG buy (should rescale 20x)
        "2022-02-23,buy,GOOG,0.0038043,2628.60,10.00,USD,us_equity\n"
        # Post-split GOOG sell (no rescale)
        "2022-12-08,sell,GOOG,0.076086,94.37,7.18,USD,us_equity\n"
        # Pre-2021 NVDA buy (rescale 40x)
        "2020-05-15,buy,NVDA,1.0,500.00,500.00,USD,us_equity\n"
        # Between splits NVDA buy (rescale 10x)
        "2022-10-15,buy,NVDA,2.0,120.00,240.00,USD,us_equity\n"
        # Post-2024 NVDA buy (no rescale)
        "2025-01-15,buy,NVDA,5.0,130.00,650.00,USD,us_equity\n"
        # Non-US row (INR equity) is untouched even if symbol collides
        "2022-02-23,buy,GOOG,1.0,2000.00,2000.00,INR,indian_equity\n"
    )
    rows, warnings = parse_investment_csv(csv_text.encode("utf-8"), default_asset_class="us_equity")
    assert warnings == []
    assert len(rows) == 6

    # 1. Pre-split GOOG: units * 20, price / 20, amount_native_paise preserved
    goog_buy = rows[0]
    assert goog_buy.units == Decimal("0.07608600")
    assert goog_buy.price == Decimal("131.43000000")
    assert goog_buy.amount_native_paise == 1000

    # 2. Post-split GOOG: unchanged
    goog_sell = rows[1]
    assert goog_sell.units == Decimal("0.07608600")
    assert goog_sell.price == Decimal("94.37000000")
    assert goog_sell.amount_native_paise == 718

    # 3. Pre-2021 NVDA: units * 40, price / 40
    nvda_2020 = rows[2]
    assert nvda_2020.units == Decimal("40.00000000")
    assert nvda_2020.price == Decimal("12.50000000")
    assert nvda_2020.amount_native_paise == 50000

    # 4. Between-splits NVDA: units * 10, price / 10
    nvda_2022 = rows[3]
    assert nvda_2022.units == Decimal("20.00000000")
    assert nvda_2022.price == Decimal("12.00000000")
    assert nvda_2022.amount_native_paise == 24000

    # 5. Post-2024 NVDA: unchanged
    nvda_2025 = rows[4]
    assert nvda_2025.units == Decimal("5.00000000")
    assert nvda_2025.price == Decimal("130.00000000")
    assert nvda_2025.amount_native_paise == 65000

    # 6. INR row: unchanged
    inr_row = rows[5]
    assert inr_row.units == Decimal("1.00000000")
    assert inr_row.price == Decimal("2000.00000000")
    assert inr_row.currency == "INR"


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


def test_indian_class_usd_row_skipped() -> None:
    # indian_* are rupee-traded: a USD row is a contradiction, skipped like the US/INR one.
    rows, warnings = parse_investment_csv(
        _csv(
            "2024-01-01,buy,A,10,100,1000,USD,indian_equity",
            "2024-01-02,buy,B,10,100,1000,USD,",
            "2024-01-03,buy,C,10,100,1000,INR,indian_equity",
            header="date,type,symbol,units,price,amount,currency,asset_class",
        ),
        default_asset_class="indian_mf",
    )
    assert [r.symbol for r in rows] == ["C"]
    assert len(warnings) == 2
    assert all(w.startswith("row ") and "requires currency=INR" in w for w in warnings)


def test_nps_usd_row_skipped() -> None:
    # nps is a rupee-only scheme: pinned INR beside indian_equity / indian_mf (PRD §F7).
    rows, warnings = parse_investment_csv(
        _csv(
            "2024-01-01,buy,NPS1,10,100,1000,USD,nps",
            "2024-01-02,buy,NPS2,10,100,1000,INR,nps",
            header="date,type,symbol,units,price,amount,currency,asset_class",
        ),
        default_asset_class="indian_mf",
    )
    assert [r.symbol for r in rows] == ["NPS2"]
    assert len(warnings) == 1
    assert "requires currency=INR" in warnings[0]


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


def test_net_amount_is_not_read_as_the_gross_amount() -> None:
    """``amount`` is the gross trade value; fees are held separately and subtracted later.

    A ``net_amount`` column would be taken off by the fees a second time, so it is not an
    alias: with only that column the amount falls back to ``units * price``.
    """
    rows, _ = parse_investment_csv(
        _csv("2024-01-01,buy,INFY,10,100,990", header="date,type,symbol,units,price,net_amount"),
        default_asset_class="other",
    )
    assert rows[0].amount_native_paise == 100000  # 10 * 100, not the 990 net figure


def test_zerodha_tradebook_excel_snapshot() -> None:
    raw = (FIXTURES / "zerodha_tradebook.xlsx").read_bytes()
    expected = json.loads((FIXTURES / "zerodha_tradebook.expected.json").read_text("utf-8"))
    rows, warnings = parse_investment_csv(raw, default_asset_class="indian_equity")
    assert warnings == []
    assert _serialize(rows) == expected


def test_zerodha_tradebook_excel_with_banners() -> None:
    raw = (FIXTURES / "zerodha_tradebook_with_banners.xlsx").read_bytes()
    rows, warnings = parse_investment_csv(raw, default_asset_class="indian_equity")
    assert warnings == []
    assert len(rows) == 3
    # Header was row 4; data rows must reflect physical row numbers 5, 6, 7
    assert [r.line_no for r in rows] == [5, 6, 7]
    assert [r.symbol for r in rows] == ["INFY", "INFY", "TCS"]


def test_groww_orders_excel() -> None:
    raw = (FIXTURES / "groww_orders.xlsx").read_bytes()
    rows, warnings = parse_investment_csv(raw, default_asset_class="indian_equity")
    assert warnings == []
    assert len(rows) == 2
    assert rows[0].symbol == "INFY"
    assert rows[0].date.isoformat() == "2024-03-15"
    assert rows[0].txn_type == "buy"
    assert rows[0].amount_native_paise == 1450500
    assert rows[1].symbol == "TCS"
    assert rows[1].date.isoformat() == "2024-04-10"
    assert rows[1].txn_type == "buy"
    assert rows[1].amount_native_paise == 1900125


def test_multisheet_portfolio_excel() -> None:
    raw = (FIXTURES / "multisheet_portfolio.xlsx").read_bytes()
    rows, warnings = parse_investment_csv(raw, default_asset_class="indian_equity")
    assert warnings == []
    assert len(rows) == 1
    assert rows[0].symbol == "INFY"


def test_legacy_xls_raises_unsupported_format() -> None:
    raw = (FIXTURES / "legacy_format.xls").read_bytes()
    with pytest.raises(UnsupportedFileFormatError, match="legacy .xls format is not supported"):
        parse_investment_csv(raw, default_asset_class="indian_equity")


def test_type_vocabulary_synonyms_normalized() -> None:
    csv_bytes = _csv(
        "2024-01-01,Purchase,A,10,100,1000",
        "2024-01-02,b,B,10,100,1000",
        "2024-01-03,Redemption,C,10,100,1000",
        "2024-01-04,s,D,10,100,1000",
    )
    rows, warnings = parse_investment_csv(csv_bytes, default_asset_class="indian_equity")
    assert warnings == []
    assert [r.txn_type for r in rows] == ["buy", "buy", "sell", "sell"]
