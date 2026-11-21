"""Yahoo Finance v8 chart endpoint: daily closes plus split events.

    GET {base}/v8/finance/chart/{ticker}?period1=..&period2=..&interval=1d&events=div,splits

Split handling
--------------
Yahoo's ``close`` is split-adjusted: after a 10-for-1 split every earlier close
is rewritten to a tenth of what actually traded. Caching that would mean cached
rows silently go stale on every split, and that ``units x price`` is wrong for
pre-split transactions. So prices are *de-adjusted at ingest* using the split
events from the same response:

    raw_close(d) = close(d) * product(ratio of every split with ex-date > d)

Stored rows are then immutable facts, and splits live in their own table to be
applied at read time. Set ``close_is_split_adjusted=False`` if
Yahoo is returning raw closes.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from fractions import Fraction
from urllib.parse import quote

import httpx

from ..money import to_micros
from .base import (
    FetchResult,
    InstrumentNotFound,
    PricePoint,
    SplitEvent,
    TransientUpstreamError,
    http_get,
    json_body,
)

_BLOCKED = frozenset({401, 403, 429})


def _epoch(day: date) -> int:
    return int(datetime.combine(day, time.min, tzinfo=UTC).timestamp())


def _fraction(value: object) -> Fraction:
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, float):
        return Fraction(repr(value))
    return Fraction(str(value))


class YahooChartProvider:
    name = "yahoo"

    def __init__(
        self,
        client: httpx.Client,
        base_url: str = "https://query1.finance.yahoo.com",
        *,
        close_is_split_adjusted: bool = True,
        today: Callable[[], date] = date.today,
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._adjusted = close_is_split_adjusted
        self._today = today

    def fetch_history(self, symbol: str, start: date | None = None) -> FetchResult:
        params = {
            # period2 is always "now", so every split after any returned bar is
            # inside the requested window and present in `events`.
            "period1": _epoch(start) if start is not None else 0,
            "period2": _epoch(self._today() + timedelta(days=2)),
            "interval": "1d",
            "events": "div,splits",
        }
        url = f"{self._base_url}/v8/finance/chart/{quote(symbol, safe='')}"
        try:
            response = http_get(self._client, url, params=params, blocked_statuses=_BLOCKED)
        except InstrumentNotFound:
            raise InstrumentNotFound(
                f"Yahoo has no data for {symbol} (delisted or renamed?)"
            ) from None
        chart = json_body(response).get("chart") or {}

        error = chart.get("error")
        if error:
            text = f"{error.get('code')}: {error.get('description')}"
            if "not found" in text.lower() or "no data" in text.lower():
                raise InstrumentNotFound(f"Yahoo: {text}")
            raise TransientUpstreamError(f"Yahoo: {text}")
        results = chart.get("result") or []
        if not results:
            raise TransientUpstreamError(f"Yahoo returned no result for {symbol}")
        result = results[0]

        meta = result.get("meta") or {}
        offset = timedelta(seconds=int(meta.get("gmtoffset") or 0))

        def local_day(epoch_s: int) -> date:
            # Bars are stamped at the exchange's open; shift to exchange-local date.
            return (datetime.fromtimestamp(int(epoch_s), UTC) + offset).date()

        splits: list[SplitEvent] = []
        for event in ((result.get("events") or {}).get("splits") or {}).values():
            try:
                num, den = _fraction(event["numerator"]), _fraction(event["denominator"])
                if num <= 0 or den <= 0:
                    continue
                splits.append(SplitEvent(local_day(event["date"]), num, den))
            except (KeyError, TypeError, ValueError):
                continue
        splits.sort(key=lambda s: s.day)

        timestamps = result.get("timestamp") or []
        quotes = (result.get("indicators") or {}).get("quote") or [{}]
        closes = quotes[0].get("close") or []

        by_day: dict[date, int] = {}
        for stamp, close in zip(timestamps, closes, strict=False):
            if close is None:
                continue  # halted / holiday placeholder bar
            day = local_day(stamp)
            if start is not None and day < start:
                continue
            price = Fraction(repr(float(close)))
            if self._adjusted:
                for split in splits:
                    if split.day > day:
                        price *= split.ratio
            value = to_micros(price)
            if value > 0:
                by_day[day] = value

        if not by_day and start is None:
            raise InstrumentNotFound(f"Yahoo returned an empty series for {symbol}")
        points = [PricePoint(day, by_day[day]) for day in sorted(by_day)]
        currency = meta.get("currency")
        return FetchResult(
            points=points,
            splits=splits,
            currency=currency.upper() if isinstance(currency, str) and len(currency) == 3 else None,
        )
