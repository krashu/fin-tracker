"""Market NAV / price history data service (ADR-0021, PRD §F7/§F8/§F9).

High-level market data service that delegates to `market_pricing` store and query engine.
Manages system-wide shared market reference pricing with 10^-4 integer micro-units
(100 micro-units = 1 paise / 4 decimal places). Global reference data without user_id.

Sparse storage policy: only business/trading days with published quotes are stored.
In-memory LOCF (Last Observation Carried Forward) resolves weekend and holiday valuations.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core import clock
from app.core.log_config import get_logger
from app.models.market_pricing import MarketInstrument
from app.services.market_pricing import (
    FilledPoint,
    PricePoint,
    TrackRequest,
    ensure_instrument,
    filled_series,
    from_micros,
    get_instrument,
    price_on,
    to_micros,
    track,
    upsert_prices,
)

logger = get_logger(__name__)

# 10^-4 integer micro-units:
# 1 whole currency unit (₹1.0000 or $1.0000) = 10,000 micro-units.
# 1 minor currency unit (1 paise or 1 cent) = 100 micro-units.
MICROS_PER_CURRENCY_UNIT = 10_000
MICROS_PER_PAISE = 100


def to_nav_micros(val: Decimal | str | float | int) -> int:
    """Convert a price or NAV into 10^-4 integer micro-units."""
    return to_micros(val)


def from_nav_micros(micros: int) -> Decimal:
    """Convert 10^-4 integer micro-units back to a 4-decimal place Decimal."""
    return from_micros(micros)


def get_nav_as_of(
    session: Session,
    price_key: str,
    as_of: date | None = None,
    *,
    max_stale_days: int | None = None,
) -> int | None:
    """Look up the most recent NAV in micro-units on or before `as_of` via LOCF.

    Returns None if no observation exists on or before `as_of`.
    """
    as_of_date = as_of or clock.today()
    instrument = get_instrument(session, price_key)
    if instrument is None:
        return None
    point = price_on(session, instrument.id, as_of_date, max_stale_days=max_stale_days)
    return point.nav_micros if point else None


def get_nav_history_series(
    session: Session,
    price_key: str,
    start_date: date,
    end_date: date,
) -> list[FilledPoint]:
    """Retrieve daily calendar forward-filled series in [start_date, end_date]."""
    instrument = get_instrument(session, price_key)
    if instrument is None:
        return []
    return filled_series(session, instrument.id, start_date, end_date)


def apply_locf_to_dates(
    session: Session,
    price_key: str,
    dates: Sequence[date],
) -> dict[date, int]:
    """Resolve NAV in micro-units for an arbitrary list of dates using LOCF.

    Returns a dict mapping each date to its LOCF nav_micros.
    Dates before the earliest cached observation will not be present in the output.
    """
    if not dates:
        return {}
    instrument = get_instrument(session, price_key)
    if instrument is None:
        return {}

    sorted_dates = sorted(dates)
    series = filled_series(session, instrument.id, sorted_dates[0], sorted_dates[-1])
    by_day = {p.day: p.nav_micros for p in series}
    return {d: by_day[d] for d in sorted_dates if d in by_day}


def track_instrument(
    session: Session,
    price_key: str,
    first_trade_date: date | None = None,
    amfi_code: str | None = None,
    weight: float | None = None,
) -> MarketInstrument:
    """Register an instrument and queue backfill if necessary."""
    req = TrackRequest(
        price_key=price_key,
        first_trade_date=first_trade_date,
        amfi_code=amfi_code,
        weight=weight,
    )
    return track(session, [req])[0]


def record_market_nav(
    session: Session,
    price_key: str,
    nav_date: date,
    nav_micros: int,
    currency: str = "INR",
    source: str = "manual",
) -> None:
    """Directly record an observed price/NAV point into the price history store."""
    instrument = ensure_instrument(session, price_key)
    if currency and currency != instrument.currency:
        instrument.currency = currency
    upsert_prices(
        session,
        instrument.id,
        [PricePoint(day=nav_date, nav_micros=nav_micros)],
    )
    coverage = instrument.coverage
    coverage.covered_from = min(coverage.covered_from or nav_date, nav_date)
    coverage.covered_to = max(coverage.covered_to or nav_date, nav_date)
    coverage.last_success_at = clock.naive_utcnow()
    coverage.status = "ok"
