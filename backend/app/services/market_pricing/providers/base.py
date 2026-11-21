"""Provider contract, shared value types and HTTP error mapping."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from fractions import Fraction
from typing import Protocol

import httpx


class ProviderError(Exception):
    """Any upstream failure."""


class RateLimited(ProviderError):
    """The host is throttling or blocking us (HTTP 429/403). Pause the whole host."""

    def __init__(self, message: str, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class InstrumentNotFound(ProviderError):
    """The host answered, and it does not know this symbol."""


class TransientUpstreamError(ProviderError):
    """Timeouts, 5xx, malformed payloads. ``network=True`` means we never got a reply."""

    def __init__(self, message: str, network: bool = False) -> None:
        super().__init__(message)
        self.network = network


@dataclass(frozen=True, slots=True)
class PricePoint:
    day: date
    nav_micros: int


@dataclass(frozen=True, slots=True)
class SplitEvent:
    day: date  # ex-date: prices on and after this day are post-split
    numerator: Fraction  # 10-for-1 -> numerator 10, denominator 1
    denominator: Fraction

    @property
    def ratio(self) -> Fraction:
        return self.numerator / self.denominator


@dataclass(slots=True)
class FetchResult:
    points: list[PricePoint]  # ascending by day, raw (as-traded) prices
    splits: list[SplitEvent] = field(default_factory=list)
    currency: str | None = None


class PriceProvider(Protocol):
    """One upstream. ``name`` doubles as the rate-limit host key."""

    name: str

    def fetch_history(self, symbol: str, start: date | None = None) -> FetchResult:
        """Fetch daily history. ``start=None`` means everything upstream has."""
        ...


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def http_get(
    client: httpx.Client,
    url: str,
    *,
    params: dict | None = None,
    blocked_statuses: frozenset[int] = frozenset({429, 403}),
) -> httpx.Response:
    """GET with upstream failures mapped onto the provider exception types."""
    try:
        response = client.get(url, params=params)
    except httpx.TimeoutException as exc:
        raise TransientUpstreamError(f"timeout: {url}", network=True) from exc
    except httpx.TransportError as exc:
        raise TransientUpstreamError(f"network error: {exc}", network=True) from exc

    status = response.status_code
    if status in blocked_statuses:
        raise RateLimited(f"HTTP {status} from {url}", _retry_after_seconds(response))
    if status == 404:
        raise InstrumentNotFound(f"HTTP 404 from {url}")
    if status >= 500:
        raise TransientUpstreamError(f"HTTP {status} from {url}")
    if status >= 400:
        raise ProviderError(f"HTTP {status} from {url}")
    return response


def json_body(response: httpx.Response) -> dict:
    try:
        body = response.json()
    except ValueError as exc:
        raise TransientUpstreamError(f"non-JSON reply from {response.request.url}") from exc
    if not isinstance(body, dict):
        raise TransientUpstreamError(f"unexpected JSON shape from {response.request.url}")
    return body
