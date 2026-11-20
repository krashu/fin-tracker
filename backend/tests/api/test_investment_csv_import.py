"""API tests for ``POST /api/v1/imports/investments`` (PRD §F7 investment CSV import).

Integration through the route + investment_import_service: the wire shape of the
summary, the multipart ``asset_class`` form field, the generic (no-echo) 422 mapping,
and the PII-safety of the per-row warnings. The parsing / dedup logic is unit-tested in
``tests/parsers`` and ``tests/services``.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import User

_URL = "/api/v1/imports/investments"
_FIXTURES = Path(__file__).parent.parent / "fixtures" / "investment_csv"


def _post(client: TestClient, csv: str, asset_class: str = "indian_equity"):  # type: ignore[no-untyped-def]
    return client.post(
        _URL,
        files={"file": ("tradebook.csv", csv.encode("utf-8"), "text/csv")},
        data={"asset_class": asset_class},
    )


def test_import_returns_summary(client: TestClient, seeded_user: User) -> None:
    resp = _post(
        client,
        "date,type,symbol,units,price,amount\n"
        "2024-01-01,buy,INFY,10,100,1000\n"
        "2024-02-01,buy,TCS,5,200,1000\n",
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "batch_id": body["batch_id"],
        "instruments_new": 2,
        "txns_imported": 2,
        "txns_skipped_dupe": 0,
        "rows_rejected": 0,
        "already_imported": False,
        "warnings": [],
    }
    assert isinstance(body["batch_id"], int)

    holdings = client.get("/api/v1/holdings").json()["holdings"]
    assert {h["symbol"] for h in holdings} == {"INFY", "TCS"}
    # No NAV from a CSV → current value unavailable.
    assert all(h["current_value_native_paise"] is None for h in holdings)


def test_bad_csv_returns_generic_422(client: TestClient, seeded_user: User) -> None:
    # Missing the required 'type' column.
    resp = _post(client, "date,symbol,units,price\n2024-01-01,INFY,10,100\n")
    assert resp.status_code == 422
    assert resp.json()["detail"] == "could not parse investment CSV"


def test_usd_row_without_fx_rate_rejected_in_summary(client: TestClient, seeded_user: User) -> None:
    # USD is accepted by the parser now, but with no cached FX rate the row can't be stamped
    # → rejected in the summary (counted, not mis-stamped 1); the import itself still 200s.
    resp = _post(
        client,
        "date,type,symbol,units,price,amount,currency\n2024-01-01,buy,AAPL,10,100,1000,USD\n",
        asset_class="us_equity",
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["txns_imported"] == 0
    assert body["rows_rejected"] == 1
    assert body["warnings"] and body["warnings"][0].startswith("row ")
    assert "/fx/refresh" in body["warnings"][0]


def test_import_oversell_row_rejected(client: TestClient, seeded_user: User) -> None:
    # An in-file buy 10 then sell 15 for the same instrument: the one coverage replay per
    # instrument (ADR-0015 rule 9) finds the sell short, so NONE of the instrument's rows
    # import (the buy included) and the instrument the file would have created is not kept.
    resp = _post(
        client,
        "date,type,symbol,units,price,amount\n"
        "2024-01-01,buy,INFY,10,100,1000\n"
        "2024-02-01,sell,INFY,15,150,2250\n",
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["txns_imported"] == 0
    assert body["rows_rejected"] == 2
    assert body["instruments_new"] == 0
    assert body["warnings"] == [
        "row 3: sell of 15 is short by 5 units at its date"
        " \u2014 none of this instrument's rows were imported"
    ]


def test_import_partial_sell_ok(client: TestClient, seeded_user: User) -> None:
    # In-file buy 10 then sell 5: both import (the buy funds the sell in the same batch).
    resp = _post(
        client,
        "date,type,symbol,units,price,amount\n"
        "2024-01-01,buy,INFY,10,100,1000\n"
        "2024-02-01,sell,INFY,5,150,750\n",
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["txns_imported"] == 2
    assert body["rows_rejected"] == 0


def test_invalid_asset_class_returns_422(client: TestClient, seeded_user: User) -> None:
    resp = _post(
        client,
        "date,type,symbol,units,price,amount\n2024-01-01,buy,INFY,10,100,1000\n",
        asset_class="frobnicate",
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "invalid asset_class"


def test_warnings_are_pii_safe(client: TestClient, seeded_user: User) -> None:
    # A rejected row (buy with no price) carrying a distinctive token in the name
    # column — the token must never surface in the summary warnings or the body.
    token = "SECRETFOLIO99999"
    resp = _post(
        client,
        "date,type,symbol,name,units,price,amount\n"
        f"2024-01-01,buy,INFY,{token},10,,\n"
        "2024-02-01,buy,TCS,Tata,5,200,1000\n",
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["txns_imported"] == 1
    assert body["rows_rejected"] == 1
    assert all(w.startswith("row ") for w in body["warnings"])
    assert token not in resp.text


def test_over_cap_upload_is_413(client: TestClient, seeded_user: User) -> None:
    resp = client.post(
        _URL,
        files={"file": ("tradebook.csv", b"\0" * (11 * 1024 * 1024), "text/csv")},
        data={"asset_class": "indian_equity"},
    )
    assert resp.status_code == 413
    assert resp.json()["detail"] == "file too large"


def test_import_excel_file_returns_summary(client: TestClient, seeded_user: User) -> None:
    excel_bytes = (_FIXTURES / "zerodha_tradebook.xlsx").read_bytes()
    resp = client.post(
        _URL,
        files={
            "file": (
                "tradebook.xlsx",
                excel_bytes,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
        data={"asset_class": "indian_equity"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["instruments_new"] == 2
    assert body["txns_imported"] == 3
    assert body["rows_rejected"] == 0
    assert body["warnings"] == []


def test_import_legacy_xls_returns_actionable_422(client: TestClient, seeded_user: User) -> None:
    xls_bytes = (_FIXTURES / "legacy_format.xls").read_bytes()
    resp = client.post(
        _URL,
        files={"file": ("tradebook.xls", xls_bytes, "application/vnd.ms-excel")},
        data={"asset_class": "indian_equity"},
    )
    assert resp.status_code == 422
    assert "legacy .xls format is not supported" in resp.json()["detail"]


def test_drivewealth_us_import_with_fx_rate(
    client: TestClient, seeded_user: User, session: Session
) -> None:
    from datetime import date
    from decimal import Decimal

    from app.models import FxRateQuote

    session.add(
        FxRateQuote(
            from_currency="USD",
            to_currency="INR",
            date=date(2022, 1, 1),
            rate=Decimal("75.00"),
            source="frankfurter",
        )
    )
    session.commit()
    csv_text = (
        "Account Details,,,,,,,,,\n"
        "Broker Name,DriveWealth,,,,,,,,\n"
        "Broker Account,,,,,,,,,\n"
        "Period From,2022-01-01,,,,,,,,\n"
        "Period To,2026-11-20,,,,,,,,\n"
        "Total Buy Amount ($),20.00,,,,,,,,\n"
        "Total Sell Amount ($),0,,,,,,,,\n"
        ",,,,,,,,,\n"
        ",,,,,,,,,\n"
        "Stock Name,Stock Symbol,Order Placed Time,Order Execution Time,"
        "Broker Reference Id,Transaction Type,Order Type,Quantity,Price ($),"
        "Order Amount ($),Brokerage ($)\n"
        '"Invesco QQQ Trust, Series 1",QQQ,"07 Dec 2021, 12:40 AM",'
        '"26 Jan 2022, 09:29 AM",IL.123,BUY,market,0.05164355,387.27,20,0\n'
    )
    resp = _post(client, csv_text, asset_class="us_equity")
    assert resp.status_code == 200
    body = resp.json()
    assert body["txns_imported"] == 1
    assert body["instruments_new"] == 1
    assert body["rows_rejected"] == 0
    assert body["warnings"] == []

    holdings = client.get("/api/v1/holdings").json()["holdings"]
    qqq = next(h for h in holdings if h["symbol"] == "QQQ")
    assert qqq["currency"] == "USD"
    assert qqq["asset_class"] == "us_equity"
    assert qqq["net_units"] == "0.05164355"


def test_drivewealth_us_import_with_stock_split(
    client: TestClient, seeded_user: User, session: Session
) -> None:
    from datetime import date
    from decimal import Decimal

    from app.models import FxRateQuote

    session.add(
        FxRateQuote(
            from_currency="USD",
            to_currency="INR",
            date=date(2022, 1, 1),
            rate=Decimal("75.00"),
            source="frankfurter",
        )
    )
    session.commit()
    # GOOG: pre-split buy in Feb 2022 (0.0038043 @ 2628.60),
    # post-split sell in Dec 2022 (0.076086 @ 94.37).
    # Without split adjustment, selling 0.076086 oversells the 0.0038043 buy and fails.
    # With split adjustment, buy is rescaled by 20x to 0.076086 and matches cleanly.
    csv_text = (
        "Stock Name,Stock Symbol,Order Execution Time,Broker Reference Id,"
        "Transaction Type,Order Type,Quantity,Price ($),Order Amount ($),Brokerage ($)\n"
        'Alphabet Inc. Class C Capital Stock,GOOG,"23 Feb 2022, 08:09 PM",'
        "JB.1,BUY,market,0.0038043,2628.6,10,0\n"
        'Alphabet Inc. Class C Capital Stock,GOOG,"08 Dec 2022, 10:52 PM",'
        "JL.2,SELL,market,0.076086,94.37,7.18,0\n"
    )
    resp = _post(client, csv_text, asset_class="us_equity")
    assert resp.status_code == 200
    body = resp.json()
    assert body["txns_imported"] == 2
    assert body["instruments_new"] == 1
    assert body["rows_rejected"] == 0
    assert body["warnings"] == []

    # Net units for GOOG is 0 (fully closed lot, no open holding)
    holdings = client.get("/api/v1/holdings").json()["holdings"]
    assert not any(h["symbol"] == "GOOG" for h in holdings)
