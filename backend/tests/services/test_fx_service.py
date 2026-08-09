"""Service tests for the FX backfill + lookup (PRD §F7 FX layer).

Exercises ``refresh_fx_rates`` against an in-memory session with frankfurter mocked via
``httpx.MockTransport`` (no network), plus the ``rate_on`` / ``latest_rate`` reads. Covers:
``/latest`` caches one row; a range caches business days; idempotent re-run inserts nothing;
only missing dates inserted; rate scaled exactly through ``FxRate``; a source failure
counted-not-raised; ``rate_on`` exact / weekend carry-forward / before-earliest→None / gap;
``latest_rate`` newest / empty→None.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import FxRateQuote
from app.services.fx_service import (
    latest_rate,
    latest_rate_date,
    rate_on,
    refresh_fx_rates,
    resolve_fx_rate_to_inr,
)

_FX_URL = "https://fx.test"


def _single_body(rate_date: str, inr: object) -> bytes:
    return json.dumps({"date": rate_date, "rates": {"INR": inr}}).encode("utf-8")


def _range_body(sel: dict[str, object]) -> bytes:
    return json.dumps({"rates": {d: {"INR": r} for d, r in sel.items()}}).encode("utf-8")


def _serving(rates: dict[str, object], *, down: bool = False):  # type: ignore[no-untyped-def]
    """MockTransport handler that serves /latest, /<date>, and /<start>..<end> from ``rates``.

    Date strings are ISO so lexicographic comparison matches chronological. The single-date
    branch emulates frankfurter's "echo the nearest business day ≤ requested" behaviour.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if down:
            return httpx.Response(503)
        seg = request.url.path.rsplit("/", 1)[-1]
        if seg == "latest":
            d = max(rates)
            return httpx.Response(200, content=_single_body(d, rates[d]))
        if ".." in seg:
            start, end = seg.split("..")
            sel = {d: r for d, r in rates.items() if start <= d <= end}
            return httpx.Response(200, content=_range_body(sel))
        if seg in rates:
            return httpx.Response(200, content=_single_body(seg, rates[seg]))
        candidates = [d for d in rates if d <= seg]
        if candidates:
            d = max(candidates)
            return httpx.Response(200, content=_single_body(d, rates[d]))
        return httpx.Response(404)

    return handler


def _run(session: Session, handler, **kw):  # type: ignore[no-untyped-def]
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        return refresh_fx_rates(session, client=client, base_url=_FX_URL, **kw)


def _cached(session: Session) -> dict[date, Decimal]:
    rows = session.scalars(select(FxRateQuote))
    return {r.date: r.rate for r in rows}


def _seed(session: Session, rates: dict[date, str]) -> None:
    for d, r in rates.items():
        session.add(
            FxRateQuote(
                date=d, from_currency="USD", to_currency="INR", rate=Decimal(r), source="seed"
            )
        )
    session.flush()


def test_latest_caches_one_row(session: Session) -> None:
    handler = _serving({"2026-06-22": 83.4, "2026-06-23": 83.5})

    result = _run(session, handler)  # no start ⇒ /latest

    assert result.rates_inserted == 1
    assert result.fetch_errors == 0
    assert result.range_start == result.range_end == date(2026, 6, 23)
    assert _cached(session) == {date(2026, 6, 23): Decimal("83.5")}


def test_range_caches_business_days_and_scales_rate(session: Session) -> None:
    # 6dp rate must round-trip exactly through the FxRate scaled-int type.
    handler = _serving({"2026-06-22": 83.512345, "2026-06-23": 83.6, "2026-06-24": 83.62})

    result = _run(session, handler, start=date(2026, 6, 22), end=date(2026, 6, 24))

    assert result.rates_inserted == 3
    assert result.range_start == date(2026, 6, 22)
    assert result.range_end == date(2026, 6, 24)
    assert _cached(session)[date(2026, 6, 22)] == Decimal("83.512345")


