"""AMFI's NAVAll.txt: the latest NAV of every Indian mutual fund in one request.

This is what makes the daily refresh cheap: one download covers every fund a
household holds, instead of one mfapi call per scheme. It is also where an
ISIN is mapped to the AMFI scheme code that mfapi needs.

    Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Net Asset Value;Date
    119551;INF209KA12Z1;INF209KA13Z9;ABSL Banking & PSU Debt Fund - DIRECT;111.0395;06-Mar-2026

Section headings and fund-house names appear as lines without semicolons.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date

import httpx

from ..money import to_micros
from ..symbols import is_isin
from .base import ProviderError, RateLimited, TransientUpstreamError, http_get

_MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1
    )
}


@dataclass(frozen=True, slots=True)
class AmfiRecord:
    scheme_code: str
    isins: tuple[str, ...]
    name: str
    nav_micros: int | None  # None when AMFI prints 'N.A.'
    day: date | None


def _parse_day(raw: str) -> date | None:
    # '06-Mar-2026'; parsed by hand so the process locale cannot break it.
    parts = raw.strip().split("-")
    if len(parts) != 3:
        return None
    try:
        return date(int(parts[2]), _MONTHS[parts[1][:3].lower()], int(parts[0]))
    except (KeyError, ValueError):
        return None


def parse_navall(text: str) -> list[AmfiRecord]:
    records: list[AmfiRecord] = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(";")]
        if len(parts) < 6 or not parts[0].isdigit():
            continue  # header, blank line, section or fund-house heading
        try:
            nav = to_micros(parts[4])
        except ValueError:
            nav = None
        records.append(
            AmfiRecord(
                scheme_code=parts[0],
                isins=tuple(p.upper() for p in parts[1:3] if is_isin(p.upper())),
                name=parts[3],
                nav_micros=nav if nav and nav > 0 else None,
                day=_parse_day(parts[-1]),
            )
        )
    return records


class AmfiBulkClient:
    """Downloads and caches NAVAll.txt (in memory, with a TTL)."""

    name = "amfi"

    def __init__(
        self,
        client: httpx.Client,
        urls: Sequence[str] = ("https://portal.amfiindia.com/spages/NAVAll.txt",),
        *,
        ttl_s: float = 6 * 3600.0,
        pace: Callable[[], None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._urls = tuple(urls)
        self._ttl_s = ttl_s
        self._pace = pace
        self._monotonic = monotonic
        self._fetched_at: float | None = None
        self._records: list[AmfiRecord] = []
        self._by_isin: dict[str, AmfiRecord] = {}
        self._by_code: dict[str, AmfiRecord] = {}

    def _load(self, force: bool = False) -> None:
        fresh = self._fetched_at is not None and self._monotonic() - self._fetched_at < self._ttl_s
        if fresh and not force:
            return
        last_error: ProviderError | None = None
        for url in self._urls:
            if self._pace is not None:
                self._pace()
            try:
                text = http_get(self._client, url).text
            except RateLimited:
                raise
            except ProviderError as exc:
                last_error = exc
                continue
            records = parse_navall(text)
            if len(records) < 1:
                last_error = TransientUpstreamError(f"no scheme rows parsed from {url}")
                continue
            self._records = records
            self._by_code = {r.scheme_code: r for r in records}
            self._by_isin = {isin: r for r in records for isin in r.isins}
            self._fetched_at = self._monotonic()
            return
        raise last_error or TransientUpstreamError("no AMFI URL configured")

    def records(self, force: bool = False) -> list[AmfiRecord]:
        self._load(force)
        return self._records

    def by_isin(self, isin: str) -> AmfiRecord | None:
        self._load()
        return self._by_isin.get(isin.upper())

    def by_code(self, scheme_code: str) -> AmfiRecord | None:
        self._load()
        return self._by_code.get(str(scheme_code))

    def scheme_code_for(self, isin: str) -> str | None:
        record = self.by_isin(isin)
        return record.scheme_code if record else None
