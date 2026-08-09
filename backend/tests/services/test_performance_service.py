"""Service tests for the portfolio-vs-benchmark scalar alpha (PRD §F8 view 5).

The load-bearing money math. ``benchmark_nav`` is hand-seeded directly (NOT via the
mfapi refresh) so the golden-number assertion is independent of fixture drift. Covers:
the golden 1-year counterfactual (alpha within 0.1%, PRD §Verification §4); forward
pricing on a gap date; the sign trap (buy adds units, sell removes); cache-stale clamp;
pre-inception clamp + partial; as_of-before-inception; no benchmark data; no portfolio
cashflows; the net-negative-units guard; staleness flagged-not-suppressed; multi-asset;
a hand-priced holding reporting its real valuation age; the held-set vs whole-catalogue
staleness numbers differing on purpose, under two names; and the Friday→Monday /
Friday→Tuesday calendar arithmetic behind ``STALENESS_WARN_DAYS``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import httpx
from sqlalchemy.orm import Session

from app.models import (
    Benchmark,
    BenchmarkNav,
    FxRateQuote,
    Instrument,
    InvestmentTransaction,
    User,
)
from app.models.instrument import AssetClassStr
from app.models.investment_transaction import InvestmentTxnTypeStr
from app.schemas.performance import STALENESS_WARN_DAYS
from app.services.nav_snapshot_service import as_valuation_stamp, refresh_navs
from app.services.performance_service import compute_portfolio_performance

_BUY_ON = date(2025, 6, 19)
_AS_OF = date(2026, 6, 19)  # exactly 365 days after _BUY_ON


def _instrument(
    session: Session,
    user_id: UUID,
    *,
    symbol: str = "INF001",
    asset_class: AssetClassStr = "indian_mf",
    current_nav: Decimal | None = None,
    nav_updated_at: datetime | None = None,
) -> Instrument:
    inst = Instrument(
        user_id=user_id,
        symbol=symbol,
        name=f"{symbol} Fund",
        asset_class=asset_class,
        currency="INR",
        exchange="MFCentral",
        current_nav=current_nav,
        nav_updated_at=nav_updated_at,
    )
    session.add(inst)
    session.flush()
    return inst


def _txn(
    session: Session,
    user_id: UUID,
    instrument_id: int,
    *,
    txn_type: InvestmentTxnTypeStr,
    units: Decimal = Decimal("0"),
    amount_paise: int = 0,
    price: Decimal | None = None,
    on: date = _BUY_ON,
) -> InvestmentTransaction:
    txn = InvestmentTransaction(
        user_id=user_id,
        instrument_id=instrument_id,
        date=on,
        transaction_type=txn_type,
        units=units,
        price_per_unit_native=price,
        amount_native_paise=amount_paise,
        fees_native_paise=0,
        fx_rate_to_inr=Decimal("1"),
    )
    session.add(txn)
    session.flush()
    return txn


def _benchmark(session: Session, *, amfi_code: str = "100", name: str = "Nifty 50") -> Benchmark:
    b = Benchmark(name=name, kind="index_fund", amfi_code=amfi_code, currency="INR")
    session.add(b)
    session.flush()
    return b


def _nav(session: Session, benchmark_id: int, on: date, nav: str) -> None:
    session.add(BenchmarkNav(benchmark_id=benchmark_id, nav_date=on, nav=Decimal(nav)))
    session.flush()


def test_golden_alpha_one_year(session: Session, user: User) -> None:
    """Buy ₹1000 → worth ₹1500 (portfolio +50%); the index doubled (+100%) over the
    same year → alpha ≈ −0.5, within the PRD §Verification §4 0.1% bar."""
    inst = _instrument(session, user.id, current_nav=Decimal("150"))
    _txn(
        session,
        user.id,
        inst.id,
        txn_type="buy",
        units=Decimal("10"),
        amount_paise=100_000,
        price=Decimal("100"),
        on=_BUY_ON,
    )
    b = _benchmark(session)
    _nav(session, b.id, _BUY_ON, "100")
    _nav(session, b.id, _AS_OF, "200")  # doubled over the year

    perf = compute_portfolio_performance(session, user_id=user.id, benchmark_id=b.id, as_of=_AS_OF)

    assert perf.portfolio_xirr is not None and abs(perf.portfolio_xirr - 0.5) < 0.001
    assert perf.benchmark_xirr is not None and abs(perf.benchmark_xirr - 1.0) < 0.001
    assert perf.alpha is not None and abs(perf.alpha - (-0.5)) < 0.001
    assert perf.portfolio_value_paise == 150_000
    assert perf.benchmark_value_paise == 200_000  # 10 units × NAV 200 × 100
    assert perf.value_gap_paise == -50_000
    assert perf.benchmark_unavailable_reason is None
    assert perf.partial is False
    assert perf.benchmark_cache_stale is False
    assert perf.is_multi_asset is False
    assert perf.is_fund_post_ter is True


def test_multi_cashflow_benchmark_xirr_pinned(session: Session, user: User) -> None:
    """Two buys a year apart into a benchmark compounding at a constant +100%/yr →
    benchmark money-weighted XIRR = 1.0 EXACTLY, independent of flow timing (every rupee
    compounds at the same rate). Pins the XIRR *vector*, not just terminal units, so an
    intermediate per-flow mis-pricing (right terminal, wrong NAV index) can't pass silently.

    Hand check at r=1.0: ₹1000·(1+r)^2 + ₹1000·(1+r) = 4000 + 2000 = ₹6000 = terminal."""
    on1, on2 = date(2024, 6, 19), date(2025, 6, 19)
    as_of = date(2026, 6, 19)
    inst = _instrument(session, user.id, current_nav=Decimal("200"))
    _txn(
        session,
        user.id,
        inst.id,
        txn_type="buy",
        units=Decimal("10"),
        amount_paise=100_000,
        price=Decimal("100"),
        on=on1,
    )  # at NAV 100 → 10 benchmark units
    _txn(
        session,
        user.id,
        inst.id,
        txn_type="buy",
        units=Decimal("5"),
        amount_paise=100_000,
        price=Decimal("200"),
        on=on2,
    )  # at NAV 200 → 5 benchmark units
    b = _benchmark(session)
    _nav(session, b.id, on1, "100")
    _nav(session, b.id, on2, "200")  # ×2 over year 1
    _nav(session, b.id, as_of, "400")  # ×2 over year 2 → constant 100%/yr CAGR

    perf = compute_portfolio_performance(session, user_id=user.id, benchmark_id=b.id, as_of=as_of)

    assert perf.benchmark_value_paise == 600_000  # (10 + 5) units × NAV 400 × 100
    assert perf.benchmark_xirr is not None and abs(perf.benchmark_xirr - 1.0) < 0.001


def test_forward_pricing_uses_next_available_nav(session: Session, user: User) -> None:
    """A buy on a gap date prices at the NEXT NAV (≥ date), not the prior close."""
    inst = _instrument(session, user.id, current_nav=Decimal("200"))
    _txn(
        session,
        user.id,
        inst.id,
        txn_type="buy",
        units=Decimal("5"),
        amount_paise=100_000,
        price=Decimal("200"),
        on=date(2025, 6, 21),
    )  # gap day
    b = _benchmark(session)
    _nav(session, b.id, date(2025, 6, 20), "100")  # prior — must NOT be used
    _nav(session, b.id, date(2025, 6, 23), "200")  # next — used (forward pricing)
    _nav(session, b.id, _AS_OF, "200")

    perf = compute_portfolio_performance(session, user_id=user.id, benchmark_id=b.id, as_of=_AS_OF)

    # Next NAV 200 → 100000/200/100 = 5 units → terminal 5×200×100 = 100000.
    # Prior NAV 100 (the bug) would give 10 units → 200000.
    assert perf.benchmark_value_paise == 100_000


def test_sign_trap_buy_adds_sell_removes(session: Session, user: User) -> None:
    """Buy 20 units then sell 10 → net 10 benchmark units (buy adds, sell removes)."""
    inst = _instrument(session, user.id, current_nav=Decimal("100"))
    _txn(
        session,
        user.id,
        inst.id,
        txn_type="buy",
        units=Decimal("20"),
        amount_paise=200_000,
        price=Decimal("100"),
        on=_BUY_ON,
    )
    _txn(
        session,
        user.id,
        inst.id,
        txn_type="sell",
        units=Decimal("10"),
        amount_paise=100_000,
        price=Decimal("100"),
        on=date(2025, 9, 19),
    )
    b = _benchmark(session)
    _nav(session, b.id, _BUY_ON, "100")
    _nav(session, b.id, _AS_OF, "100")

    perf = compute_portfolio_performance(session, user_id=user.id, benchmark_id=b.id, as_of=_AS_OF)

    # +20 − 10 = 10 units × NAV 100 × 100 = 100000.
    assert perf.benchmark_value_paise == 100_000


def test_cashflow_after_cache_clamps_and_flags_stale(session: Session, user: User) -> None:
    inst = _instrument(session, user.id, current_nav=Decimal("100"))
    _txn(
        session,
        user.id,
        inst.id,
        txn_type="buy",
        units=Decimal("10"),
        amount_paise=100_000,
        price=Decimal("100"),
        on=date(2025, 12, 1),
    )  # after the last cached NAV
    b = _benchmark(session)
    _nav(session, b.id, date(2025, 6, 19), "100")  # cache ends here

    perf = compute_portfolio_performance(session, user_id=user.id, benchmark_id=b.id, as_of=_AS_OF)

    assert perf.benchmark_cache_stale is True
    assert perf.benchmark_value_paise == 100_000  # clamped to the last cached NAV (100)


def test_pre_inception_clamps_and_flags_partial(session: Session, user: User) -> None:
    inst = _instrument(session, user.id, current_nav=Decimal("200"))
    _txn(
        session,
        user.id,
        inst.id,
        txn_type="buy",
        units=Decimal("10"),
        amount_paise=100_000,
        price=Decimal("100"),
        on=date(2024, 1, 1),
    )  # before the fund's earliest NAV
    b = _benchmark(session)
    _nav(session, b.id, _BUY_ON, "100")  # inception
    _nav(session, b.id, _AS_OF, "200")

    perf = compute_portfolio_performance(session, user_id=user.id, benchmark_id=b.id, as_of=_AS_OF)

    assert perf.partial is True
    assert perf.benchmark_xirr is not None  # still computed, just flagged
    assert perf.benchmark_value_paise == 200_000  # priced at inception NAV 100 → 10 units


def test_partial_and_cache_stale_flags_together(session: Session, user: User) -> None:
    """One buy pre-inception (partial) + one buy past the cache (cache_stale): both flags
    fire at once — guards a regression that drops one of the two assignment sites."""
    inst = _instrument(session, user.id, current_nav=Decimal("100"))
    _txn(
        session,
        user.id,
        inst.id,
        txn_type="buy",
        units=Decimal("10"),
        amount_paise=100_000,
        price=Decimal("100"),
        on=date(2024, 1, 1),
    )  # before the only cached NAV → partial
    _txn(
        session,
        user.id,
        inst.id,
        txn_type="buy",
        units=Decimal("10"),
        amount_paise=100_000,
        price=Decimal("100"),
        on=date(2026, 3, 1),
    )  # after the cache → cache_stale
    b = _benchmark(session)
    _nav(session, b.id, _BUY_ON, "100")  # lone cached NAV (2025-06-19)

    perf = compute_portfolio_performance(session, user_id=user.id, benchmark_id=b.id, as_of=_AS_OF)

    assert perf.partial is True
    assert perf.benchmark_cache_stale is True


def test_as_of_before_inception_is_unavailable(session: Session, user: User) -> None:
    early_as_of = date(2024, 12, 1)
    inst = _instrument(session, user.id, current_nav=Decimal("100"))
    _txn(
        session,
        user.id,
        inst.id,
        txn_type="buy",
        units=Decimal("10"),
        amount_paise=100_000,
        price=Decimal("100"),
        on=date(2024, 6, 1),
    )
    b = _benchmark(session)
    _nav(session, b.id, _BUY_ON, "100")  # whole cache postdates early_as_of

    perf = compute_portfolio_performance(
        session, user_id=user.id, benchmark_id=b.id, as_of=early_as_of
    )

    assert perf.benchmark_xirr is None
    assert perf.benchmark_unavailable_reason == "as_of_before_inception"
    assert perf.alpha is None


def test_no_benchmark_data(session: Session, user: User) -> None:
    inst = _instrument(session, user.id, current_nav=Decimal("150"))
    _txn(
        session,
        user.id,
        inst.id,
        txn_type="buy",
        units=Decimal("10"),
        amount_paise=100_000,
        price=Decimal("100"),
        on=_BUY_ON,
    )
    b = _benchmark(session)  # no NAV rows seeded

    perf = compute_portfolio_performance(session, user_id=user.id, benchmark_id=b.id, as_of=_AS_OF)

    assert perf.benchmark_xirr is None
    assert perf.benchmark_unavailable_reason == "no_benchmark_data"
    assert perf.benchmark_value_paise == 0
    assert perf.portfolio_xirr is not None  # portfolio leg still computed


def test_empty_portfolio_no_cashflows(session: Session, user: User) -> None:
    b = _benchmark(session)
    _nav(session, b.id, _BUY_ON, "100")
    _nav(session, b.id, _AS_OF, "200")

    perf = compute_portfolio_performance(session, user_id=user.id, benchmark_id=b.id, as_of=_AS_OF)

    assert perf.portfolio_value_paise == 0
    assert perf.benchmark_xirr is None
    assert perf.benchmark_unavailable_reason == "no_portfolio_cashflows"
    assert perf.alpha is None


def test_negative_units_guard(session: Session, user: User) -> None:
    """A cash dividend exceeding the buy over-sells benchmark units → guarded, not garbage."""
    inst = _instrument(session, user.id, current_nav=Decimal("150"))
    _txn(
        session,
        user.id,
        inst.id,
        txn_type="buy",
        units=Decimal("10"),
        amount_paise=100_000,
        price=Decimal("100"),
        on=_BUY_ON,
    )
    _txn(session, user.id, inst.id, txn_type="dividend", amount_paise=300_000, on=date(2025, 9, 1))
    b = _benchmark(session)
    _nav(session, b.id, _BUY_ON, "100")
    _nav(session, b.id, _AS_OF, "100")  # flat → +10 then −30 units = −20 net

    perf = compute_portfolio_performance(session, user_id=user.id, benchmark_id=b.id, as_of=_AS_OF)

    assert perf.benchmark_xirr is None
    assert perf.benchmark_unavailable_reason == "negative_units"
    assert perf.alpha is None


def test_staleness_flagged_but_alpha_computed(session: Session, user: User) -> None:
    """PRD §Verification §4: a stale portfolio leg flags staleness, still computes alpha."""
    stale = datetime(2026, 6, 14, tzinfo=UTC)  # 5 days before _AS_OF
    inst = _instrument(session, user.id, current_nav=Decimal("150"), nav_updated_at=stale)
    _txn(
        session,
        user.id,
        inst.id,
        txn_type="buy",
        units=Decimal("10"),
        amount_paise=100_000,
        price=Decimal("100"),
        on=_BUY_ON,
    )
    b = _benchmark(session)
    _nav(session, b.id, _BUY_ON, "100")
    _nav(session, b.id, _AS_OF, "200")

    perf = compute_portfolio_performance(session, user_id=user.id, benchmark_id=b.id, as_of=_AS_OF)

    assert perf.nav_staleness_days == 5
    assert perf.alpha is not None  # shown, not suppressed


def test_hand_priced_holding_reports_its_real_valuation_age(session: Session, user: User) -> None:
    """PRD §Verification §4 (new bullet): an fd priced off a 90-day-old statement reads 90.

    The asset class is the point, not decoration. ``refresh_navs`` classes ``fd`` as
    ``skipped`` — there is no source — so this is a holding the sync button can never
    correct. Before ``nav_as_of`` the manual write stamped the write instant, this number
    was 0, and the page therefore showed no caveat for exactly the holdings whose price
    only the user can refresh. Stamped through ``as_valuation_stamp`` so the encoding is
    the route's, not a hand-rolled literal that could drift from it.
    """
    inst = _instrument(
        session,
        user.id,
        asset_class="fd",
        current_nav=Decimal("150"),
        nav_updated_at=as_valuation_stamp(_AS_OF - timedelta(days=90)),
    )
    _txn(
        session,
        user.id,
        inst.id,
        txn_type="buy",
        units=Decimal("10"),
        amount_paise=100_000,
        price=Decimal("100"),
        on=_BUY_ON,
    )
    b = _benchmark(session)
    _nav(session, b.id, _BUY_ON, "100")
    _nav(session, b.id, _AS_OF, "200")

    perf = compute_portfolio_performance(session, user_id=user.id, benchmark_id=b.id, as_of=_AS_OF)

    assert perf.nav_staleness_days == 90


def test_the_two_staleness_numbers_differ_and_are_named_apart(session: Session, user: User) -> None:
    """B#49: one user, one instant, two correct answers — now under two names.

    Both numbers used to be called ``nav_staleness_days`` and neither module referenced
    the other, so ``GET /portfolio/performance`` and ``POST /instruments/refresh-navs``
    could report 1 and 200 for the same data with both docstrings claiming the same
    meaning. They fold the SAME expression (``holdings_service.max_staleness_days``) over
    different populations: the held, NAV-bearing, FX-priceable set here; every active
    priced instrument there, fully-exited positions included.

    Both instruments are ``gold`` so ``refresh_navs`` has no source to call for either —
    the transport asserts it is never touched, which also pins that a catalogue with no
    auto-priceable rows makes no network call.
    """
    held = _instrument(
        session,
        user.id,
        symbol="GOLDHELD",
        asset_class="gold",
        current_nav=Decimal("150"),
        nav_updated_at=as_valuation_stamp(_AS_OF - timedelta(days=1)),
    )
    _txn(
        session,
        user.id,
        held.id,
        txn_type="buy",
        units=Decimal("10"),
        amount_paise=100_000,
        price=Decimal("100"),
        on=_BUY_ON,
    )
    # Bought and fully sold — no longer a holding, but still an ACTIVE instrument row, so
    # its 200-day-old valuation ages forever in the catalogue and never in the portfolio.
    exited = _instrument(
        session,
        user.id,
        symbol="GOLDEXITED",
        asset_class="gold",
        current_nav=Decimal("90"),
        nav_updated_at=as_valuation_stamp(_AS_OF - timedelta(days=200)),
    )
    for kind in ("buy", "sell"):
        _txn(
            session,
            user.id,
            exited.id,
            txn_type=kind,  # ty: ignore[invalid-argument-type]
            units=Decimal("5"),
            amount_paise=50_000,
            price=Decimal("100"),
            on=_BUY_ON,
        )
    b = _benchmark(session)
    _nav(session, b.id, _BUY_ON, "100")
    _nav(session, b.id, _AS_OF, "200")

    perf = compute_portfolio_performance(session, user_id=user.id, benchmark_id=b.id, as_of=_AS_OF)

    def _no_network(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError(f"no source should be fetched for gold-only holdings: {request.url}")

    with httpx.Client(transport=httpx.MockTransport(_no_network)) as http:
        refresh = refresh_navs(
            session,
            user_id=user.id,
            client=http,
            amfi_url="https://amfi.test/NAVAll.txt",
            yahoo_base_url="https://yahoo.test/v8/finance/chart",
            as_of=_AS_OF,
        )

    assert perf.nav_staleness_days == 1  # the held set
    assert refresh.catalogue_staleness_days == 200  # the whole catalogue
    assert refresh.skipped == 2  # neither is auto-priceable, so neither warning is actionable


def test_a_friday_nav_is_three_days_old_on_monday_and_four_on_tuesday(
    session: Session, user: User
) -> None:
    """The off-by-one behind B#50, pinned as arithmetic against the shared constant.

    The old UI gate was ``>= 3`` under a comment claiming it excluded "beyond a weekend's
    lag". Friday to Monday IS 3 calendar days, so every Indian-MF portfolio showed "NAVs
    are 3 days behind — refresh them" every Monday, and pressing sync returned
    stale_skipped and changed nothing all day. ``STALENESS_WARN_DAYS = 4`` is the smallest
    threshold that clears an ordinary Monday while still catching the Tuesday after a
    Monday market holiday.

    This asserts the two numbers and their relation to the constant. It does NOT assert
    the gate: nothing server-side applies the threshold — the API reports the raw age and
    the client compares — and there are no frontend tests (frontend/CLAUDE.md). Renaming
    the constant or changing 4 breaks this; a regression in the tsx would not.
    """
    friday, monday, tuesday = date(2026, 6, 19), date(2026, 6, 22), date(2026, 6, 23)
    inst = _instrument(
        session, user.id, current_nav=Decimal("150"), nav_updated_at=as_valuation_stamp(friday)
    )
    _txn(
        session,
        user.id,
        inst.id,
        txn_type="buy",
        units=Decimal("10"),
        amount_paise=100_000,
        price=Decimal("100"),
        on=_BUY_ON,
    )
    b = _benchmark(session)
    _nav(session, b.id, _BUY_ON, "100")
    _nav(session, b.id, friday, "200")

    def age_on(as_of: date) -> int | None:
        return compute_portfolio_performance(
            session, user_id=user.id, benchmark_id=b.id, as_of=as_of
        ).nav_staleness_days

    assert age_on(monday) == 3
    assert age_on(monday) < STALENESS_WARN_DAYS  # an ordinary Monday must stay quiet
    assert age_on(tuesday) == 4
    assert age_on(tuesday) >= STALENESS_WARN_DAYS  # Tuesday after a Monday holiday warns


def test_is_multi_asset_flag(session: Session, user: User) -> None:
    mf = _instrument(
        session, user.id, symbol="INF001", asset_class="indian_mf", current_nav=Decimal("150")
    )
    eq = _instrument(
        session, user.id, symbol="TCS", asset_class="indian_equity", current_nav=Decimal("200")
    )
    _txn(
        session,
        user.id,
        mf.id,
        txn_type="buy",
        units=Decimal("10"),
        amount_paise=100_000,
        price=Decimal("100"),
        on=_BUY_ON,
    )
    _txn(
        session,
        user.id,
        eq.id,
        txn_type="buy",
        units=Decimal("5"),
        amount_paise=100_000,
        price=Decimal("200"),
        on=_BUY_ON,
    )
    b = _benchmark(session)
    _nav(session, b.id, _BUY_ON, "100")
    _nav(session, b.id, _AS_OF, "200")

    perf = compute_portfolio_performance(session, user_id=user.id, benchmark_id=b.id, as_of=_AS_OF)

    assert perf.is_multi_asset is True


def _usd_holding(session: Session, user_id: UUID) -> Instrument:
    inst = Instrument(
        user_id=user_id,
        symbol="AAPL",
        name="Apple",
        asset_class="us_equity",
        currency="USD",
        exchange="NASDAQ",
        current_nav=Decimal("150"),
        nav_updated_at=datetime(_AS_OF.year, _AS_OF.month, _AS_OF.day, tzinfo=UTC),
    )
    session.add(inst)
    session.flush()
    session.add(
        InvestmentTransaction(
            user_id=user_id,
            instrument_id=inst.id,
            date=_BUY_ON,
            transaction_type="buy",
            units=Decimal("10"),
            price_per_unit_native=Decimal("100"),
            amount_native_paise=100_000,
            fees_native_paise=0,
            fx_rate_to_inr=Decimal("80"),
        )
    )
    session.flush()
    return inst


def test_usd_holding_alpha_computed_no_guard(session: Session, user: User) -> None:
    """A USD holding no longer raises (the old INR guard is gone): cashflows are
    INR-normalised, so alpha is INR-vs-INR. $1000@80 → ₹80k in, worth ₹120k (50%);
    the INR index doubled (100%) → alpha ≈ −0.5. fx_staleness 0, fx_unavailable 0."""
    _usd_holding(session, user.id)
    session.add(
        FxRateQuote(
            date=_AS_OF, from_currency="USD", to_currency="INR", rate=Decimal("80"), source="seed"
        )
    )
    b = _benchmark(session)
    _nav(session, b.id, _BUY_ON, "100")
    _nav(session, b.id, _AS_OF, "200")
    session.flush()

    perf = compute_portfolio_performance(session, user_id=user.id, benchmark_id=b.id, as_of=_AS_OF)

    assert perf.portfolio_value_paise == 150_000 * 80  # USD value at the as-of rate, in INR
    assert perf.portfolio_xirr is not None and abs(perf.portfolio_xirr - 0.5) < 0.001
    assert perf.benchmark_xirr is not None and abs(perf.benchmark_xirr - 1.0) < 0.001
    assert perf.alpha is not None and abs(perf.alpha - (-0.5)) < 0.001
    assert perf.fx_unavailable_count == 0
    assert perf.fx_staleness_days == 0


def test_usd_holding_no_fx_rate_flagged_not_raised(session: Session, user: User) -> None:
    """No fx_rates cached ⇒ the USD holding can't be priced in INR: excluded from the sourced
    set (so no cashflows), flagged via fx_unavailable_count, and — critically — no exception."""
    _usd_holding(session, user.id)
    b = _benchmark(session)
    _nav(session, b.id, _BUY_ON, "100")
    _nav(session, b.id, _AS_OF, "200")
    session.flush()

    perf = compute_portfolio_performance(session, user_id=user.id, benchmark_id=b.id, as_of=_AS_OF)

    assert perf.fx_unavailable_count == 1
    assert perf.benchmark_unavailable_reason == "no_portfolio_cashflows"
    assert perf.alpha is None
    assert perf.fx_staleness_days is None  # no sourced holding to compute against
