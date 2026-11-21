"""Shared utilities for market pricing."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from app.core import clock


def utcnow() -> datetime:
    """Current time as a naive UTC datetime."""
    return clock.naive_utcnow()


def weekdays_between(start: date, end: date) -> int:
    """Number of Mon-Fri days strictly between ``start`` and ``end``."""
    count = 0
    day = start + timedelta(days=1)
    while day < end:
        if day.weekday() < 5:
            count += 1
        day += timedelta(days=1)
    return count


def coerce_date(value: object) -> date:
    """Accept a date, datetime, ISO string, or YYYYMMDD integer."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    if isinstance(value, int) and 10000101 <= value <= 99991231:
        return date(value // 10000, value // 100 % 100, value % 100)
    raise TypeError(f"cannot interpret {value!r} as a date")
