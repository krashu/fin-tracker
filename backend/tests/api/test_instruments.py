"""End-to-end tests for ``/api/v1/instruments`` CRUD (PRD §F7).

Covers POST (minimal + with-NAV stamping, USD accepted, 409 on duplicate active
(symbol, currency), same-symbol-different-currency both created), GET (active-only,
symbol-sorted), PATCH (rename, NAV restamp, locked-field rejection), and DELETE
(soft-delete, 404 on re-DELETE, re-create after archive).

The ``nav_as_of`` group pins the valuation-date contract
(:class:`app.models.instrument.Instrument`): the stamp is the date the price is *valid
for*, defaulting to today, never the moment of the write. The ``isin`` group pins
write-once capture and the end-to-end payoff — a fund registered here is priceable from
AMFI NAVAll, which is what the CSV importer's rows already got.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core import clock
from app.models import User
from app.services.nav_snapshot_service import refresh_navs

_MINIMAL = {
    # A scheme handle, NOT an ISIN. The ISIN has its own field now; typing one into
    # `symbol` is the dead end the review found the Add-instrument placeholder steering
    # users into, and it was baked into this fixture too.
    "symbol": "HDFCNIFTY",
    "name": "Index Fund",
    "asset_class": "indian_mf",
    "exchange": "MFCentral",
}

# Scheme 119551 from the shared NAVAll fixture — growth ISIN, and the NAV/date it serves.
_AMFI_URL = "https://amfi.test/NAVAll.txt"
_NAVALL_BODY = (
    Path(__file__).parent.parent / "fixtures" / "amfi_navall" / "navall_sample.txt"
).read_bytes()
_GROWTH_ISIN = "INF209KA12Z1"
_REINVEST_ISIN = "INF209KA13Z9"  # same scheme, different key — still a conflicting write
_FIXTURE_NAV = "105.9219"
_FIXTURE_NAV_DATE = date(2026, 6, 20)


def _stamp(d: date) -> str:
    """How a valuation date serializes: naive-UTC midnight, so no offset suffix.

    Asserting the exact string is deliberate — it pins the *date* (the whole point of
    ``nav_as_of``) and the naive encoding (ADR-0001 rule 5) in one comparison. An aware
    write would round-trip as ``...+00:00`` and fail here.
    """
    return f"{d.isoformat()}T00:00:00"


def test_create_minimal(client: TestClient, seeded_user: User) -> None:
    resp = client.post("/api/v1/instruments", json=_MINIMAL)
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] >= 1
    assert body["symbol"] == "HDFCNIFTY"
    assert body["currency"] == "INR"
    assert body["isin"] is None
    assert body["current_nav"] is None
    assert body["nav_updated_at"] is None
    assert body["archived_at"] is None


def test_create_with_nav_stamps_updated_at(client: TestClient, seeded_user: User) -> None:
    resp = client.post("/api/v1/instruments", json={**_MINIMAL, "current_nav": "150.5"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["current_nav"] == "150.5"  # decimal-as-string
    assert body["nav_updated_at"] is not None


def test_create_usd_accepted(client: TestClient, seeded_user: User) -> None:
    # USD instruments are accepted now (the FX layer rolls them up to INR). The former
    # v1 INR-only guard is gone.
    resp = client.post(
        "/api/v1/instruments",
        json={
            "symbol": "AAPL",
            "name": "Apple",
            "asset_class": "us_equity",
            "exchange": "NASDAQ",
            "currency": "USD",
        },
    )
    assert resp.status_code == 201
    assert resp.json()["currency"] == "USD"


def test_blank_symbol_rejected(client: TestClient, seeded_user: User) -> None:
    resp = client.post("/api/v1/instruments", json={**_MINIMAL, "symbol": "   "})
    assert resp.status_code == 422


def test_us_equity_requires_usd_currency(client: TestClient, seeded_user: User) -> None:
    # us_equity/us_etf are Yahoo-priced in USD; an INR currency would mis-value 1:1, so it's
    # rejected at create (symmetric to the CSV parser's mismatch guard).
    resp = client.post(
        "/api/v1/instruments",
        json={
            "symbol": "AAPL",
            "name": "Apple",
            "asset_class": "us_equity",
            "exchange": "NASDAQ",
            "currency": "INR",
        },
    )
    assert resp.status_code == 422


def test_duplicate_active_symbol_conflicts(client: TestClient, seeded_user: User) -> None:
    assert client.post("/api/v1/instruments", json=_MINIMAL).status_code == 201
    dup = client.post("/api/v1/instruments", json=_MINIMAL)
    assert dup.status_code == 409
    assert "HDFCNIFTY" not in dup.json()["detail"]  # no symbol echo


def test_same_symbol_different_currency_both_created(client: TestClient, seeded_user: User) -> None:
    # Active-symbol uniqueness now includes currency: a cross-listed ticker can be held
    # once in INR and once in USD without colliding.
    inr = client.post("/api/v1/instruments", json={**_MINIMAL, "symbol": "X"})
    usd = client.post(
        "/api/v1/instruments",
        json={
            "symbol": "X",
            "name": "X (US)",
            "asset_class": "us_equity",
            "exchange": "NASDAQ",
            "currency": "USD",
        },
    )
    assert inr.status_code == 201
    assert usd.status_code == 201
    assert {r["currency"] for r in client.get("/api/v1/instruments").json()} == {"INR", "USD"}


def test_list_active_only_symbol_sorted(client: TestClient, seeded_user: User) -> None:
    client.post("/api/v1/instruments", json={**_MINIMAL, "symbol": "ZED"})
    client.post("/api/v1/instruments", json={**_MINIMAL, "symbol": "ABC"})
    rows = client.get("/api/v1/instruments").json()
    assert [r["symbol"] for r in rows] == ["ABC", "ZED"]


def test_patch_name(client: TestClient, seeded_user: User) -> None:
    iid = client.post("/api/v1/instruments", json=_MINIMAL).json()["id"]
    resp = client.patch(f"/api/v1/instruments/{iid}", json={"name": "Renamed Fund"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Renamed Fund"


def test_patch_null_name_rejected(client: TestClient, seeded_user: User) -> None:
    """An explicit ``null`` name is a 422, not a 500.

    ``_strip_name`` used to open ``if v is None: return None``, passing null through to
    a NOT NULL column: the commit raised an IntegrityError the symbol/currency 409
    matcher doesn't recognise, so it surfaced as a catch-all 500. Same message
    /categories and /labels already return.
    """
    iid = client.post("/api/v1/instruments", json=_MINIMAL).json()["id"]
    resp = client.patch(f"/api/v1/instruments/{iid}", json={"name": None})
    assert resp.status_code == 422
    assert "name cannot be cleared" in resp.text
    # No single-instrument GET route; read it back off the list.
    rows = client.get("/api/v1/instruments").json()
    assert next(r for r in rows if r["id"] == iid)["name"] == _MINIMAL["name"]


def test_patch_nav_restamps_updated_at(client: TestClient, seeded_user: User) -> None:
    iid = client.post("/api/v1/instruments", json=_MINIMAL).json()["id"]
    resp = client.patch(f"/api/v1/instruments/{iid}", json={"current_nav": "200"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["current_nav"] == "200"
    assert body["nav_updated_at"] == _stamp(clock.today())


def test_patch_locked_field_rejected(client: TestClient, seeded_user: User) -> None:
    iid = client.post("/api/v1/instruments", json=_MINIMAL).json()["id"]
    # symbol / asset_class / currency / exchange are locked → extra="forbid" 422.
    assert client.patch(f"/api/v1/instruments/{iid}", json={"symbol": "OTHER"}).status_code == 422


def test_patch_missing_404(client: TestClient, seeded_user: User) -> None:
    assert client.patch("/api/v1/instruments/999", json={"name": "x"}).status_code == 404


def test_delete_soft_then_recreate(client: TestClient, seeded_user: User) -> None:
    iid = client.post("/api/v1/instruments", json=_MINIMAL).json()["id"]
    assert client.delete(f"/api/v1/instruments/{iid}").status_code == 204
    # Re-DELETE → 404 (loader filters archived).
    assert client.delete(f"/api/v1/instruments/{iid}").status_code == 404
    # Archived row frees the symbol for re-create (partial unique index).
    assert client.post("/api/v1/instruments", json=_MINIMAL).status_code == 201
    # List shows only the new active row.
    assert len(client.get("/api/v1/instruments").json()) == 1


# --------------------------------------------------------------------------------------
# nav_as_of — nav_updated_at is the VALUATION date, on the manual path too (B#48)
# --------------------------------------------------------------------------------------


def test_create_nav_as_of_is_the_valuation_date(client: TestClient, seeded_user: User) -> None:
    """The headline case: an FD priced off a 90-day-old statement reads as 90 days old.

    Before this, the route stamped the write instant, so every staleness reader answered
    0 for exactly the hand-priced classes (fd / bond / nps / gold / other) that
    ``refresh-navs`` classes as ``skipped`` and can therefore never correct.
    """
    as_of = clock.today() - timedelta(days=90)
    resp = client.post(
        "/api/v1/instruments",
        json={
            **_MINIMAL,
            "asset_class": "fd",
            "current_nav": "150.5",
            "nav_as_of": as_of.isoformat(),
        },
    )
    assert resp.status_code == 201
    assert resp.json()["nav_updated_at"] == _stamp(as_of)


def test_create_nav_without_as_of_defaults_to_today(client: TestClient, seeded_user: User) -> None:
    resp = client.post("/api/v1/instruments", json={**_MINIMAL, "current_nav": "150.5"})
    assert resp.status_code == 201
    assert resp.json()["nav_updated_at"] == _stamp(clock.today())


def test_nav_as_of_without_a_nav_is_rejected(client: TestClient, seeded_user: User) -> None:
    """It dates a price; with no price there is nothing to date."""
    resp = client.post(
        "/api/v1/instruments", json={**_MINIMAL, "nav_as_of": clock.today().isoformat()}
    )
    assert resp.status_code == 422
    assert "nav_as_of requires current_nav" in resp.text


def test_future_nav_as_of_is_rejected_but_today_is_accepted(
    client: TestClient, seeded_user: User
) -> None:
    """A typo'd year would suppress this holding's staleness warning permanently — a
    negative age never crosses the threshold — so it is a boundary 422. Today is the
    inclusive edge and must still work, or every ordinary manual entry breaks."""
    tomorrow = clock.today() + timedelta(days=1)
    resp = client.post(
        "/api/v1/instruments",
        json={**_MINIMAL, "current_nav": "150.5", "nav_as_of": tomorrow.isoformat()},
    )
    assert resp.status_code == 422
    assert "must not be in the future" in resp.text

    ok = client.post(
        "/api/v1/instruments",
        json={**_MINIMAL, "current_nav": "150.5", "nav_as_of": clock.today().isoformat()},
    )
    assert ok.status_code == 201


def test_patch_nav_as_of_restamps_an_unchanged_nav(client: TestClient, seeded_user: User) -> None:
    """Correcting the valuation date of a price you are NOT changing must take effect.

    The route's idempotency short-circuit compares every supplied field against the
    current row, so an unchanged ``current_nav`` returned 200 and wrote nothing — silently
    swallowing the exact correction ``nav_as_of`` exists to make. An explicit ``nav_as_of``
    now bypasses that check.
    """
    iid = client.post("/api/v1/instruments", json={**_MINIMAL, "current_nav": "150.5"}).json()["id"]
    corrected = clock.today() - timedelta(days=45)
    resp = client.patch(
        f"/api/v1/instruments/{iid}",
        json={"current_nav": "150.5", "nav_as_of": corrected.isoformat()},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["current_nav"] == "150.5"  # unchanged...
    assert body["nav_updated_at"] == _stamp(corrected)  # ...but re-dated


def test_patch_clearing_the_nav_clears_its_valuation_date(
    client: TestClient, seeded_user: User
) -> None:
    """A date for a price that no longer exists is incoherent. The old code stamped a
    fresh ``nav_updated_at`` onto the NULL, leaving a valuation date with nothing to
    value."""
    iid = client.post("/api/v1/instruments", json={**_MINIMAL, "current_nav": "150.5"}).json()["id"]
    resp = client.patch(f"/api/v1/instruments/{iid}", json={"current_nav": None})
    assert resp.status_code == 200
    body = resp.json()
    assert body["current_nav"] is None
    assert body["nav_updated_at"] is None


def test_patch_nav_as_of_alongside_a_cleared_nav_is_rejected(
    client: TestClient, seeded_user: User
) -> None:
    iid = client.post("/api/v1/instruments", json={**_MINIMAL, "current_nav": "150.5"}).json()["id"]
    resp = client.patch(
        f"/api/v1/instruments/{iid}",
        json={"current_nav": None, "nav_as_of": clock.today().isoformat()},
    )
    assert resp.status_code == 422
    assert "nav_as_of requires current_nav" in resp.text


def test_patch_nav_as_of_alone_is_rejected(client: TestClient, seeded_user: User) -> None:
    """``nav_as_of`` is not a column — a body carrying only it would reach the route's
    ``getattr(instrument, "nav_as_of")`` comparison. The schema stops it first."""
    iid = client.post("/api/v1/instruments", json={**_MINIMAL, "current_nav": "150.5"}).json()["id"]
    resp = client.patch(f"/api/v1/instruments/{iid}", json={"nav_as_of": clock.today().isoformat()})
    assert resp.status_code == 422


# --------------------------------------------------------------------------------------
# isin — a UI-created fund can carry one, write-once (B#52)
# --------------------------------------------------------------------------------------


def test_create_normalises_and_stores_isin(client: TestClient, seeded_user: User) -> None:
    """Padded / lower-case input is trimmed and upper-cased, exactly as the CSV parser
    does — ``_apply_mf_nav`` matches the AMFI index on exact string equality, so two
    spellings of one identity key are one unpriceable holding."""
    resp = client.post(
        "/api/v1/instruments", json={**_MINIMAL, "isin": f"  {_GROWTH_ISIN.lower()} "}
    )
    assert resp.status_code == 201
    assert resp.json()["isin"] == _GROWTH_ISIN


def test_create_rejects_a_wrong_length_isin(client: TestClient, seeded_user: User) -> None:
    """Stricter than the importer, deliberately: this is an HTTP body, and the column is
    VARCHAR(12) — SQLite would store an over-long value happily and Postgres would raise."""
    assert (
        client.post("/api/v1/instruments", json={**_MINIMAL, "isin": "TOOSHORT"}).status_code == 422
    )


def test_patch_fills_a_null_isin(client: TestClient, seeded_user: User) -> None:
    """The recovery path for an instrument registered before its ISIN was known. It also
    has to survive the route's idempotency short-circuit, which compares only the fields
    left in the dump — and ``isin`` is popped out of it."""
    iid = client.post("/api/v1/instruments", json=_MINIMAL).json()["id"]
    resp = client.patch(f"/api/v1/instruments/{iid}", json={"isin": _GROWTH_ISIN})
    assert resp.status_code == 200
    assert resp.json()["isin"] == _GROWTH_ISIN


def test_patch_conflicting_isin_is_rejected(client: TestClient, seeded_user: User) -> None:
    """Write-once. The importer's fill-if-null is silent because it ingests machine-
    generated values in bulk, where one conflicting row must not abort five hundred. A
    PATCH is one deliberate human act, and a silent no-op there would leave the user
    believing they had fixed the pricing dead-end. Re-sending the SAME value stays a
    no-op 200 — idempotent, not a conflict."""
    iid = client.post("/api/v1/instruments", json={**_MINIMAL, "isin": _GROWTH_ISIN}).json()["id"]

    conflict = client.patch(f"/api/v1/instruments/{iid}", json={"isin": _REINVEST_ISIN})
    assert conflict.status_code == 422
    assert "write-once" in conflict.text

    same = client.patch(f"/api/v1/instruments/{iid}", json={"isin": _GROWTH_ISIN})
    assert same.status_code == 200
    assert same.json()["isin"] == _GROWTH_ISIN
    # ...and the stored value is untouched by either call.
    rows = client.get("/api/v1/instruments").json()
    assert next(r for r in rows if r["id"] == iid)["isin"] == _GROWTH_ISIN


def test_patch_cannot_clear_a_set_isin(client: TestClient, seeded_user: User) -> None:
    """An explicit ``null`` is a conflicting value like any other — write-once means
    write-once, not write-once-unless-you-erase-it-first."""
    iid = client.post("/api/v1/instruments", json={**_MINIMAL, "isin": _GROWTH_ISIN}).json()["id"]
    assert client.patch(f"/api/v1/instruments/{iid}", json={"isin": None}).status_code == 422


def test_a_ui_created_fund_is_priceable_from_amfi(
    client: TestClient, session: Session, seeded_user: User
) -> None:
    """PRD §Verification §4: the UI path reaches the CSV path's pricing outcome.

    THE point of B#52. ``_apply_mf_nav`` matches strictly on ``inst.isin``, and until now
    only the CSV importer could set it — so an MF registered through Add-instrument could
    never be auto-priced, and delete-and-re-import-by-CSV was the only recovery. The
    instrument here is created through the real route (schema, normalisation and all);
    only the AMFI *source* is mocked, and the ``session`` fixture shares the engine with
    the route so it sees that committed row.

    The first bind also has to say which scheme it matched: ``isin`` is free-text with no
    checksum and no uniqueness constraint, so a wrong-but-valid one would otherwise price
    this holding off another fund permanently and silently.
    """
    created = client.post("/api/v1/instruments", json={**_MINIMAL, "isin": _GROWTH_ISIN})
    assert created.status_code == 201

    with httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=_NAVALL_BODY))
    ) as http:
        result = refresh_navs(
            session,
            user_id=seeded_user.id,
            client=http,
            amfi_url=_AMFI_URL,
            yahoo_base_url="https://yahoo.test/v8/finance/chart",
            as_of=_FIXTURE_NAV_DATE,
        )
    session.commit()

    assert result.mf_updated == 1
    assert result.unmatched == 0
    assert any("matched AMFI scheme" in w for w in result.warnings)

    row = next(r for r in client.get("/api/v1/instruments").json() if r["isin"] == _GROWTH_ISIN)
    assert row["current_nav"] == _FIXTURE_NAV
    assert row["nav_updated_at"] == _stamp(_FIXTURE_NAV_DATE)