def test_idempotent_rerun_inserts_zero(session: Session) -> None:
    handler = _serving({"2026-06-22": 83.4, "2026-06-23": 83.5})

    first = _run(session, handler, start=date(2026, 6, 22), end=date(2026, 6, 23))
    second = _run(session, handler, start=date(2026, 6, 22), end=date(2026, 6, 23))

    assert first.rates_inserted == 2
    assert second.rates_inserted == 0
    assert len(_cached(session)) == 2


def test_only_missing_dates_inserted(session: Session) -> None:
    _run(session, _serving({"2026-06-22": 83.4}), start=date(2026, 6, 22))  # cache one
    result = _run(
        session,
        _serving({"2026-06-22": 83.4, "2026-06-23": 83.5}),
        start=date(2026, 6, 22),
        end=date(2026, 6, 23),
    )

    assert result.rates_inserted == 1
    assert set(_cached(session)) == {date(2026, 6, 22), date(2026, 6, 23)}


def test_source_failure_counted_not_raised(session: Session) -> None:
    result = _run(session, _serving({}, down=True))

    assert result.fetch_errors == 1
    assert result.rates_inserted == 0
    assert result.warnings  # carries the trimmed cause (the 503)
    assert _cached(session) == {}


def test_rate_on_exact_hit(session: Session) -> None:
    _seed(session, {date(2026, 6, 23): "83.5"})
    assert rate_on(session, on=date(2026, 6, 23)) == Decimal("83.5")


def test_rate_on_carry_forward_over_weekend(session: Session) -> None:
    # Friday cached; a Sunday lookup carries Friday's rate forward (date <= on).
    _seed(session, {date(2026, 6, 19): "83.4"})  # Friday
    assert rate_on(session, on=date(2026, 6, 21)) == Decimal("83.4")  # Sunday


def test_rate_on_before_earliest_is_none(session: Session) -> None:
    _seed(session, {date(2026, 6, 19): "83.4"})
    assert rate_on(session, on=date(2026, 6, 18)) is None


def test_rate_on_picks_prior_in_gap(session: Session) -> None:
    _seed(session, {date(2026, 6, 19): "83.4", date(2026, 6, 24): "83.9"})
    # A date in the gap resolves to the most recent prior cached date, not the later one.
    assert rate_on(session, on=date(2026, 6, 22)) == Decimal("83.4")


def test_latest_rate_newest(session: Session) -> None:
    _seed(session, {date(2026, 6, 19): "83.4", date(2026, 6, 24): "83.9"})
    assert latest_rate(session) == Decimal("83.9")


def test_latest_rate_empty_is_none(session: Session) -> None:
    assert latest_rate(session) is None


def test_latest_rate_date_newest(session: Session) -> None:
    _seed(session, {date(2026, 6, 19): "83.4", date(2026, 6, 24): "83.9"})
    assert latest_rate_date(session) == date(2026, 6, 24)


def test_latest_rate_date_empty_is_none(session: Session) -> None:
    assert latest_rate_date(session) is None


def test_resolve_inr_is_exact_one_without_cache(session: Session) -> None:
    # INR → exact Decimal(1) with no fx_rates row (the byte-identical no-op for INR holdings).
    assert resolve_fx_rate_to_inr(session, currency="INR", on=date(2026, 6, 23)) == Decimal(1)


def test_resolve_usd_uses_cached_rate(session: Session) -> None:
    _seed(session, {date(2026, 6, 23): "83.5"})
    got = resolve_fx_rate_to_inr(session, currency="USD", on=date(2026, 6, 23))
    assert got == Decimal("83.5")


def test_resolve_usd_without_rate_is_none(session: Session) -> None:
    # No cached rate at-or-before the date → None (the caller rejects / 422s, never mis-stamps).
    assert resolve_fx_rate_to_inr(session, currency="USD", on=date(2026, 6, 23)) is None
