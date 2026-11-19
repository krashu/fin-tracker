"""NAV / price snapshot (PRD §F7) — refresh ``instruments.current_nav`` from public feeds.

Routing by ``asset_class``:

* ``indian_mf`` → AMFI NAVAll (one GET; match by ISIN against **both** ISIN columns).
* ``indian_equity`` / ``us_equity`` / ``us_etf`` → Yahoo Finance v8 quote, per instrument
  (``symbol`` + ``exchange``). US tickers are bare (``AAPL``); Indian ones get ``.NS`` / ``.BO``.
  The price is stored in ``current_nav`` in the instrument's **native** currency (USD for US rows);
  conversion to INR happens at read time in the holdings/portfolio rollup, not here.
* everything else (``fd`` / ``bond`` / ``nps`` / ``gold`` / ``other``) → ``skipped`` (no source).

**``nav_updated_at`` = the source's effective NAV date**, NOT fetch time — it is the
valuation date the portfolio "today's change" (PRD §F9) and the staleness checks read.
The manual route writes the same thing from its ``nav_as_of``; the meaning is single and
:class:`app.models.instrument.Instrument` owns the statement of it.

**Skip-when-not-newer:** a matched NAV is written only if the instrument has no NAV date
yet, or the source's date is the same day or newer. This stops a lagging AMFI date (a
fund that hasn't filed today, a weekend) from clobbering a fresher hand-entered value.

The caller (the route) owns the commit — this module never calls ``Session.commit``.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from decimal import Decimal, InvalidOperation
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import clock
from app.core.log_config import get_logger
from app.models import FxRateQuote, Instrument
from app.parsers import AmfiNavRow, AmfiParseError, parse_navall
from app.services.fx_service import refresh_fx_rates
from app.services.holdings_service import max_staleness_days

logger = get_logger(__name__)

_MF_CLASS = "indian_mf"
# Classes priced via a Yahoo quote (vs AMFI for MFs, or no source for fd/bond/nps/gold/other).
_QUOTE_CLASSES = frozenset({"indian_equity", "us_equity", "us_etf"})
# Yahoo ticker suffix by exchange. US exchanges use the bare symbol ("" suffix) — distinct
# from an absent key (None → no quote source). _yahoo_ticker tests `is not None`, not truthiness.
_EXCHANGE_SUFFIX = {"NSE": ".NS", "BSE": ".BO", "NASDAQ": "", "NYSE": ""}
# Yahoo v8 serves JSON to a browser-style UA but 4xxs an empty / bot one.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# In-memory quote cache, circuit breaker and concurrency state
_quote_cache: dict[str, tuple[Quote, datetime]] = {}
_circuit_breaker_tripped_until: datetime | None = None
_last_refresh_time: dict[UUID | None, datetime] = {}
_refresh_lock = threading.Lock()


def reset_nav_state() -> None:
    """Reset quote cache, circuit breaker, and cooldown tracking (for test isolation)."""
    global _circuit_breaker_tripped_until
    _quote_cache.clear()
    _circuit_breaker_tripped_until = None
    _last_refresh_time.clear()


def is_circuit_breaker_tripped() -> bool:
    """True if upstream rate limit circuit breaker is actively suppressing requests."""
    global _circuit_breaker_tripped_until
    if _circuit_breaker_tripped_until is not None:
        if clock.utcnow() < _circuit_breaker_tripped_until:
            return True
        _circuit_breaker_tripped_until = None
    return False


def trip_circuit_breaker(hours: int = 2) -> None:
    """Trip upstream rate limit circuit breaker for `hours` hours."""
    global _circuit_breaker_tripped_until
    _circuit_breaker_tripped_until = clock.utcnow() + timedelta(hours=hours)
    logger.warning("circuit_breaker_tripped", hours=hours)


def is_fx_rate_present_for_date(session: Session, as_of: date) -> bool:
    """Check whether a USD->INR rate is already recorded for the given date."""
    exists = session.scalar(
        select(FxRateQuote.id).where(
            FxRateQuote.from_currency == "USD",
            FxRateQuote.to_currency == "INR",
            FxRateQuote.date == as_of,
        )
    )
    return exists is not None


def is_indian_equity_market_open(now_ist: datetime | None = None) -> bool:
    """Whether Indian equity markets (NSE/BSE) are currently trading (Mon-Fri 09:15-16:00 IST)."""
    if now_ist is None:
        now_ist = clock.utcnow().astimezone(clock.BUSINESS_TZ)
    if now_ist.weekday() >= 5:
        return False
    return dt_time(9, 15) <= now_ist.time() <= dt_time(16, 0)


def is_us_equity_market_open(now_ist: datetime | None = None) -> bool:
    """Whether US equity markets (NASDAQ/NYSE) are currently trading.

    US trading hours (09:30 - 16:00 Eastern) correspond in IST to roughly 19:00 to 02:00
    the next day. In IST:
    - Monday: 19:00 to 23:59 IST
    - Tuesday to Friday: 00:00 to 02:00 IST and 19:00 to 23:59 IST
    - Saturday: 00:00 to 02:00 IST
    - Sunday: closed
    """
    if now_ist is None:
        now_ist = clock.utcnow().astimezone(clock.BUSINESS_TZ)
    weekday = now_ist.weekday()
    t = now_ist.time()
    if weekday == 0:
        return t >= dt_time(19, 0)
    if 1 <= weekday <= 4:
        return t < dt_time(2, 0) or t >= dt_time(19, 0)
    if weekday == 5:
        return t < dt_time(2, 0)
    return False


def is_amfi_nav_window_open(now_ist: datetime | None = None) -> bool:
    """Whether the AMFI NAV update window is open (Mon-Fri after 21:00 IST)."""
    if now_ist is None:
        now_ist = clock.utcnow().astimezone(clock.BUSINESS_TZ)
    if now_ist.weekday() >= 5:
        return False
    return now_ist.time() >= dt_time(21, 0)


def supports_price_feed(asset_class: str, exchange: str | None = None) -> bool:
    """Whether an asset class or exchange-traded instrument has an automated price
    feed source (AMFI or Yahoo quote).
    """
    if exchange in _EXCHANGE_SUFFIX:
        return True
    return asset_class == _MF_CLASS or asset_class in _QUOTE_CLASSES


def is_manually_priced(asset_class: str, exchange: str | None = None) -> bool:
    """Whether an instrument is manually priced by the user (no automated price feed)."""
    return not supports_price_feed(asset_class, exchange)


@dataclass(frozen=True, slots=True)
class Quote:
    """One equity quote: the latest price + the market time it is valid for."""

    price: Decimal
    as_of: datetime


@dataclass(frozen=True, slots=True)
class MfIndex:
    """NAVAll reduced to what pricing needs: the ISIN→row lookup, plus the ISINs
    deliberately withheld from it.

    ``ambiguous`` is *kept* rather than discarded so that a holding matching one can be
    told why it wasn't priced. NAVAll carries a handful of these permanently (plus a
    literal ``REDEEMED`` in the ISIN column), and they are catalogue defects in AMFI's
    file, not defects in anyone's portfolio — so they are reported per-holding, never as
    a standing list. Warning about all of them unconditionally put ~5 lines nobody owned
    into every refresh panel, where they read as portfolio errors.
    """

    by_isin: dict[str, AmfiNavRow]
    ambiguous: frozenset[str]


@dataclass(frozen=True, slots=True)
class NavRefreshResult:
    """Summary of one snapshot run. Counts + PII-safe warnings only.

    ``unmatched`` = had an auto-priced class but no price found (e.g. ISIN not in
    NAVAll). ``fetch_errors`` = network / parse failures (a source-level AMFI failure
    counts once). ``stale_skipped`` = a newer existing value was kept. ``skipped`` =
    user-entered classes (no auto source) — for those a refresh is a no-op however stale
    they are.

    ``catalogue_staleness_days`` measures the **instrument catalogue**: the oldest
    valuation among *every* active priced instrument, fully-exited positions included. It
    is NOT ``PortfolioPerformance.nav_staleness_days``, which folds the same expression
    over the currently-held, FX-priceable set. One MF refreshed today plus one gold row
    created 200 days ago and long since sold makes the two report 1 and 200 for the same
    user at the same instant — both right, which is why they no longer share a name. This
    one is a catalogue-hygiene diagnostic; the portfolio one is what the user is warned on.
    """

    mf_updated: int = 0
    equity_updated: int = 0
    unmatched: int = 0
    fetch_errors: int = 0
    stale_skipped: int = 0
    skipped: int = 0
    null_nav_count: int = 0
    catalogue_staleness_days: int | None = None
    warnings: list[str] = field(default_factory=list)


def as_valuation_stamp(valuation_date: date) -> datetime:
    """Encode a valuation *date* as the ``instruments.nav_updated_at`` value.

    Naive-UTC midnight (ADR-0001 rule 5). One function rather than two literals because
    the auto path and the manual route both write this column and
    :func:`_source_is_newer` compares their outputs — an encoding that drifted between
    them would silently change which write wins.
    """
    return datetime(valuation_date.year, valuation_date.month, valuation_date.day)


def refresh_navs(
    session: Session,
    *,
    user_id: UUID,
    client: httpx.Client,
    amfi_url: str,
    yahoo_base_url: str,
    as_of: date,
    frankfurter_base_url: str | None = None,
    check_cooldown: bool = False,
    cooldown_minutes: int = 5,
    ttl_hours: int = 4,
) -> NavRefreshResult:
    """Fetch + apply current NAVs for the user's active instruments (PRD §F7).

    URLs + ``client`` are injected so the route owns config and tests use
    ``httpx.MockTransport``. ``as_of`` (the route passes ``clock.today()`` — the IST business
    date, never the host's local date) anchors the staleness computation deterministically.
    """
    if check_cooldown:
        now = clock.utcnow()
        last = _last_refresh_time.get(user_id)
        if last is not None and (now - last) < timedelta(minutes=cooldown_minutes):
            return NavRefreshResult(warnings=["Refreshed recently. Cooldown active."])
        _last_refresh_time[user_id] = now

    if is_circuit_breaker_tripped():
        return NavRefreshResult(warnings=["Circuit breaker active due to upstream rate limits."])

    instruments = list(
        session.scalars(
            select(Instrument).where(
                Instrument.user_id == user_id,
                Instrument.archived_at.is_(None),
            )
        )
    )
    warnings: list[str] = []
    mf_updated = equity_updated = unmatched = fetch_errors = stale_skipped = skipped = 0

    # AMFI is one GET for every MF holding — fetch once, up front.
    mf_index: MfIndex | None = None
    if any(i.asset_class == _MF_CLASS for i in instruments):
        mf_index, cause = _fetch_mf_index(client, amfi_url, warnings)
        if mf_index is None:
            # Network/parse failure ≠ "ISIN not found" — leave MF NAVs untouched and
            # say so once, rather than marking every MF unmatched.
            fetch_errors += 1
            warnings.append(f"AMFI source unreachable — MF NAVs unchanged — {cause}")

    for inst in instruments:
        if inst.asset_class == _MF_CLASS:
            if mf_index is None:
                continue  # source failed — leave untouched
            outcome = _apply_mf_nav(inst, mf_index, warnings)
            if outcome == "updated":
                mf_updated += 1
            elif outcome == "stale_skipped":
                stale_skipped += 1
            else:
                unmatched += 1
        elif inst.asset_class in _QUOTE_CLASSES or inst.exchange in _EXCHANGE_SUFFIX:
            outcome = _apply_equity_quote(
                inst, client, yahoo_base_url, warnings, ttl_hours=ttl_hours
            )
            if outcome == "updated":
                equity_updated += 1
            elif outcome == "stale_skipped":
                stale_skipped += 1
            elif outcome == "fetch_error":
                fetch_errors += 1
            else:
                unmatched += 1
        else:
            # fd / bond / nps / gold / other without quote source — user-entered (no auto source).
            skipped += 1

    if frankfurter_base_url and any(i.currency != "INR" for i in instruments):
        if is_fx_rate_present_for_date(session, as_of):
            logger.info("fx_rate_already_present", as_of=str(as_of))
        else:
            fx_res = refresh_fx_rates(session, client=client, base_url=frankfurter_base_url)
            warnings.extend(fx_res.warnings)
            fetch_errors += fx_res.fetch_errors

    null_nav_count = sum(1 for i in instruments if i.current_nav is None)
    # The CATALOGUE population — every active priced instrument, exited ones included.
    # `performance_service` folds the same expression over the held set; see
    # `max_staleness_days` for why the populations are passed in rather than queried.
    catalogue_staleness_days = max_staleness_days(
        (i.nav_updated_at for i in instruments if i.current_nav is not None), as_of=as_of
    )

    result = NavRefreshResult(
        mf_updated=mf_updated,
        equity_updated=equity_updated,
        unmatched=unmatched,
        fetch_errors=fetch_errors,
        stale_skipped=stale_skipped,
        skipped=skipped,
        null_nav_count=null_nav_count,
        catalogue_staleness_days=catalogue_staleness_days,
        warnings=warnings,
    )
    logger.info(
        "nav_snapshot_completed",
        mf_updated=mf_updated,
        equity_updated=equity_updated,
        unmatched=unmatched,
        fetch_errors=fetch_errors,
        stale_skipped=stale_skipped,
        skipped=skipped,
        null_nav_count=null_nav_count,
        catalogue_staleness_days=catalogue_staleness_days,
    )
    return result


def _fetch_mf_index(
    client: httpx.Client, amfi_url: str, warnings: list[str]
) -> tuple[MfIndex | None, str | None]:
    """GET + parse NAVAll into an ISIN→row index.

    Returns ``(index, None)`` on success or ``(None, cause)`` on any source failure.
    Success-path parse warnings are appended to ``warnings`` directly (a separate channel
    from the failure cause); the trimmed cause rides the return so the caller can surface
    it (e.g. a corp-proxy SSL error) instead of a bare "unreachable".
    """
    if is_circuit_breaker_tripped():
        return None, "circuit breaker active"
    try:
        resp = client.get(amfi_url)
        resp.raise_for_status()
        rows, parse_warnings = parse_navall(resp.content)
    except (httpx.HTTPError, AmfiParseError) as e:
        logger.warning("amfi_fetch_failed", error=str(e))
        if isinstance(e, httpx.HTTPStatusError) and e.response.status_code in (429, 403):
            trip_circuit_breaker()
        return None, str(e)[:160]
    warnings.extend(parse_warnings)
    return _build_isin_index(rows), None


def _build_isin_index(rows: list[AmfiNavRow]) -> MfIndex:
    """Index rows by ISIN over BOTH the growth/payout and reinvestment columns.

    A holding's ISIN can be either. If one ISIN maps to two schemes with *different*
    NAVs (a data glitch), withhold it — a holding matching it lands in ``unmatched``
    rather than getting a coin-flip price, and :func:`_apply_mf_nav` names the reason.
    Takes no ``warnings`` sink: nothing here is worth reporting until a holding is known
    to be affected (see :class:`MfIndex`).
    """
    index: dict[str, AmfiNavRow] = {}
    ambiguous: set[str] = set()
    for row in rows:
        for isin in (row.isin_growth, row.isin_reinvest):
            if isin is None:
                continue
            existing = index.get(isin)
            if existing is not None and existing.nav != row.nav:
                ambiguous.add(isin)
            else:
                index[isin] = row
    for isin in ambiguous:
        index.pop(isin, None)
    return MfIndex(by_isin=index, ambiguous=frozenset(ambiguous))


def _apply_mf_nav(inst: Instrument, index: MfIndex, warnings: list[str]) -> str:
    """Apply a matched NAV (skip-when-not-newer). Returns updated / stale_skipped / unmatched."""
    if inst.isin is None:
        warnings.append(f"instrument {inst.id}: no ISIN — cannot match AMFI NAVAll")
        return "unmatched"
    row = index.by_isin.get(inst.isin)
    if row is None:
        # Absent and withheld are different diagnoses: an ambiguous ISIN *is* in NAVAll,
        # so "not found" would send the user hunting a typo that isn't there. The ISIN is
        # public fund reference data, not PII — same reasoning as the scheme name below.
        if inst.isin in index.ambiguous:
            warnings.append(
                f"instrument {inst.id}: ISIN {inst.isin} maps to multiple AMFI schemes "
                f"with differing NAV — not priced"
            )
        else:
            warnings.append(f"instrument {inst.id}: ISIN not found in AMFI NAVAll")
        return "unmatched"
    if not _source_is_newer(inst.nav_updated_at, row.nav_date):
        return "stale_skipped"
    inst.current_nav = row.nav
    inst.nav_updated_at = as_valuation_stamp(row.nav_date)
    if inst.amfi_code is None:
        inst.amfi_code = row.scheme_code
        # First bind — name the scheme so a mistyped ISIN is visible. `isin` is free-text
        # on the Add-instrument form, it has no checksum and no uniqueness constraint, and
        # a wrong-but-VALID 12-char string matches silently and permanently: this holding
        # would then be priced off another fund's NAV with nothing anywhere to say so. The
        # scheme name is public AMFI reference data, not PII.
        warnings.append(
            f"instrument {inst.id}: ISIN matched AMFI scheme {row.scheme_name!r} — "
            f"check this is the fund you meant"
        )
    elif inst.amfi_code != row.scheme_code:
        warnings.append(
            f"instrument {inst.id}: amfi_code disagrees with matched scheme — kept existing"
        )
    return "updated"


def _yahoo_ticker(symbol: str, exchange: str, asset_class: str = "") -> str | None:
    """Map a holding to its Yahoo ticker: ``NSE → .NS``, ``BSE → .BO``, US equities/ETFs →
    the bare symbol (with '.' converted to '-' for share classes like BRK-B); an Indian exchange
    with no quote source → ``None`` (skip).

    Tests ``is not None``, not truthiness: a US exchange maps to an empty (``""``) suffix, which
    is falsy but valid — ``f"{symbol}"`` is the correct bare US ticker.
    """
    cleaned = symbol.strip().upper()
    if not cleaned:
        return None
    if asset_class in ("us_equity", "us_etf"):
        return cleaned.replace(".", "-")
    suffix = _EXCHANGE_SUFFIX.get(exchange)
    return f"{cleaned}{suffix}" if suffix is not None else None


def _fetch_equity_quote(
    client: httpx.Client, base_url: str, ticker: str, ttl_hours: int = 4
) -> tuple[Quote | None, str | None]:
    """GET one Yahoo v8 chart quote → ``(Quote, None)``, or ``(None, cause)`` on any
    failure (never raises).

    The cause distinguishes a network/SSL error from a payload with no usable price, so the
    caller's warning says which — only the caught-exception branch has an exception to
    stringify, so the data-shape branches carry their own descriptive cause.
    """
    if is_circuit_breaker_tripped():
        return None, "circuit breaker active"

    cached = _quote_cache.get(ticker)
    if cached is not None:
        quote, cached_at = cached
        if clock.utcnow() - cached_at < timedelta(hours=ttl_hours):
            return quote, None

    try:
        resp = client.get(f"{base_url}/{ticker}", headers={"User-Agent": _USER_AGENT})
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as e:  # ValueError covers malformed JSON
        logger.warning("yahoo_fetch_failed", ticker=ticker, error=str(e))
        if isinstance(e, httpx.HTTPStatusError) and e.response.status_code in (429, 403):
            trip_circuit_breaker()
        return None, str(e)[:160]
    results = (data.get("chart") or {}).get("result")
    if not results:
        return None, "empty quote result"
    meta = results[0].get("meta") or {}
    price = (
        meta.get("regularMarketPrice")
        or meta.get("previousClose")
        or meta.get("chartPreviousClose")
    )
    ts = meta.get("regularMarketTime")
    if ts is None:
        timestamps = results[0].get("timestamp")
        if timestamps:
            ts = timestamps[-1]
    if price is None or ts is None:
        return None, "no price/timestamp in payload"
    try:
        quote = Quote(price=Decimal(str(price)), as_of=datetime.fromtimestamp(ts, tz=UTC))
    except (InvalidOperation, ValueError, OverflowError, OSError) as e:
        return None, f"unparseable price/timestamp: {str(e)[:120]}"

    _quote_cache[ticker] = (quote, clock.utcnow())
    return quote, None


def _apply_equity_quote(
    inst: Instrument,
    client: httpx.Client,
    base_url: str,
    warnings: list[str],
    ttl_hours: int = 4,
) -> str:
    """Price one equity from Yahoo (skip-when-not-newer).

    Returns updated / stale_skipped / fetch_error / unmatched. ``unmatched`` = no quote
    source for the exchange (config issue, won't change on retry); ``fetch_error`` =
    network / no-price (retry may help) — the split lets the user tell them apart.
    """
    ticker = _yahoo_ticker(inst.symbol, inst.exchange, inst.asset_class)
    if ticker is None:
        warnings.append(f"instrument {inst.id}: exchange {inst.exchange} has no quote source")
        return "unmatched"
    quote, cause = _fetch_equity_quote(client, base_url, ticker, ttl_hours=ttl_hours)
    if quote is None:
        warnings.append(f"instrument {inst.id}: no quote returned for {ticker} — {cause}")
        return "fetch_error"
    if not _source_is_newer(inst.nav_updated_at, quote.as_of.date()):
        return "stale_skipped"
    inst.current_nav = quote.price
    inst.nav_updated_at = quote.as_of.replace(tzinfo=None)
    return "updated"


def refresh_all_active_navs(
    session: Session,
    *,
    client: httpx.Client,
    amfi_url: str,
    yahoo_base_url: str,
    as_of: date,
    frankfurter_base_url: str | None = None,
    market_hours_only: bool = True,
    pacing_delay_min: float = 0.3,
    pacing_delay_max: float = 0.5,
    ttl_hours: int = 4,
) -> NavRefreshResult:
    """Batch-refresh NAVs for all active instruments across all users.

    Deduplicates tickers across users, observes market hours gating, respects quote
    TTL cache, and spaces requests politely.
    """
    if not _refresh_lock.acquire(blocking=False):
        logger.info("nav_refresh_already_in_progress")
        return NavRefreshResult(warnings=["Refresh already in progress."])

    try:
        if is_circuit_breaker_tripped():
            return NavRefreshResult(
                warnings=["Circuit breaker active due to upstream rate limits."]
            )

        instruments = list(
            session.scalars(select(Instrument).where(Instrument.archived_at.is_(None)))
        )
        warnings: list[str] = []
        mf_updated = equity_updated = unmatched = fetch_errors = stale_skipped = skipped = 0

        mf_instruments = [i for i in instruments if i.asset_class == _MF_CLASS]
        equity_instruments = [
            i
            for i in instruments
            if i.asset_class in _QUOTE_CLASSES or i.exchange in _EXCHANGE_SUFFIX
        ]
        other_instruments = [
            i for i in instruments if i not in mf_instruments and i not in equity_instruments
        ]
        skipped += len(other_instruments)

        # 1. Process Mutual Funds
        mf_index: MfIndex | None = None
        if mf_instruments:
            if market_hours_only and not is_amfi_nav_window_open():
                logger.info("amfi_market_hours_closed")
            else:
                mf_index, cause = _fetch_mf_index(client, amfi_url, warnings)
                if mf_index is None:
                    fetch_errors += 1
                    warnings.append(f"AMFI source unreachable — MF NAVs unchanged — {cause}")

            if mf_index is not None:
                for inst in mf_instruments:
                    outcome = _apply_mf_nav(inst, mf_index, warnings)
                    if outcome == "updated":
                        mf_updated += 1
                    elif outcome == "stale_skipped":
                        stale_skipped += 1
                    else:
                        unmatched += 1

        # 2. Process Equities / ETFs with Ticker Deduplication
        if equity_instruments:
            ticker_map: dict[str, list[Instrument]] = {}
            for inst in equity_instruments:
                ticker = _yahoo_ticker(inst.symbol, inst.exchange, inst.asset_class)
                if ticker is None:
                    unmatched += 1
                    warnings.append(
                        f"instrument {inst.id}: exchange {inst.exchange} has no quote source"
                    )
                    continue
                ticker_map.setdefault(ticker, []).append(inst)

            now_ist = clock.utcnow().astimezone(clock.BUSINESS_TZ)
            indian_open = is_indian_equity_market_open(now_ist)
            us_open = is_us_equity_market_open(now_ist)

            first_fetch = True
            for ticker, inst_list in ticker_map.items():
                is_indian = ticker.endswith((".NS", ".BO"))
                if market_hours_only:
                    if is_indian and not indian_open:
                        continue
                    if not is_indian and not us_open:
                        continue

                is_cached = ticker in _quote_cache and (
                    clock.utcnow() - _quote_cache[ticker][1]
                ) < timedelta(hours=ttl_hours)
                if not is_cached and not first_fetch and pacing_delay_max > 0:
                    time.sleep(random.uniform(pacing_delay_min, pacing_delay_max))

                quote, cause = _fetch_equity_quote(
                    client, yahoo_base_url, ticker, ttl_hours=ttl_hours
                )
                if not is_cached:
                    first_fetch = False

                if quote is None:
                    fetch_errors += len(inst_list)
                    warnings.append(f"no quote returned for {ticker} — {cause}")
                    continue

                for inst in inst_list:
                    if not _source_is_newer(inst.nav_updated_at, quote.as_of.date()):
                        stale_skipped += 1
                    else:
                        inst.current_nav = quote.price
                        inst.nav_updated_at = quote.as_of.replace(tzinfo=None)
                        equity_updated += 1

        # 3. Process Foreign FX
        if frankfurter_base_url and any(i.currency != "INR" for i in instruments):
            if is_fx_rate_present_for_date(session, as_of):
                logger.info("fx_rate_already_present", as_of=str(as_of))
            elif market_hours_only and clock.utcnow().astimezone(clock.BUSINESS_TZ).weekday() >= 5:
                logger.info("fx_rates_weekend_skipped")
            else:
                fx_res = refresh_fx_rates(session, client=client, base_url=frankfurter_base_url)
                warnings.extend(fx_res.warnings)
                fetch_errors += fx_res.fetch_errors

        null_nav_count = sum(1 for i in instruments if i.current_nav is None)
        catalogue_staleness_days = max_staleness_days(
            (i.nav_updated_at for i in instruments if i.current_nav is not None), as_of=as_of
        )

        result = NavRefreshResult(
            mf_updated=mf_updated,
            equity_updated=equity_updated,
            unmatched=unmatched,
            fetch_errors=fetch_errors,
            stale_skipped=stale_skipped,
            skipped=skipped,
            null_nav_count=null_nav_count,
            catalogue_staleness_days=catalogue_staleness_days,
            warnings=warnings,
        )
        logger.info(
            "nav_batch_refresh_completed",
            mf_updated=mf_updated,
            equity_updated=equity_updated,
            unmatched=unmatched,
            fetch_errors=fetch_errors,
            stale_skipped=stale_skipped,
            skipped=skipped,
            null_nav_count=null_nav_count,
            catalogue_staleness_days=catalogue_staleness_days,
        )
        return result
    finally:
        _refresh_lock.release()


def _source_is_newer(existing: datetime | None, source_date: date) -> bool:
    """True if the source NAV date is the same day or newer than the existing stamp.

    Date-granular so a same-day AMFI NAV (authoritative) overwrites a manual entry made
    earlier the same day; an older source date is skipped (never regress a fresher value).
    That day-truncation is the POLICY here, not a workaround — the ``.date()`` is also the
    mandatory ``datetime`` → ``date`` conversion, since the operands are different types.
    The datetime-portability rule this used to restate is owned by ADR-0001 rule 5 and
    :mod:`app.core.clock`; don't restate it per site.
    """
    if existing is None:
        return True
    return source_date >= existing.date()
