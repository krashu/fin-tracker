"""Service tests for the NAV/price snapshot (PRD §F7 — MF + equity paths).

Exercises ``refresh_navs`` against an in-memory session with the AMFI + Yahoo feeds
mocked via ``httpx.MockTransport`` (no network, no new dep). Covers: MF match by both
the growth and the reinvestment ISIN; back-filled ``amfi_code``; ``nav_updated_at`` set
to the *source* date; skip-when-not-newer (incl. same-day overwrite); ISIN-absent →
unmatched; dup-ISIN withheld and reported against the holding it affects (and silent when
nobody holds it); total AMFI-source failure (MFs untouched, one warning, no
mass-unmatched); equity priced from Yahoo (NSE/BSE suffixes); equity fetch-error vs
unquotable-exchange; user-entered classes skipped; staleness + null-NAV signals; a
no-advance re-run; and that both write paths stamp ``nav_updated_at`` **naive**.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from sqlalchemy.orm import Session

from app.models import Instrument, User
from app.services.nav_snapshot_service import refresh_navs, reset_nav_state


@pytest.fixture(autouse=True)
def _clean_nav_state() -> Iterator[None]:
    reset_nav_state()
    yield
    reset_nav_state()


_AMFI_URL = "https://amfi.test/NAVAll.txt"
_NAVALL_BODY = (
    Path(__file__).parent.parent / "fixtures" / "amfi_navall" / "navall_sample.txt"
).read_bytes()
# From the fixture: scheme 119551 — growth INF209KA12Z1 / reinvest INF209KA13Z9, NAV 105.9219.
_GROWTH_ISIN = "INF209KA12Z1"
_REINVEST_ISIN = "INF209KA13Z9"
_NAV_119551 = Decimal("105.9219")
_NAV_DATE = date(2026, 6, 20)
_AS_OF = date(2026, 6, 25)


def _amfi_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=_NAVALL_BODY)


def _amfi_down(request: httpx.Request) -> httpx.Response:
    return httpx.Response(503)


_HEADER = (
    "Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;"
    "Scheme Name;Net Asset Value;Date"
)


def _amfi_serving(body: str):  # type: ignore[no-untyped-def]
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body.encode("utf-8"))

    return handler


def _client(handler) -> httpx.Client:  # type: ignore[no-untyped-def]
    return httpx.Client(transport=httpx.MockTransport(handler))


def _mf(session: Session, user_id: UUID, symbol: str, **kw) -> Instrument:  # type: ignore[no-untyped-def]
    inst = Instrument(
        user_id=user_id,
        symbol=symbol,
        name=symbol,
        asset_class="indian_mf",
        currency="INR",
        exchange="MFCentral",
        **kw,
    )
    session.add(inst)
    session.flush()
    return inst


_YAHOO_URL = "https://yahoo.test/v8/finance/chart"
_YAHOO_PRICE = Decimal("1850.5")
_YAHOO_DT = datetime(2026, 6, 24, 10, 0, tzinfo=UTC)
_YAHOO_TS = int(_YAHOO_DT.timestamp())


def _yahoo_body(price: float = 1850.5, ts: int = _YAHOO_TS) -> dict[str, object]:
    return {
        "chart": {
            "result": [{"meta": {"regularMarketPrice": price, "regularMarketTime": ts}}],
            "error": None,
        }
    }


def _equity_ok(request: httpx.Request) -> httpx.Response:
    if str(request.url).startswith(_AMFI_URL):
        return httpx.Response(200, content=_NAVALL_BODY)
    return httpx.Response(200, json=_yahoo_body())


def _equity_404(request: httpx.Request) -> httpx.Response:
    if str(request.url).startswith(_AMFI_URL):
        return httpx.Response(200, content=_NAVALL_BODY)
    return httpx.Response(404)


def _equity(  # type: ignore[no-untyped-def]
    session: Session, user_id: UUID, symbol: str, exchange: str = "NSE", **kw
) -> Instrument:
    asset_class = kw.pop("asset_class", "indian_equity")
    currency = kw.pop("currency", "INR")
    inst = Instrument(
        user_id=user_id,
        symbol=symbol,
        name=symbol,
        asset_class=asset_class,
        currency=currency,
        exchange=exchange,
        **kw,
    )
    session.add(inst)
    session.flush()
    return inst


def _run(session: Session, user_id: UUID, handler, as_of: date = _AS_OF):  # type: ignore[no-untyped-def]
    with _client(handler) as client:
        return refresh_navs(
            session,
            user_id=user_id,
            client=client,
            amfi_url=_AMFI_URL,
            yahoo_base_url=_YAHOO_URL,
            as_of=as_of,
        )


def test_mf_priced_by_growth_isin(session: Session, user: User) -> None:
    inst = _mf(session, user.id, "ABSLBPSU", isin=_GROWTH_ISIN)
    result = _run(session, user.id, _amfi_ok)

    assert result.mf_updated == 1
    assert result.unmatched == 0
    assert inst.current_nav == _NAV_119551
    assert inst.amfi_code == "119551"  # back-filled
    assert inst.nav_updated_at is not None
    assert inst.nav_updated_at.date() == _NAV_DATE  # source date, not today
    assert result.catalogue_staleness_days == (_AS_OF - _NAV_DATE).days == 5


def test_mf_priced_by_reinvest_isin(session: Session, user: User) -> None:
    # A holding carrying the reinvestment ISIN must still match scheme 119551.
    inst = _mf(session, user.id, "ABSLBPSU-R", isin=_REINVEST_ISIN)
    result = _run(session, user.id, _amfi_ok)
    assert result.mf_updated == 1
    assert inst.current_nav == _NAV_119551


def test_isin_absent_from_file_is_unmatched(session: Session, user: User) -> None:
    inst = _mf(session, user.id, "GHOST", isin="INF000000099")
    result = _run(session, user.id, _amfi_ok)
    assert result.unmatched == 1
    assert result.mf_updated == 0
    assert inst.current_nav is None
    assert result.null_nav_count == 1


def test_mf_without_isin_is_unmatched(session: Session, user: User) -> None:
    inst = _mf(session, user.id, "NOISIN", isin=None)
    result = _run(session, user.id, _amfi_ok)
    assert result.unmatched == 1
    assert inst.current_nav is None
    assert any("no ISIN" in w for w in result.warnings)


def test_skip_when_existing_is_newer(session: Session, user: User) -> None:
    # Manual NAV stamped AFTER the source date → snapshot must not regress it.
    inst = _mf(
        session,
        user.id,
        "ABSLBPSU",
        isin=_GROWTH_ISIN,
        current_nav=Decimal("999.0000"),
        nav_updated_at=datetime(2026, 6, 24, tzinfo=UTC),  # newer than 20-Jun source
    )
    result = _run(session, user.id, _amfi_ok)
    assert result.stale_skipped == 1
    assert result.mf_updated == 0
    assert inst.current_nav == Decimal("999.0000")  # kept
    assert inst.nav_updated_at == datetime(2026, 6, 24, tzinfo=UTC)


def test_same_day_source_overwrites_with_new_value(session: Session, user: User) -> None:
    # Existing stamp is the SAME day as the source but a different value (AMFI corrected
    # today's NAV, or a same-day manual guess) → AMFI is authoritative, overwrite (>= rule).
    inst = _mf(
        session,
        user.id,
        "ABSLBPSU",
        isin=_GROWTH_ISIN,
        current_nav=Decimal("100.0000"),
        nav_updated_at=datetime(_NAV_DATE.year, _NAV_DATE.month, _NAV_DATE.day, tzinfo=UTC),
    )
    result = _run(session, user.id, _amfi_ok)
    assert result.mf_updated == 1
    assert result.stale_skipped == 0
    assert inst.current_nav == _NAV_119551  # overwritten with AMFI's value


def test_amfi_source_failure_leaves_mfs_untouched(session: Session, user: User) -> None:
    inst = _mf(session, user.id, "ABSLBPSU", isin=_GROWTH_ISIN)
    result = _run(session, user.id, _amfi_down)
    assert result.fetch_errors == 1
    assert result.mf_updated == 0
    assert result.unmatched == 0  # NOT mass-unmatched — it's a source failure
    assert inst.current_nav is None
    # Warning carries the real cause (503), not just the generic phrase.
    assert any("AMFI source unreachable" in w and "503" in w for w in result.warnings)


def test_amfi_code_kept_when_already_set(session: Session, user: User) -> None:
    inst = _mf(session, user.id, "ABSLBPSU", isin=_GROWTH_ISIN, amfi_code="OLD")
    result = _run(session, user.id, _amfi_ok)
    assert inst.amfi_code == "OLD"  # not clobbered
    assert any("amfi_code disagrees" in w for w in result.warnings)


def test_user_entered_classes_are_skipped(session: Session, user: User) -> None:
    # fd / bond / nps / gold / other have no auto source → skipped (us_equity/us_etf now
    # route to Yahoo, see test_us_equity_priced_from_yahoo).
    session.add(
        Instrument(
            user_id=user.id,
            symbol="SGB2031",
            name="Sovereign Gold Bond",
            asset_class="gold",
            currency="INR",
            exchange="OTHER",
        )
    )
    session.flush()
    result = _run(session, user.id, _amfi_ok)
    assert result.skipped == 1
    assert result.mf_updated == 0


def test_us_equity_priced_from_yahoo(session: Session, user: User) -> None:
    # us_equity / us_etf now route through Yahoo (currency was the only blocker). The USD
    # price is stored as-is in current_nav (native); INR conversion happens at read time.
    aapl = Instrument(
        user_id=user.id,
        symbol="AAPL",
        name="Apple",
        asset_class="us_equity",
        currency="USD",
        exchange="NASDAQ",
    )
    voo = Instrument(
        user_id=user.id,
        symbol="VOO",
        name="Vanguard S&P 500",
        asset_class="us_etf",
        currency="USD",
        exchange="NYSE",
    )
    session.add_all([aapl, voo])
    session.flush()

    result = _run(session, user.id, _equity_ok)

    assert result.equity_updated == 2
    assert result.skipped == 0
    assert aapl.current_nav == _YAHOO_PRICE  # stored in native USD, unconverted
    assert voo.current_nav == _YAHOO_PRICE


def test_us_equity_non_us_exchange_priced_from_yahoo(session: Session, user: User) -> None:
    # US equities/ETFs with exchange="OTHER" (e.g. from CSV import) or "MFCentral"
    # (default in Add dialog) still price from Yahoo using the bare ticker.
    spy = Instrument(
        user_id=user.id,
        symbol="SPY",
        name="SPDR S&P 500 ETF",
        asset_class="us_etf",
        currency="USD",
        exchange="OTHER",
    )
    msft = Instrument(
        user_id=user.id,
        symbol="MSFT",
        name="Microsoft",
        asset_class="us_equity",
        currency="USD",
        exchange="MFCentral",
    )
    session.add_all([spy, msft])
    session.flush()

    result = _run(session, user.id, _equity_ok)

    assert result.equity_updated == 2
    assert result.unmatched == 0
    assert spy.current_nav == _YAHOO_PRICE
    assert msft.current_nav == _YAHOO_PRICE


def test_duplicate_isin_differing_nav_is_dropped(session: Session, user: User) -> None:
    # Same ISIN in two schemes with different NAVs → ambiguous → withheld (no coin-flip);
    # a holding matching it lands unmatched, not coin-flip-priced. The warning is scoped
    # to the affected instrument and says *ambiguous*, not "not found" — the ISIN is in
    # NAVAll, and conflating the two sends the user hunting a typo that isn't there.
    body = (
        "\n".join(
            [
                _HEADER,
                "119551;INFDUP0000001;-;Fund A;100.0000;20-Jun-2026",
                "119552;INFDUP0000001;-;Fund B;200.0000;20-Jun-2026",
            ]
        )
        + "\n"
    )
    inst = _mf(session, user.id, "DUP", isin="INFDUP0000001")
    result = _run(session, user.id, _amfi_serving(body))
    assert result.unmatched == 1
    assert inst.current_nav is None
    assert [w for w in result.warnings if "multiple AMFI schemes" in w] == [
        f"{inst.name}: ISIN INFDUP0000001 maps to multiple AMFI schemes "
        f"with differing NAV — not priced"
    ]
    assert not any("not found" in w for w in result.warnings)


def test_ambiguous_isin_nobody_holds_is_silent(session: Session, user: User) -> None:
    """An AMFI catalogue glitch in a scheme the user does not hold must not be reported.

    Real NAVAll carries a handful of these permanently (including a literal ``REDEEMED``
    in the ISIN column), so warning per glitch rather than per affected holding put ~5
    lines nobody owned into every refresh panel, where they read as portfolio errors.
    The held fund still prices normally in the same pass.
    """
    body = (
        "\n".join(
            [
                _HEADER,
                "119551;INFDUP0000001;-;Fund A;100.0000;20-Jun-2026",
                "119552;INFDUP0000001;-;Fund B;200.0000;20-Jun-2026",
                "119553;INFHELD000001;-;Held Fund;50.0000;20-Jun-2026",
            ]
        )
        + "\n"
    )
    inst = _mf(session, user.id, "HELD", isin="INFHELD000001")
    result = _run(session, user.id, _amfi_serving(body))

    assert result.mf_updated == 1
    assert result.unmatched == 0
    assert inst.current_nav == Decimal("50.0000")
    assert not any("INFDUP0000001" in w for w in result.warnings)
    assert not any("multiple AMFI schemes" in w for w in result.warnings)


def test_equity_priced_from_yahoo(session: Session, user: User) -> None:
    inst = _equity(session, user.id, "INFY", exchange="NSE")
    result = _run(session, user.id, _equity_ok)
    assert result.equity_updated == 1
    assert inst.current_nav == _YAHOO_PRICE
    # The market time, stored naive-UTC (ADR-0001 rule 5) — same instant, offset dropped.
    assert inst.nav_updated_at == _YAHOO_DT.replace(tzinfo=None)


def test_equity_bse_uses_bo_suffix(session: Session, user: User) -> None:
    inst = _equity(session, user.id, "RELIANCE", exchange="BSE")
    result = _run(session, user.id, _equity_ok)
    assert result.equity_updated == 1
    assert inst.current_nav == _YAHOO_PRICE


def test_equity_fetch_failure_is_fetch_error(session: Session, user: User) -> None:
    inst = _equity(session, user.id, "INFY", exchange="NSE")
    result = _run(session, user.id, _equity_404)
    assert result.fetch_errors == 1
    assert result.equity_updated == 0
    assert inst.current_nav is None  # left null, distinct from unmatched
    # Warning names the ticker AND the real cause (the 404), not just "no quote returned".
    assert any("INFY.NS" in w and "404" in w for w in result.warnings)


def test_equity_missing_price_reports_payload_cause(session: Session, user: User) -> None:
    # A 200 with a result but no usable price is a non-exception None path — its cause is a
    # descriptive string (there's no exception to stringify), still surfaced in the warning.
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(_AMFI_URL):
            return httpx.Response(200, content=_NAVALL_BODY)
        return httpx.Response(200, json={"chart": {"result": [{"meta": {}}], "error": None}})

    inst = _equity(session, user.id, "INFY", exchange="NSE")
    result = _run(session, user.id, handler)
    assert result.fetch_errors == 1
    assert inst.current_nav is None
    assert any("no price/timestamp in payload" in w for w in result.warnings)


def test_equity_unquotable_exchange_is_unmatched(session: Session, user: User) -> None:
    inst = _equity(session, user.id, "WEIRD", exchange="OTHER")
    result = _run(session, user.id, _equity_ok)
    assert result.unmatched == 1
    assert inst.current_nav is None
    assert any("no quote source" in w for w in result.warnings)


def test_yahoo_ticker_mapping() -> None:
    from app.services.nav_snapshot_service import _yahoo_ticker

    assert _yahoo_ticker("INFY", "NSE") == "INFY.NS"
    assert _yahoo_ticker("RELIANCE", "BSE") == "RELIANCE.BO"
    assert _yahoo_ticker("X", "OTHER") is None
    assert _yahoo_ticker("AAPL", "NASDAQ", "us_equity") == "AAPL"
    assert _yahoo_ticker("VOO", "OTHER", "us_etf") == "VOO"
    assert _yahoo_ticker("AAPL", "MFCentral", "us_equity") == "AAPL"
    assert _yahoo_ticker("BRK.B", "NYSE", "us_equity") == "BRK-B"


def test_rerun_does_not_advance_nav_updated_at(session: Session, user: User) -> None:
    inst = _mf(session, user.id, "ABSLBPSU", isin=_GROWTH_ISIN)
    _run(session, user.id, _amfi_ok)
    first = inst.nav_updated_at
    _run(session, user.id, _amfi_ok)  # same source date
    assert inst.nav_updated_at == first  # stays the source NAV date, never "now"


def test_both_write_paths_stamp_nav_updated_at_naive(session: Session, user: User) -> None:
    """ADR-0001 rule 5: ``nav_updated_at`` is naive UTC whichever source wrote it.

    Asserted **in memory**, never after a readback, for the reason
    ``test_datetime_boundary`` documents: SQLite strips the offset on the way in, so once
    the value has been through the DB an aware write and a naive one are indistinguishable
    and this test would pass no matter what the service assigned.

    The MF path is the acute one — it stamps midnight, so an aware bind assignment-cast
    through a negative-offset Postgres ``TimeZone`` lands the valuation on the *previous
    day*. The equity path stamps a real market time, where the same cast shifts by hours.
    """
    mf = _mf(session, user.id, "ABSLBPSU", isin=_GROWTH_ISIN)
    equity = _equity(session, user.id, "INFY", exchange="NSE")
    _run(session, user.id, _equity_ok)

    assert mf.nav_updated_at is not None and mf.nav_updated_at.tzinfo is None
    assert equity.nav_updated_at is not None and equity.nav_updated_at.tzinfo is None
    # ...and nothing moved: the offset was dropped, not applied.
    assert mf.nav_updated_at == datetime(_NAV_DATE.year, _NAV_DATE.month, _NAV_DATE.day)
    assert equity.nav_updated_at == _YAHOO_DT.replace(tzinfo=None)


def test_gold_etf_on_nse_priced_from_yahoo(session: Session, user: User) -> None:
    # GOLDBEES (asset_class="gold", exchange="NSE") prices via Yahoo quotes (GOLDBEES.NS)
    # rather than being skipped as hand-priced.
    gold_etf = Instrument(
        user_id=user.id,
        symbol="GOLDBEES",
        name="Nippon India Gold BeES",
        asset_class="gold",
        currency="INR",
        exchange="NSE",
    )
    session.add(gold_etf)
    session.flush()

    result = _run(session, user.id, _equity_ok)
    assert result.equity_updated == 1
    assert result.skipped == 0
    assert gold_etf.current_nav == _YAHOO_PRICE
    assert gold_etf.nav_updated_at == _YAHOO_DT.replace(tzinfo=None)


def test_physical_gold_on_other_exchange_is_skipped(session: Session, user: User) -> None:
    # Gold held off-exchange (exchange="OTHER") has no quote feed and is skipped.
    gold_bar = Instrument(
        user_id=user.id,
        symbol="GOLDBAR",
        name="Physical Gold 24K",
        asset_class="gold",
        currency="INR",
        exchange="OTHER",
    )
    session.add(gold_bar)
    session.flush()

    result = _run(session, user.id, _equity_ok)
    assert result.skipped == 1
    assert result.equity_updated == 0
    assert gold_bar.current_nav is None


def test_refresh_navs_with_frankfurter_refreshes_fx_rates(session: Session, user: User) -> None:
    from sqlalchemy import select

    from app.models import FxRateQuote

    aapl = Instrument(
        user_id=user.id,
        symbol="AAPL",
        name="Apple Inc",
        asset_class="us_equity",
        currency="USD",
        exchange="NASDAQ",
    )
    session.add(aapl)
    session.flush()

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if url_str.startswith(_AMFI_URL):
            return httpx.Response(200, content=_NAVALL_BODY)
        if "frankfurter.test" in url_str:
            return httpx.Response(
                200,
                json={
                    "amount": 1.0,
                    "base": "USD",
                    "date": "2026-06-25",
                    "rates": {"INR": 89.5},
                },
            )
        return httpx.Response(200, json=_yahoo_body())

    with _client(handler) as client:
        result = refresh_navs(
            session,
            user_id=user.id,
            client=client,
            amfi_url=_AMFI_URL,
            yahoo_base_url=_YAHOO_URL,
            as_of=_AS_OF,
            frankfurter_base_url="https://frankfurter.test",
        )

    assert result.equity_updated == 1
    rate_row = session.scalar(
        select(FxRateQuote).where(
            FxRateQuote.from_currency == "USD",
            FxRateQuote.to_currency == "INR",
            FxRateQuote.date == date(2026, 6, 25),
        )
    )
    assert rate_row is not None
    assert rate_row.rate == Decimal("89.5")


def test_market_hours_helpers() -> None:
    from datetime import datetime

    from app.core.clock import BUSINESS_TZ
    from app.services.nav_snapshot_service import (
        is_amfi_nav_window_open,
        is_indian_equity_market_open,
        is_us_equity_market_open,
    )

    # 1. Indian equity: Mon 11:30 AM IST (open) vs Mon 08:30 AM IST (closed)
    # vs Sat 11:30 AM (closed)
    mon_open = datetime(2026, 6, 22, 11, 30, tzinfo=BUSINESS_TZ)  # Monday
    mon_early = datetime(2026, 6, 22, 8, 30, tzinfo=BUSINESS_TZ)
    sat_open = datetime(2026, 6, 27, 11, 30, tzinfo=BUSINESS_TZ)  # Saturday
    assert is_indian_equity_market_open(mon_open) is True
    assert is_indian_equity_market_open(mon_early) is False
    assert is_indian_equity_market_open(sat_open) is False

    # 2. US equity: Mon 20:30 IST (open), Tue 01:30 IST (open), Sat 01:30 IST (open),
    # Sun 20:30 (closed)
    mon_night = datetime(2026, 6, 22, 20, 30, tzinfo=BUSINESS_TZ)  # Monday night
    tue_early = datetime(2026, 6, 23, 1, 30, tzinfo=BUSINESS_TZ)  # Tuesday morning
    sat_early = datetime(2026, 6, 27, 1, 30, tzinfo=BUSINESS_TZ)  # Saturday 01:30 IST
    sun_night = datetime(2026, 6, 28, 20, 30, tzinfo=BUSINESS_TZ)  # Sunday night
    tue_noon = datetime(2026, 6, 23, 12, 0, tzinfo=BUSINESS_TZ)  # Tuesday noon
    assert is_us_equity_market_open(mon_night) is True
    assert is_us_equity_market_open(tue_early) is True
    assert is_us_equity_market_open(sat_early) is True
    assert is_us_equity_market_open(sun_night) is False
    assert is_us_equity_market_open(tue_noon) is False

    # 3. AMFI NAV window: Mon 21:30 IST (open) vs Mon 15:00 IST (closed) vs Sat 21:30 IST (closed)
    mon_amfi_open = datetime(2026, 6, 22, 21, 30, tzinfo=BUSINESS_TZ)
    mon_amfi_closed = datetime(2026, 6, 22, 15, 0, tzinfo=BUSINESS_TZ)
    sat_amfi_closed = datetime(2026, 6, 27, 21, 30, tzinfo=BUSINESS_TZ)
    assert is_amfi_nav_window_open(mon_amfi_open) is True
    assert is_amfi_nav_window_open(mon_amfi_closed) is False
    assert is_amfi_nav_window_open(sat_amfi_closed) is False


def test_ticker_deduplication_across_users(session: Session, user: User) -> None:
    import uuid

    from app.services.nav_snapshot_service import refresh_all_active_navs

    # Create second user
    user2 = User(id=uuid.uuid4(), email="user2@test.com")
    session.add(user2)
    session.flush()

    # User 1 holds AAPL, User 2 also holds AAPL
    aapl1 = Instrument(
        user_id=user.id,
        symbol="AAPL",
        name="Apple Inc",
        asset_class="us_equity",
        currency="USD",
        exchange="NASDAQ",
    )
    aapl2 = Instrument(
        user_id=user2.id,
        symbol="AAPL",
        name="Apple Inc",
        asset_class="us_equity",
        currency="USD",
        exchange="NASDAQ",
    )
    session.add_all([aapl1, aapl2])
    session.flush()

    yahoo_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal yahoo_calls
        if "/AAPL" in str(request.url):
            yahoo_calls += 1
            return httpx.Response(200, json=_yahoo_body(price=195.0))
        return httpx.Response(404)

    with _client(handler) as client:
        result = refresh_all_active_navs(
            session,
            client=client,
            amfi_url=_AMFI_URL,
            yahoo_base_url=_YAHOO_URL,
            as_of=_AS_OF,
            market_hours_only=False,
            pacing_delay_max=0,
        )

    # AAPL should be fetched only ONCE despite two users holding it
    assert yahoo_calls == 1
    assert result.equity_updated == 2
    assert aapl1.current_nav == Decimal("195.0")
    assert aapl2.current_nav == Decimal("195.0")


def test_quote_ttl_cache(session: Session, user: User) -> None:
    _equity(session, user.id, "INFY", exchange="NSE")
    yahoo_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal yahoo_calls
        yahoo_calls += 1
        return httpx.Response(200, json=_yahoo_body(price=1600.0))

    with _client(handler) as client:
        # First call fetches from network
        res1 = refresh_navs(
            session,
            user_id=user.id,
            client=client,
            amfi_url=_AMFI_URL,
            yahoo_base_url=_YAHOO_URL,
            as_of=_AS_OF,
            ttl_hours=4,
        )
        assert res1.equity_updated == 1
        assert yahoo_calls == 1

        # Second call within TTL reuses cached quote without network call
        res2 = refresh_navs(
            session,
            user_id=user.id,
            client=client,
            amfi_url=_AMFI_URL,
            yahoo_base_url=_YAHOO_URL,
            as_of=_AS_OF,
            ttl_hours=4,
        )
        assert res2.equity_updated == 1
        assert yahoo_calls == 1


def test_frankfurter_db_short_circuit(session: Session, user: User) -> None:
    from app.models import FxRateQuote

    # Pre-seed today's USD->INR rate in the database
    today_rate = FxRateQuote(
        from_currency="USD",
        to_currency="INR",
        date=_AS_OF,
        rate=Decimal("89.75"),
        source="frankfurter",
    )
    session.add(today_rate)

    _equity(session, user.id, "AAPL", exchange="NASDAQ", currency="USD", asset_class="us_equity")
    session.flush()

    frankfurter_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal frankfurter_calls
        if "frankfurter" in str(request.url):
            frankfurter_calls += 1
            return httpx.Response(200, json={"rates": {"INR": 90.0}})
        return httpx.Response(200, json=_yahoo_body())

    with _client(handler) as client:
        result = refresh_navs(
            session,
            user_id=user.id,
            client=client,
            amfi_url=_AMFI_URL,
            yahoo_base_url=_YAHOO_URL,
            as_of=_AS_OF,
            frankfurter_base_url="https://frankfurter.test",
        )

    assert result.equity_updated == 1
    # Frankfurter network endpoint was short-circuited because DB already had today's rate
    assert frankfurter_calls == 0


def test_circuit_breaker_on_429(session: Session, user: User) -> None:
    from app.services.nav_snapshot_service import is_circuit_breaker_tripped

    _equity(session, user.id, "MSFT", exchange="NASDAQ", currency="USD", asset_class="us_equity")

    network_calls = 0

    def rate_limited_handler(request: httpx.Request) -> httpx.Response:
        nonlocal network_calls
        network_calls += 1
        return httpx.Response(429, json={"error": "Too Many Requests"})

    with _client(rate_limited_handler) as client:
        res1 = refresh_navs(
            session,
            user_id=user.id,
            client=client,
            amfi_url=_AMFI_URL,
            yahoo_base_url=_YAHOO_URL,
            as_of=_AS_OF,
        )
        assert res1.fetch_errors == 1
        assert is_circuit_breaker_tripped() is True
        assert network_calls == 1

        # Subsequent call should immediately short-circuit without network attempt
        res2 = refresh_navs(
            session,
            user_id=user.id,
            client=client,
            amfi_url=_AMFI_URL,
            yahoo_base_url=_YAHOO_URL,
            as_of=_AS_OF,
        )
        assert "Circuit breaker active" in res2.warnings[0]
        assert network_calls == 1


def test_cooldown_debounce(session: Session, user: User) -> None:
    _equity(session, user.id, "RELIANCE", exchange="NSE")

    with _client(lambda req: httpx.Response(200, json=_yahoo_body())) as client:
        res1 = refresh_navs(
            session,
            user_id=user.id,
            client=client,
            amfi_url=_AMFI_URL,
            yahoo_base_url=_YAHOO_URL,
            as_of=_AS_OF,
            check_cooldown=True,
            cooldown_minutes=5,
        )
        assert res1.equity_updated == 1
        assert res1.warnings == []

        # Rapid second call within 5 minutes returns cooldown advisory
        res2 = refresh_navs(
            session,
            user_id=user.id,
            client=client,
            amfi_url=_AMFI_URL,
            yahoo_base_url=_YAHOO_URL,
            as_of=_AS_OF,
            check_cooldown=True,
            cooldown_minutes=5,
        )
        assert res2.equity_updated == 0
        assert "Cooldown active" in res2.warnings[0]


def test_refresh_all_active_navs_full(session: Session, user: User) -> None:
    from app.services.nav_snapshot_service import refresh_all_active_navs

    # Add MF, Indian equity, US equity, and unpriced gold
    mf = _mf(session, user.id, _GROWTH_ISIN, isin=_GROWTH_ISIN)
    eq_in = _equity(session, user.id, "TCS", exchange="NSE")
    eq_us = Instrument(
        user_id=user.id,
        symbol="GOOGL",
        name="Alphabet Inc",
        asset_class="us_equity",
        currency="USD",
        exchange="NASDAQ",
    )
    gold = Instrument(
        user_id=user.id,
        symbol="GOLD",
        name="Physical Gold",
        asset_class="gold",
        currency="INR",
        exchange="OTHER",
    )
    session.add_all([eq_us, gold])
    session.flush()

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if url_str.startswith(_AMFI_URL):
            return httpx.Response(200, content=_NAVALL_BODY)
        if "frankfurter.test" in url_str:
            return httpx.Response(
                200,
                json={
                    "amount": 1.0,
                    "base": "USD",
                    "date": "2026-06-25",
                    "rates": {"INR": 89.5},
                },
            )
        return httpx.Response(200, json=_yahoo_body(price=2500.0))

    with _client(handler) as client:
        result = refresh_all_active_navs(
            session,
            client=client,
            amfi_url=_AMFI_URL,
            yahoo_base_url=_YAHOO_URL,
            as_of=_AS_OF,
            frankfurter_base_url="https://frankfurter.test",
            market_hours_only=False,
            pacing_delay_max=0,
        )

    assert result.mf_updated == 1
    assert result.equity_updated == 2  # TCS + GOOGL
    assert result.skipped == 1  # gold
    assert mf.current_nav == _NAV_119551
    assert eq_in.current_nav == Decimal("2500.0")
    assert eq_us.current_nav == Decimal("2500.0")
