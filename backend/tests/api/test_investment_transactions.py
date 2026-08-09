"""End-to-end tests for ``/api/v1/investment-transactions`` (PRD §F7).

Covers POST (per-type validators, server-stamped FX — INR→1, USD→cached rate, 422 when
uncached, extra="forbid" on a client-sent fx field — instrument-ownership 422, decimal-string
round-trip), GET (filters + date-range 422), PATCH (note-only), and DELETE (hard delete).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import FxRateQuote, Instrument, User


def _buy(instrument_id: int, **over: object) -> dict[str, object]:
    body: dict[str, object] = {
        "date": "2026-06-01",
        "instrument_id": instrument_id,
        "transaction_type": "buy",
        "units": "10",
        "price_per_unit_native": "100",
        "amount_native_paise": 100_000,
    }
    body.update(over)
    return body


def _reinvest(instrument_id: int, **over: object) -> dict[str, object]:
    """An IDCW reinvestment: ₹100 of dividend became 0.8 units at NAV ₹125."""
    body: dict[str, object] = {
        "date": "2025-12-19",
        "instrument_id": instrument_id,
        "amount_native_paise": 10_000,
        "units": "0.8",
        "price_per_unit_native": "125",
    }
    body.update(over)
    return body


def test_reinvestment_records_both_legs(
    client: TestClient, seeded_user: User, instrument: Instrument
) -> None:
    """D3: an IDCW dividend-reinvestment plan is recordable as a linked pair.

    Before this, no shape expressed "₹X of dividend became Y units at NAV Z on
    date D": ``dividend`` rejects units, and folding the units onto the dividend
    row would conflate income with acquisition — which is what breaks FIFO
    holding periods.
    """
    resp = client.post(
        "/api/v1/investment-transactions/reinvestment", json=_reinvest(instrument.id)
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()

    div, buy = body["dividend"], body["buy"]
    assert div["transaction_type"] == "dividend"
    assert div["units"] == "0"
    assert div["price_per_unit_native"] is None
    assert div["amount_native_paise"] == 10_000

    assert buy["transaction_type"] == "buy"
    assert buy["units"] == "0.8"
    assert buy["price_per_unit_native"] == "125"
    assert buy["amount_native_paise"] == 10_000

    assert div["date"] == buy["date"] == "2025-12-19"
    assert div["fx_rate_to_inr"] == buy["fx_rate_to_inr"] == "1"


def _post_reinvestment(client: TestClient, instrument_id: int, **over: object) -> dict[str, dict]:
    resp = client.post(
        "/api/v1/investment-transactions/reinvestment", json=_reinvest(instrument_id, **over)
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_reinvestment_writes_both_pair_directions(
    client: TestClient, seeded_user: User, instrument: Instrument
) -> None:
    """First-ever coverage of the pair column — and of the writer contract.

    Both directions must be set: the delete path keys on ``txn.pair_id``, so a
    one-directional link would leave a dangling composite FK when the pointed-at row
    is deleted first. ``div.id < buy.id`` is also load-bearing — the FIFO replay's
    same-date tie-break is id-ascending, so a date-ordered listing reads
    income → acquisition.
    """
    body = _post_reinvestment(client, instrument.id)
    div, buy = body["dividend"], body["buy"]

    assert div["pair_id"] == buy["id"]
    assert buy["pair_id"] == div["id"]
    assert div["id"] < buy["id"]


def test_list_exposes_non_null_pair_id_on_both_legs(
    client: TestClient, seeded_user: User, instrument: Instrument
) -> None:
    """The contract the /investments board's "↔ paired" badge reads.

    The delete tests above only cover ``pair_id`` arriving as NULL on the LIST endpoint;
    the badge needs the populated case. Without this a schema change that dropped
    ``pair_id`` from ``InvestmentTransactionRead`` would leave the board silently
    un-badged, rendering a reinvestment as two unrelated same-date rows again — the
    exact drift the pair exists to prevent.

    Also pins listing order (date desc, id desc), which is why the two legs render
    adjacent: the ``buy`` takes the higher id, so it lists first.
    """
    body = _post_reinvestment(client, instrument.id)

    rows = client.get("/api/v1/investment-transactions").json()
    assert [r["id"] for r in rows] == [body["buy"]["id"], body["dividend"]["id"]]
    by_id = {r["id"]: r for r in rows}
    assert by_id[body["buy"]["id"]]["pair_id"] == body["dividend"]["id"]
    assert by_id[body["dividend"]["id"]]["pair_id"] == body["buy"]["id"]


def test_delete_dividend_leg_nulls_the_buy_pointer(
    client: TestClient, seeded_user: User, instrument: Instrument
) -> None:
    body = _post_reinvestment(client, instrument.id)
    assert (
        client.delete(f"/api/v1/investment-transactions/{body['dividend']['id']}").status_code
        == 204
    )

    remaining = client.get("/api/v1/investment-transactions").json()
    assert len(remaining) == 1
    assert remaining[0]["id"] == body["buy"]["id"]
    assert remaining[0]["pair_id"] is None


def test_delete_buy_leg_nulls_the_dividend_pointer(
    client: TestClient, seeded_user: User, instrument: Instrument
) -> None:
    """The mirror case. With FK enforcement ON, a missing null-out raises
    IntegrityError rather than corrupting quietly — so this is a real guard, not
    symmetry for its own sake."""
    body = _post_reinvestment(client, instrument.id)
    assert client.delete(f"/api/v1/investment-transactions/{body['buy']['id']}").status_code == 204

    remaining = client.get("/api/v1/investment-transactions").json()
    assert len(remaining) == 1
    assert remaining[0]["id"] == body["dividend"]["id"]
    assert remaining[0]["pair_id"] is None


def test_delete_both_legs_unwinds_cleanly(
    client: TestClient, seeded_user: User, instrument: Instrument
) -> None:
    """A full unwind leaves no dangling composite FK."""
    body = _post_reinvestment(client, instrument.id)
    for leg in ("dividend", "buy"):
        assert (
            client.delete(f"/api/v1/investment-transactions/{body[leg]['id']}").status_code == 204
        )
    assert client.get("/api/v1/investment-transactions").json() == []


def test_reinvestment_rejects_fees_and_non_positive_magnitudes(
    client: TestClient, seeded_user: User, instrument: Instrument
) -> None:
    """``extra="forbid"`` plus the field constraints, with no model_validator."""
    # A reinvestment carries no brokerage — a client fee fails loudly, never silently
    # capitalising into the lot.
    for over in (
        {"fees_native_paise": 100},
        {"amount_native_paise": 0},
        {"units": "0"},
        {"price_per_unit_native": "0"},
    ):
        resp = client.post(
            "/api/v1/investment-transactions/reinvestment", json=_reinvest(instrument.id, **over)
        )
        assert resp.status_code == 422, (over, resp.text)


def test_reinvestment_unknown_instrument_422(client: TestClient, seeded_user: User) -> None:
    resp = client.post("/api/v1/investment-transactions/reinvestment", json=_reinvest(999))
    assert resp.status_code == 422
    assert resp.json()["detail"] == "instrument not found or archived"


def test_reinvestment_usd_without_cached_rate_422(
    client: TestClient, seeded_user: User, session: Session
) -> None:
    """FX is resolved on this path too — once, for both legs."""
    inst = _usd_instrument(session, seeded_user)
    resp = client.post("/api/v1/investment-transactions/reinvestment", json=_reinvest(inst.id))
    assert resp.status_code == 422
    assert "/fx/refresh" in resp.json()["detail"]


def test_create_buy_roundtrips_decimals(
    client: TestClient, seeded_user: User, instrument: Instrument
) -> None:
    resp = client.post("/api/v1/investment-transactions", json=_buy(instrument.id, units="10.5"))
    assert resp.status_code == 201
    body = resp.json()
    assert body["units"] == "10.5"  # decimal-as-string, not 10.5 float
    assert body["price_per_unit_native"] == "100"
    assert body["amount_native_paise"] == 100_000
    assert body["fx_rate_to_inr"] == "1"


def test_dividend_units_must_be_zero(
    client: TestClient, seeded_user: User, instrument: Instrument
) -> None:
    resp = client.post(
        "/api/v1/investment-transactions",
        json={
            "date": "2026-06-01",
            "instrument_id": instrument.id,
            "transaction_type": "dividend",
            "units": "5",
            "amount_native_paise": 5_000,
        },
    )
    assert resp.status_code == 422


def test_dividend_valid(client: TestClient, seeded_user: User, instrument: Instrument) -> None:
    resp = client.post(
        "/api/v1/investment-transactions",
        json={
            "date": "2026-06-01",
            "instrument_id": instrument.id,
            "transaction_type": "dividend",
            "units": "0",
            "amount_native_paise": 5_000,
        },
    )
    assert resp.status_code == 201


def test_bonus_amount_must_be_zero(
    client: TestClient, seeded_user: User, instrument: Instrument
) -> None:
    resp = client.post(
        "/api/v1/investment-transactions",
        json={
            "date": "2026-06-01",
            "instrument_id": instrument.id,
            "transaction_type": "bonus",
            "units": "5",
            "amount_native_paise": 100,
        },
    )
    assert resp.status_code == 422


def test_sell_requires_price(client: TestClient, seeded_user: User, instrument: Instrument) -> None:
    resp = client.post(
        "/api/v1/investment-transactions",
        json={
            "date": "2026-06-01",
            "instrument_id": instrument.id,
            "transaction_type": "sell",
            "units": "5",
            "amount_native_paise": 80_000,
        },
    )
    assert resp.status_code == 422


def test_switch_rejected_on_manual_entry(
    client: TestClient, seeded_user: User, instrument: Instrument
) -> None:
    resp = client.post(
        "/api/v1/investment-transactions",
        json=_buy(instrument.id, transaction_type="switch_in"),
    )
    assert resp.status_code == 422


def test_fx_rate_field_rejected_by_extra_forbid(
    client: TestClient, seeded_user: User, instrument: Instrument
) -> None:
    # fx_rate_to_inr is no longer a client field — the route server-stamps it. A body that
    # still sends it is rejected by extra="forbid".
    resp = client.post(
        "/api/v1/investment-transactions", json=_buy(instrument.id, fx_rate_to_inr="83.5")
    )
    assert resp.status_code == 422


def _sell(instrument_id: int, units: str, **over: object) -> dict[str, object]:
    body: dict[str, object] = {
        "date": "2026-06-02",
        "instrument_id": instrument_id,
        "transaction_type": "sell",
        "units": units,
        "price_per_unit_native": "150",
        "amount_native_paise": 150_000,
    }
    body.update(over)
    return body


def test_sell_exceeding_holdings_422(
    client: TestClient, seeded_user: User, instrument: Instrument
) -> None:
    """Selling more units than held is rejected at the write boundary (the
    read-model would only clamp+log an already-persisted oversell)."""
    client.post("/api/v1/investment-transactions", json=_buy(instrument.id, units="10"))
    resp = client.post("/api/v1/investment-transactions", json=_sell(instrument.id, units="15"))
    assert resp.status_code == 422
    assert resp.json()["detail"] == "sell/switch_out exceeds available units for this instrument"


def test_partial_sell_ok(client: TestClient, seeded_user: User, instrument: Instrument) -> None:
    client.post("/api/v1/investment-transactions", json=_buy(instrument.id, units="10"))
    resp = client.post("/api/v1/investment-transactions", json=_sell(instrument.id, units="4"))
    assert resp.status_code == 201


def test_sell_exact_holdings_ok(
    client: TestClient, seeded_user: User, instrument: Instrument
) -> None:
    """Selling exactly the held quantity must pass (strict `>` guard, not `>=`)."""
    client.post("/api/v1/investment-transactions", json=_buy(instrument.id, units="10"))
    resp = client.post("/api/v1/investment-transactions", json=_sell(instrument.id, units="10"))
    assert resp.status_code == 201


def test_sell_may_draw_on_bonus_units(
    client: TestClient, seeded_user: User, instrument: Instrument
) -> None:
    """buy(10) + bonus(5) → a sell of 12 is ACCEPTED. Bonus units are sellable.

    This is the only production path where that matters. It was bought as the net under
    a queued unit-sign consolidation, which has since landed: the four hand-mirrored
    spellings are now one ``holdings_service.UNIT_SIGN`` map (A2.7/A4.1), and picking
    the wrong one for ``bonus`` would 422 this legitimate sell through both
    ``available_units`` callers.
    """
    client.post("/api/v1/investment-transactions", json=_buy(instrument.id, units="10"))
    bonus = client.post(
        "/api/v1/investment-transactions",
        # bonus is free units: no price, no cashflow (schema rule).
        json=_buy(
            instrument.id,
            units="5",
            transaction_type="bonus",
            price_per_unit_native=None,
            amount_native_paise=0,
        ),
    )
    assert bonus.status_code == 201, bonus.json()

    resp = client.post("/api/v1/investment-transactions", json=_sell(instrument.id, units="12"))
    assert resp.status_code == 201, resp.json()


def _usd_instrument(session: Session, user: User) -> Instrument:
    inst = Instrument(
        user_id=user.id,
        symbol="AAPL",
        name="Apple",
        asset_class="us_equity",
        currency="USD",
        exchange="NASDAQ",
    )
    session.add(inst)
    session.flush()
    return inst


def test_usd_create_stamps_cached_rate(
    client: TestClient, seeded_user: User, session: Session
) -> None:
    # The route server-stamps fx_rate_to_inr from the USD→INR rate cached on/before the date.
    inst = _usd_instrument(session, seeded_user)
    session.add(
        FxRateQuote(
            date=date(2026, 6, 1),
            from_currency="USD",
            to_currency="INR",
            rate=Decimal("83.5"),
            source="seed",
        )
    )
    session.flush()

    resp = client.post("/api/v1/investment-transactions", json=_buy(inst.id))
    assert resp.status_code == 201
    assert resp.json()["fx_rate_to_inr"] == "83.5"


def test_usd_create_without_cached_rate_422(
    client: TestClient, seeded_user: User, session: Session
) -> None:
    # USD instrument, no cached rate → the route 422s rather than mis-stamp 1.
    inst = _usd_instrument(session, seeded_user)
    resp = client.post("/api/v1/investment-transactions", json=_buy(inst.id))
    assert resp.status_code == 422
    assert "/fx/refresh" in resp.json()["detail"]


def test_unknown_instrument_422(client: TestClient, seeded_user: User) -> None:
    resp = client.post("/api/v1/investment-transactions", json=_buy(999))
    assert resp.status_code == 422
    assert "instrument" in resp.json()["detail"]


def test_over_precise_units_rejected(
    client: TestClient, seeded_user: User, instrument: Instrument
) -> None:
    # 9 decimal places exceeds the 8dp storage scale — rejected at the boundary,
    # not silently rounded at bind.
    resp = client.post(
        "/api/v1/investment-transactions", json=_buy(instrument.id, units="1.123456789")
    )
    assert resp.status_code == 422


def test_list_filters_and_date_range(
    client: TestClient, seeded_user: User, instrument: Instrument
) -> None:
    client.post("/api/v1/investment-transactions", json=_buy(instrument.id, date="2026-06-01"))
    client.post(
        "/api/v1/investment-transactions",
        json={
            "date": "2026-06-10",
            "instrument_id": instrument.id,
            "transaction_type": "dividend",
            "units": "0",
            "amount_native_paise": 5_000,
        },
    )
    # Newest-first ordering.
    all_rows = client.get("/api/v1/investment-transactions").json()
    assert [r["date"] for r in all_rows] == ["2026-06-10", "2026-06-01"]
    # Type filter.
    buys = client.get("/api/v1/investment-transactions?transaction_type=buy").json()
    assert len(buys) == 1 and buys[0]["transaction_type"] == "buy"
    # date_from > date_to → 422.
    bad = client.get("/api/v1/investment-transactions?date_from=2026-06-10&date_to=2026-06-01")
    assert bad.status_code == 422


def test_patch_note_only(client: TestClient, seeded_user: User, instrument: Instrument) -> None:
    tid = client.post("/api/v1/investment-transactions", json=_buy(instrument.id)).json()["id"]
    resp = client.patch(f"/api/v1/investment-transactions/{tid}", json={"note": "rebalance"})
    assert resp.status_code == 200
    assert resp.json()["note"] == "rebalance"
    # units is locked (extra="forbid").
    assert (
        client.patch(f"/api/v1/investment-transactions/{tid}", json={"units": "99"}).status_code
        == 422
    )


def test_delete_hard(client: TestClient, seeded_user: User, instrument: Instrument) -> None:
    tid = client.post("/api/v1/investment-transactions", json=_buy(instrument.id)).json()["id"]
    assert client.delete(f"/api/v1/investment-transactions/{tid}").status_code == 204
    assert client.delete(f"/api/v1/investment-transactions/{tid}").status_code == 404
    assert client.get("/api/v1/investment-transactions").json() == []
