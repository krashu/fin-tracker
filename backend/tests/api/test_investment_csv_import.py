"""API tests for ``POST /api/v1/imports/investments`` (PRD §F7 investment CSV import).

Integration through the route + investment_import_service: the wire shape of the
summary, the multipart ``asset_class`` form field, the generic (no-echo) 422 mapping,
and the PII-safety of the per-row warnings. The parsing / dedup logic is unit-tested in
``tests/parsers`` and ``tests/services``.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.models import User

_URL = "/api/v1/imports/investments"


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
    # An in-file buy 10 then sell 15 for the same instrument: the buy imports, the
    # sell is rejected (oversell) — the running net is seeded from DB (0 here) and
    # updated only on the persisted buy, so the sell validates against 10.
    resp = _post(
        client,
        "date,type,symbol,units,price,amount\n"
        "2024-01-01,buy,INFY,10,100,1000\n"
        "2024-02-01,sell,INFY,15,150,2250\n",
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["txns_imported"] == 1
    assert body["rows_rejected"] == 1
    assert body["warnings"] and body["warnings"][0].startswith("row ")
    assert "exceeds available units" in body["warnings"][0]


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
