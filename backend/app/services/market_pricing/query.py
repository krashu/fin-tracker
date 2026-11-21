"""Reads: point lookups, LOCF series, split adjustment, downsampling.

Nothing here touches the network. A missing price is reported as missing.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from fractions import Fraction
from typing import cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.market_pricing import MarketCorporateAction, MarketPriceHistory

from .money import to_micros
from .providers.base import PricePoint, SplitEvent


@dataclass(frozen=True, slots=True)
class FilledPoint:
    day: date  # the calendar day being valued
    nav_micros: int
    observed_on: date  # trading day the price was actually published


def price_on(
    session: Session, instrument_id: int, day: date, *, max_stale_days: int | None = None
) -> PricePoint | None:
    """Last observation on or before ``day`` (LOCF).

    One backward seek on the primary key.
    """
    row = session.execute(
        select(MarketPriceHistory.nav_date, MarketPriceHistory.nav_micros)
        .where(
            MarketPriceHistory.instrument_id == instrument_id, MarketPriceHistory.nav_date <= day
        )
        .order_by(MarketPriceHistory.nav_date.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    if max_stale_days is not None and (day - row.nav_date).days > max_stale_days:
        return None
    return PricePoint(row.nav_date, row.nav_micros)


def prices_as_of(
    session: Session, instrument_ids: Iterable[int], day: date
) -> dict[int, PricePoint]:
    """Value a whole portfolio on one date: one seek per instrument."""
    found: dict[int, PricePoint] = {}
    for instrument_id in instrument_ids:
        point = price_on(session, instrument_id, day)
        if point is not None:
            found[instrument_id] = point
    return found


def observed_series(
    session: Session, instrument_id: int, start: date, end: date
) -> list[PricePoint]:
    """Stored trading-day rows in ``[start, end]``: one PK range scan."""
    rows = session.execute(
        select(MarketPriceHistory.nav_date, MarketPriceHistory.nav_micros)
        .where(
            MarketPriceHistory.instrument_id == instrument_id,
            MarketPriceHistory.nav_date >= start,
            MarketPriceHistory.nav_date <= end,
        )
        .order_by(MarketPriceHistory.nav_date)
    ).all()
    return [PricePoint(r.nav_date, r.nav_micros) for r in rows]


def forward_fill(
    observed: Sequence[PricePoint], start: date, end: date, seed: PricePoint | None = None
) -> list[FilledPoint]:
    """One point per calendar day, carrying the last observation forward.

    Days before the first known price are omitted rather than invented.
    """
    filled: list[FilledPoint] = []
    current = seed
    index = 0
    day = start
    one = timedelta(days=1)
    while day <= end:
        while index < len(observed) and observed[index].day <= day:
            current = observed[index]
            index += 1
        if current is not None:
            filled.append(FilledPoint(day, current.nav_micros, current.day))
        day += one
    return filled


def filled_series(
    session: Session, instrument_id: int, start: date, end: date
) -> list[FilledPoint]:
    """Daily LOCF series: one range scan plus one seek for the seed, filled in memory."""
    observed = observed_series(session, instrument_id, start, end)
    seed = None
    if not observed or observed[0].day > start:
        seed = price_on(session, instrument_id, start - timedelta(days=1))
    return forward_fill(observed, start, end, seed)


# -- splits -------------------------------------------------------------------


def splits_for(session: Session, instrument_id: int) -> list[SplitEvent]:
    rows = session.scalars(
        select(MarketCorporateAction)
        .where(
            MarketCorporateAction.instrument_id == instrument_id,
            MarketCorporateAction.kind == "split",
        )
        .order_by(MarketCorporateAction.action_date)
    ).all()
    return [
        SplitEvent(r.action_date, Fraction(repr(r.numerator)), Fraction(repr(r.denominator)))
        for r in rows
    ]


def split_factor(splits: Iterable[SplitEvent], after: date, upto: date) -> Fraction:
    """Product of split ratios with ``after < ex-date <= upto``."""
    factor = Fraction(1)
    for split in splits:
        if after < split.day <= upto:
            factor *= split.ratio
    return factor


def split_adjust[P: (PricePoint, FilledPoint)](
    points: Sequence[P], splits: Sequence[SplitEvent], as_of: date
) -> list[P]:
    """Rebase raw prices onto the share basis of ``as_of`` (for charts/benchmarks).

    Storage stays raw; adjustment is a read-time view, so a new split never
    invalidates a stored row.
    """
    if not splits:
        return list(points)
    adjusted: list[P] = []
    for point in points:
        basis_day = point.observed_on if isinstance(point, FilledPoint) else point.day
        factor = split_factor(splits, basis_day, as_of)
        value = (
            point.nav_micros
            if factor == 1
            else to_micros(Fraction(point.nav_micros, 10_000) / factor)
        )
        if isinstance(point, FilledPoint):
            adjusted.append(cast(P, FilledPoint(point.day, value, point.observed_on)))
        else:
            adjusted.append(cast(P, PricePoint(point.day, value)))
    return adjusted


# -- downsampling (read side only) ---------------------------------------------


def downsample[P: (PricePoint, FilledPoint)](points: Sequence[P], max_points: int) -> list[P]:
    """Thin a series for charting. Always keeps the first and the last point."""
    count = len(points)
    if max_points < 2 or count <= max_points:
        return list(points)
    step = (count - 1) / (max_points - 1)
    picked = sorted({round(i * step) for i in range(max_points)} | {count - 1})
    return [points[i] for i in picked]


def last_per_period[P: (PricePoint, FilledPoint)](points: Sequence[P], period: str) -> list[P]:
    """Keep the last point of each ISO week (``'W'``) or calendar month (``'M'``)."""
    if period not in ("W", "M"):
        raise ValueError("period must be 'W' or 'M'")
    out: list[P] = []
    last_key: tuple[int, int] | None = None
    for point in points:
        iso = point.day.isocalendar()
        key = (iso.year, iso.week) if period == "W" else (point.day.year, point.day.month)
        if key == last_key:
            out[-1] = point
        else:
            out.append(point)
            last_key = key
    return out
