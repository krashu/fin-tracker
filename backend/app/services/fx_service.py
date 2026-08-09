"""FX rate backfill + lookup (PRD §F7 FX layer) — fill/read ``fx_rates`` (USD→INR).

``refresh_fx_rates`` is the seed-time / manual-trigger **cold** path: it fetches USD→INR
from frankfurter.app and caches new dates. ``rate_on`` / ``latest_rate`` are the **hot**
reads (ingest stamping + portfolio rollup) — they touch only the cached rows, never the network.

Resilient like ``benchmark_service`` / ``nav_snapshot_service``: a fetch/parse failure is
counted + warned (with the trimmed cause — an SSL/cert error behind a corporate proxy is the
expected failure on the corp box), and no rows are written. Idempotent: existing
``(from_currency, to_currency, date)`` rows are skipped, so a re-run inserts only new dates.
Writes go through :func:`app.core.db_errors.insert_skip_existing` — the shared
dialect-aware ``ON CONFLICT DO NOTHING`` (SQLite v1 → Postgres v2), also used by
``benchmark_service``. The caller owns the commit.

v1 is **USD→INR only** (the one non-INR currency the investment side supports). Reads
carry-forward over weekends/holidays: ``rate_on`` returns the last-known rate with
``date <= on`` rather than fabricating a non-trading-day row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db_errors import insert_skip_existing
from app.core.log_config import get_logger
from app.models import FxRateQuote
from app.models.account import CurrencyStr
from app.parsers import FrankfurterParseError, FxRateRow, parse_frankfurter_rates

logger = get_logger(__name__)

_FROM_CCY = "USD"
_TO_CCY = "INR"
_SOURCE = "frankfurter"


@dataclass(frozen=True, slots=True)
class FxRefreshResult:
    """Summary of one backfill run. Counts + PII-safe warnings only.

    ``rates_inserted`` = new ``fx_rates`` rows written; ``range_start`` / ``range_end`` =
    the oldest / newest dates actually returned by the source (``None`` if the fetch failed);
    ``fetch_errors`` = 1 when the source fetch/parse failed (rates left untouched).
    """

    rates_inserted: int = 0
    range_start: date | None = None
    range_end: date | None = None
    fetch_errors: int = 0
    warnings: list[str] = field(default_factory=list)


def refresh_fx_rates(
    session: Session,
    *,
    client: httpx.Client,
    base_url: str,
    start: date | None = None,
    end: date | None = None,
) -> FxRefreshResult:
    """Fetch + cache USD→INR rates from frankfurter (PRD §F7).

    ``client`` + ``base_url`` are injected so the route owns config and tests use
    ``httpx.MockTransport``. No ``start`` ⇒ the ``/latest`` rate. ``start`` only ⇒ that single
    date. ``start`` + ``end`` ⇒ the inclusive range (frankfurter returns business days only).
    """
    url = _build_url(base_url, start, end)
    parsed, cause = _fetch_rates(client, url)
    if parsed is None:
        return FxRefreshResult(fetch_errors=1, warnings=[f"USD→INR rates unreachable — {cause}"])

    rows, parse_warnings = parsed
    inserted = _apply(session, rows)
    dates = [r.rate_date for r in rows]
    result = FxRefreshResult(
        rates_inserted=inserted,
        range_start=min(dates) if dates else None,
        range_end=max(dates) if dates else None,
        warnings=parse_warnings,
    )
    logger.info(
        "fx_rates_refreshed",
        rates_inserted=inserted,
        range_start=str(result.range_start),
        range_end=str(result.range_end),
    )
    return result


def _build_url(base_url: str, start: date | None, end: date | None) -> str:
    """frankfurter endpoint for latest / single-date / range, all USD→INR."""
    query = f"?from={_FROM_CCY}&to={_TO_CCY}"
    if start is None:
        return f"{base_url}/latest{query}"
    if end is None:
        return f"{base_url}/{start.isoformat()}{query}"
    return f"{base_url}/{start.isoformat()}..{end.isoformat()}{query}"


def _fetch_rates(
    client: httpx.Client, url: str
) -> tuple[tuple[list[FxRateRow], list[str]] | None, str | None]:
    """GET + parse one frankfurter response.

    Returns ``(parsed, None)`` on success or ``(None, cause)`` on any source failure — the
    trimmed cause surfaces in the caller's warning so a refresh that fetched nothing isn't
    opaque (e.g. an SSL/cert error behind a corporate proxy).
    """
    try:
        resp = client.get(url)
        resp.raise_for_status()
        return parse_frankfurter_rates(resp.content, to_currency=_TO_CCY), None
    except (httpx.HTTPError, FrankfurterParseError) as e:
        logger.warning("frankfurter_fetch_failed", error=str(e))
        return None, str(e)[:160]


def _apply(session: Session, rows: list[FxRateRow]) -> int:
    """Insert USD→INR rates not already cached for their dates. Returns rows inserted."""
    by_date: dict[date, Decimal] = {}
    for r in rows:
        # frankfurter shouldn't repeat a date; if it does, keep the first.
        by_date.setdefault(r.rate_date, r.rate)
    existing: set[date] = set(
        session.scalars(
            select(FxRateQuote.date).where(
                FxRateQuote.from_currency == _FROM_CCY,
                FxRateQuote.to_currency == _TO_CCY,
                FxRateQuote.date.in_(by_date.keys()),
            )
        )
    )
    new_rows: list[dict[str, object]] = [
        {
            "date": d,
            "from_currency": _FROM_CCY,
            "to_currency": _TO_CCY,
            "rate": rate,
            "source": _SOURCE,
        }
        for d, rate in by_date.items()
        if d not in existing
    ]
    if not new_rows:
        return 0
    insert_skip_existing(
        session,
        FxRateQuote,
        new_rows,
        conflict_cols=["from_currency", "to_currency", "date"],
        label="fx_rates",
    )
    return len(new_rows)


def rate_on(
    session: Session, *, on: date, from_ccy: str = _FROM_CCY, to_ccy: str = _TO_CCY
) -> Decimal | None:
    """Last-known rate with ``date <= on`` (carry-forward over weekends/holidays/gaps).

    The historical clock for stamping ``fx_rate_to_inr`` at a transaction date. ``None`` when
    no rate at-or-before ``on`` is cached (the caller rejects the row rather than mis-stamp).
    """
    return session.scalar(
        select(FxRateQuote.rate)
        .where(
            FxRateQuote.from_currency == from_ccy,
            FxRateQuote.to_currency == to_ccy,
            FxRateQuote.date <= on,
        )
        .order_by(FxRateQuote.date.desc())
        .limit(1)
    )


def resolve_fx_rate_to_inr(session: Session, *, currency: CurrencyStr, on: date) -> Decimal | None:
    """The ``fx_rate_to_inr`` to stamp on a transaction in ``currency`` dated ``on``.

    INR is the home currency → exact ``Decimal(1)`` (the no-op that keeps INR portfolios
    byte-identical; never touches ``fx_rates``). USD → the historical rate via :func:`rate_on`
    (carry-forward over weekends/holidays), or ``None`` when no rate at-or-before ``on`` is
    cached — the caller rejects the row / 422s rather than mis-stamp. The single source of the
    server-stamp contract, shared by the importer's ``_persist_txns`` and the manual-create route.
    """
    if currency == "INR":
        return Decimal(1)
    return rate_on(session, on=on)


def latest_rate(
    session: Session, *, from_ccy: str = _FROM_CCY, to_ccy: str = _TO_CCY
) -> Decimal | None:
    """Newest cached rate for the pair. ``None`` when nothing is cached (last-known fallback
    is exhausted → the rollup degrades and ingest rejects)."""
    return session.scalar(
        select(FxRateQuote.rate)
        .where(FxRateQuote.from_currency == from_ccy, FxRateQuote.to_currency == to_ccy)
        .order_by(FxRateQuote.date.desc())
        .limit(1)
    )


def latest_rate_date(
    session: Session, *, from_ccy: str = _FROM_CCY, to_ccy: str = _TO_CCY
) -> date | None:
    """Date of the newest cached rate for the pair (the FX-staleness signal). ``None`` if
    nothing is cached."""
    return session.scalar(
        select(FxRateQuote.date)
        .where(FxRateQuote.from_currency == from_ccy, FxRateQuote.to_currency == to_ccy)
        .order_by(FxRateQuote.date.desc())
        .limit(1)
    )
