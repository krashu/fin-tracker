"""API tests for ``POST /api/v1/fx/refresh`` (PRD §F7 FX layer).

Route-level only: query-arg plumbing (``start`` / ``end`` → service), response
shape, the ``session.commit()`` the route owns, and the degrade-to-warning path.
The frankfurter fetch/parse itself is unit-tested in
``tests/services/test_fx_service.py``; here the service function is stubbed so no
HTTP is attempted (the route builds its own ``httpx.Client`` internally).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import FxRateQuote, User
from app.services.fx_service import FxRefreshResult

_URL = "/api/v1/fx/refresh"


def test_refresh_fx_happy_path(
    client: TestClient, seeded_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _stub(session: Session, **kwargs: object) -> FxRefreshResult:
        return FxRefreshResult(
            rates_inserted=2,
            range_start=date(2026, 1, 1),
            range_end=date(2026, 1, 2),
            fetch_errors=0,
            warnings=[],
        )

    monkeypatch.setattr("app.api.v1.fx.refresh_fx_rates", _stub)
    resp = client.post(_URL)
    assert resp.status_code == 200
    assert resp.json() == {
        "rates_inserted": 2,
        "range_start": "2026-01-01",
        "range_end": "2026-01-02",
        "fetch_errors": 0,
        "warnings": [],
    }


def test_refresh_fx_passes_start_end(
    client: TestClient, seeded_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def _stub(session: Session, **kwargs: object) -> FxRefreshResult:
        captured["start"] = kwargs["start"]
        captured["end"] = kwargs["end"]
        return FxRefreshResult(rates_inserted=1)

    monkeypatch.setattr("app.api.v1.fx.refresh_fx_rates", _stub)
    resp = client.post(f"{_URL}?start=2026-01-01&end=2026-01-31")
    assert resp.status_code == 200
    assert captured == {"start": date(2026, 1, 1), "end": date(2026, 1, 31)}


def test_refresh_fx_invalid_date_422(
    client: TestClient, seeded_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed ``start`` is a query-coercion 422 — the service is never called."""
    called = False

    def _stub(session: Session, **kwargs: object) -> FxRefreshResult:
        nonlocal called
        called = True
        return FxRefreshResult()

    monkeypatch.setattr("app.api.v1.fx.refresh_fx_rates", _stub)
    resp = client.post(f"{_URL}?start=not-a-date")
    assert resp.status_code == 422
    assert called is False


def test_refresh_fx_source_down_is_200_warning(
    client: TestClient, seeded_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreachable source degrades to a counted warning, not an error."""

    def _stub(session: Session, **kwargs: object) -> FxRefreshResult:
        return FxRefreshResult(
            rates_inserted=0, fetch_errors=1, warnings=["frankfurter unreachable"]
        )

    monkeypatch.setattr("app.api.v1.fx.refresh_fx_rates", _stub)
    resp = client.post(_URL)
    assert resp.status_code == 200
    body = resp.json()
    assert body["fetch_errors"] == 1
    assert body["warnings"] == ["frankfurter unreachable"]


def test_refresh_fx_requires_auth(unauth_client: TestClient) -> None:
    """The refresh is a global-reference write — it must reject an unauthenticated
    caller with 401 (mirrors POST /instruments/refresh-navs). The Origin header the
    fixture sets clears the CSRF gate, so 401 is the auth layer, not CSRF (403)."""
    resp = unauth_client.post(_URL)
    assert resp.status_code == 401


def test_refresh_fx_commits(
    client: TestClient,
    seeded_user: User,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route owns the commit: a row the service writes via the passed session
    is durable after the response (visible on a fresh session)."""

    def _stub(session: Session, **kwargs: object) -> FxRefreshResult:
        session.add(
            FxRateQuote(
                date=date(2026, 1, 1),
                from_currency="USD",
                to_currency="INR",
                rate=Decimal("83"),
                source="stub",
            )
        )
        return FxRefreshResult(rates_inserted=1)

    monkeypatch.setattr("app.api.v1.fx.refresh_fx_rates", _stub)
    resp = client.post(_URL)
    assert resp.status_code == 200
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(FxRateQuote)) == 1
