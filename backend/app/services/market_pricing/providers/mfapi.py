"""mfapi.in: full daily NAV history of one Indian mutual fund scheme per request.

    GET https://api.mfapi.in/mf/{amfi_code}
    GET https://api.mfapi.in/mf/{amfi_code}?startDate=YYYY-MM-DD&endDate=YYYY-MM-DD

    {"meta": {...}, "data": [{"date": "20-11-2026", "nav": "85.12340"}, ...], "status": "SUCCESS"}

Dates are DD-MM-YYYY, newest first. An unknown code returns an empty ``data`` list.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime

import httpx

from ..money import to_micros
from .base import FetchResult, InstrumentNotFound, PricePoint, http_get, json_body


class MfApiProvider:
    name = "mfapi"

    def __init__(
        self,
        client: httpx.Client,
        base_url: str = "https://api.mfapi.in",
        today: Callable[[], date] = date.today,
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._today = today

    def fetch_history(self, symbol: str, start: date | None = None) -> FetchResult:
        params = None
        if start is not None:
            params = {"startDate": start.isoformat(), "endDate": self._today().isoformat()}
        body = json_body(http_get(self._client, f"{self._base_url}/mf/{symbol}", params=params))

        by_day: dict[date, int] = {}
        for row in body.get("data") or []:
            try:
                day = datetime.strptime(row["date"], "%d-%m-%Y").date()
                value = to_micros(row["nav"])
            except (KeyError, TypeError, ValueError):
                continue  # one bad row must not lose the series
            if value <= 0:
                continue
            # The range filter is re-applied here in case the server ignores it.
            if start is not None and day < start:
                continue
            by_day[day] = value

        if not by_day and start is None:
            raise InstrumentNotFound(f"mfapi has no NAV data for scheme {symbol}")
        points = [PricePoint(day, by_day[day]) for day in sorted(by_day)]
        return FetchResult(points=points, currency="INR")
