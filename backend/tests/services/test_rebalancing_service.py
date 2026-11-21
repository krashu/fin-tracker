"""Tests for rebalancing service (Milestone 4 Phase 1, PRD §F7/§F8).

Covers:
- Target allocation CRUD and total bps validation (<= 10000)
- Drift solver: actual_bps, absolute drift, relative drift
- Swedroe 5/25 tolerance heuristic (absolute > 500 bps OR relative > 25.0%)
- Water-filling optimization (Mode A): partial cash, exact cash, surplus cash
- Tax-aware full rebalance (Mode B): lot ranking by tax drag, ELSS 3-year lock-in guard
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core import clock
from app.models import FxRateQuote, Instrument, InvestmentAccount, InvestmentTransaction, User
from app.schemas.rebalancing import (
    AllocationTargetBatchCreate,
    AllocationTargetCreate,
    AllocationTargetUpdate,
)
from app.services import rebalancing_service


@pytest.fixture()
def test_user(session: Session) -> User:
    u = User(email="investor-rebalance@example.com", password_hash="pw")
    session.add(u)
    session.flush()
    return u


@pytest.fixture()
def test_account(session: Session, test_user: User) -> InvestmentAccount:
    acc = InvestmentAccount(
        user_id=test_user.id,
        name="Zerodha Demat",
        account_type="demat",
        provider="zerodha",
        external_account_ref="DEMAT-123",
        currency="INR",
    )
    session.add(acc)
    session.flush()
    return acc


def test_target_crud_and_bps_validation(
    session: Session, test_user: User, test_account: InvestmentAccount
) -> None:
    # 1. Create portfolio-wide target
    t1 = rebalancing_service.create_target(
        session,
        user_id=test_user.id,
        target_in=AllocationTargetCreate(
            scope_type="all",
            account_id=None,
            target_type="asset_class",
            target_key="indian_equity",
            target_bps=6000,
            rebalance_threshold_bps=500,
        ),
    )
    assert t1.id is not None
    assert t1.target_bps == 6000

    # 2. Create second target
    t2 = rebalancing_service.create_target(
        session,
        user_id=test_user.id,
        target_in=AllocationTargetCreate(
            scope_type="all",
            account_id=None,
            target_type="asset_class",
            target_key="gold",
            target_bps=3000,
        ),
    )
    assert t2.target_bps == 3000

    # 3. Third target exceeding 10000 bps -> 422
    with pytest.raises(HTTPException) as exc:
        rebalancing_service.create_target(
            session,
            user_id=test_user.id,
            target_in=AllocationTargetCreate(
                scope_type="all",
                account_id=None,
                target_type="asset_class",
                target_key="debt",
                target_bps=2000,  # 6000 + 3000 + 2000 = 11000 > 10000
            ),
        )
    assert exc.value.status_code == 422
    assert "exceeds 10000 bps" in exc.value.detail

    # 4. Duplicate target in same scope -> 409
    with pytest.raises(HTTPException) as exc_dup:
        rebalancing_service.create_target(
            session,
            user_id=test_user.id,
            target_in=AllocationTargetCreate(
                scope_type="all",
                account_id=None,
                target_type="asset_class",
                target_key="indian_equity",
                target_bps=500,
            ),
        )
    assert exc_dup.value.status_code == 409

    # 5. List targets
    targets = rebalancing_service.list_targets(session, user_id=test_user.id, scope_type="all")
    assert len(targets) == 2

    # 6. Update target
    updated = rebalancing_service.update_target(
        session,
        user_id=test_user.id,
        target_id=t2.id,
        target_in=AllocationTargetUpdate(target_bps=4000),
    )
    assert updated.target_bps == 4000

    # 7. Update exceeding 10000 bps -> 422
    with pytest.raises(HTTPException) as exc_up:
        rebalancing_service.update_target(
            session,
            user_id=test_user.id,
            target_id=t2.id,
            target_in=AllocationTargetUpdate(target_bps=5000),  # 6000 + 5000 = 11000
        )
    assert exc_up.value.status_code == 422

    # 8. Delete target
    rebalancing_service.delete_target(session, user_id=test_user.id, target_id=t2.id)
    assert (
        len(rebalancing_service.list_targets(session, user_id=test_user.id, scope_type="all")) == 1
    )


def test_batch_set_targets(session: Session, test_user: User) -> None:
    batch_in = AllocationTargetBatchCreate(
        scope_type="all",
        account_id=None,
        targets=[
            AllocationTargetCreate(
                scope_type="all",
                target_type="asset_class",
                target_key="indian_equity",
                target_bps=5000,
            ),
            AllocationTargetCreate(
                scope_type="all",
                target_type="asset_class",
                target_key="debt",
                target_bps=3000,
            ),
            AllocationTargetCreate(
                scope_type="all",
                target_type="asset_class",
                target_key="gold",
                target_bps=2000,
            ),
        ],
    )
    targets = rebalancing_service.batch_set_targets(
        session, user_id=test_user.id, batch_in=batch_in
    )
    assert len(targets) == 3
    assert sum(t.target_bps for t in targets) == 10000


def test_drift_solver_and_swedroe_5_25_tolerance(
    session: Session, test_user: User, test_account: InvestmentAccount
) -> None:
    """Tests absolute and relative drift evaluation under Swedroe 5/25 rule:
    - Absolute trigger: |actual_bps - target_bps| > 500 (5%)
    - Relative trigger: |actual_value - target_value| / target_value > 25.0%
    """
    # Create targets:
    # 1. Indian Equity: Target 60.00% (6000 bps)
    # 2. Gold: Target 10.00% (1000 bps)
    # 3. Debt: Target 30.00% (3000 bps)
    rebalancing_service.create_target(
        session,
        user_id=test_user.id,
        target_in=AllocationTargetCreate(
            scope_type="all",
            target_type="asset_class",
            target_key="indian_equity",
            target_bps=6000,
            rebalance_threshold_bps=500,
        ),
    )
    rebalancing_service.create_target(
        session,
        user_id=test_user.id,
        target_in=AllocationTargetCreate(
            scope_type="all",
            target_type="asset_class",
            target_key="gold",
            target_bps=1000,
            rebalance_threshold_bps=500,
        ),
    )
    rebalancing_service.create_target(
        session,
        user_id=test_user.id,
        target_in=AllocationTargetCreate(
            scope_type="all",
            target_type="asset_class",
            target_key="debt",
            target_bps=3000,
            rebalance_threshold_bps=500,
        ),
    )

    # Create instruments and transactions:
    # Portfolio Total = ₹1,00,000 (10000000 paise)
    # - Equity: ₹67,000 (67.00% -> +700 bps drift > 500 bps -> OUT via ABSOLUTE)
    # - Gold: ₹13,000 (13.00% -> +300 bps drift <= 500 bps, relative drift 30% -> OUT via RELATIVE)
    # - Debt: ₹20,000 (20.00% -> -1000 bps drift > 500 bps -> OUT via ABSOLUTE)
    eq_inst = Instrument(
        user_id=test_user.id,
        symbol="NIFTY50",
        name="Nifty Index Fund",
        asset_class="indian_equity",
        currency="INR",
        exchange="NSE",
        current_nav=Decimal("100.00"),
    )
    gold_inst = Instrument(
        user_id=test_user.id,
        symbol="GOLDBEES",
        name="Gold ETF",
        asset_class="gold",
        currency="INR",
        exchange="NSE",
        current_nav=Decimal("50.00"),
    )
    debt_inst = Instrument(
        user_id=test_user.id,
        symbol="LIQUID",
        name="Liquid Debt Fund",
        asset_class="bond",
        currency="INR",
        exchange="NSE",
        current_nav=Decimal("1000.00"),
    )
    session.add_all([eq_inst, gold_inst, debt_inst])
    session.flush()

    today = clock.today()
    session.add(
        InvestmentTransaction(
            user_id=test_user.id,
            account_id=test_account.id,
            instrument_id=eq_inst.id,
            date=today,
            transaction_type="buy",
            units=Decimal("670.0"),  # 670 * 100 = ₹67,000
            price_per_unit_native=Decimal("100.00"),
            amount_native_paise=6700000,
            fx_rate_to_inr=Decimal("1.0"),
        )
    )
    session.add(
        InvestmentTransaction(
            user_id=test_user.id,
            account_id=test_account.id,
            instrument_id=gold_inst.id,
            date=today,
            transaction_type="buy",
            units=Decimal("260.0"),  # 260 * 50 = ₹13,000
            price_per_unit_native=Decimal("50.00"),
            amount_native_paise=1300000,
            fx_rate_to_inr=Decimal("1.0"),
        )
    )
    session.add(
        InvestmentTransaction(
            user_id=test_user.id,
            account_id=test_account.id,
            instrument_id=debt_inst.id,
            date=today,
            transaction_type="buy",
            units=Decimal("20.0"),  # 20 * 1000 = ₹20,000
            price_per_unit_native=Decimal("1000.00"),
            amount_native_paise=2000000,
            fx_rate_to_inr=Decimal("1.0"),
        )
    )
    session.flush()

    eval_res = rebalancing_service.evaluate_rebalancing(session, user_id=test_user.id)
    assert eval_res.total_portfolio_value_paise == 10000000
    assert eval_res.is_out_of_tolerance is True

    bucket_map = {b.target_key: b for b in eval_res.buckets}

    # Check Equity: 67% actual vs 60% target
    eq_b = bucket_map["indian_equity"]
    assert eq_b.actual_bps == 6700
    assert eq_b.drift_bps == 700
    assert eq_b.is_out_of_tolerance is True
    assert eq_b.action == "sell"

    # Check Gold: 13% actual vs 10% target. Drift is 300 bps (<= 500 bps),
    # but relative drift is (13000 - 10000) / 10000 = 30.0% (> 25.0%) -> triggers Swedroe relative!
    gold_b = bucket_map["gold"]
    assert gold_b.actual_bps == 1300
    assert gold_b.drift_bps == 300
    assert gold_b.relative_drift_pct == 30.0
    assert gold_b.is_out_of_tolerance is True
    assert gold_b.action == "sell"

    # Check Debt: 20% actual vs 30% target. Drift is -1000 bps -> triggers Swedroe absolute!
    debt_b = bucket_map["debt"]
    assert debt_b.actual_bps == 2000
    assert debt_b.drift_bps == -1000
    assert debt_b.is_out_of_tolerance is True
    assert debt_b.action == "buy"


def test_water_filling_inflow_algorithm() -> None:
    """Unit test for pure water-filling cash distribution."""
    # Bucket 1: current ₹10,000, target 50% (5000 bps)
    # Bucket 2: current ₹4,000, target 30% (3000 bps)
    # Bucket 3: current ₹6,000, target 20% (2000 bps)
    # Total current: ₹20,000
    # Inflow: ₹10,000 -> New total ₹30,000
    # Ideal targets: B1: ₹15,000 (deficit ₹5k), B2: ₹9,000 (deficit ₹5k), B3: ₹6,000 (deficit ₹0)
    # Total deficit: ₹10,000 == Inflow! Both B1 and B2 get ₹5,000 exactly!
    buckets = [(1, 1000000, 5000), (2, 400000, 3000), (3, 600000, 2000)]
    alloc = rebalancing_service.solve_water_filling(1000000, buckets, 10000)
    assert alloc[1] == 500000  # ₹5,000
    assert alloc[2] == 500000  # ₹5,000
    assert alloc[3] == 0

    # Partial inflow test: Inflow is only ₹3,000 (300000 paise)
    # Ratios:
    # B1 ratio: 10,000 / 15,000 = 0.667
    # B2 ratio: 4,000 / 9,000 = 0.444 (more severely underweight)
    # Water enters B2 first until it reaches B1's ratio!
    alloc_partial = rebalancing_service.solve_water_filling(300000, buckets, 10000)
    assert sum(alloc_partial.values()) == 300000
    # B2 should receive the majority of funds because it is more underweight
    assert alloc_partial[2] > alloc_partial[1]
    assert alloc_partial[3] == 0


def test_tax_aware_rebalance_elss_lock_and_tax_drag(
    session: Session, test_user: User, test_account: InvestmentAccount
) -> None:
    """Test Mode B: ELSS 3-year lock-in guard and lot ranking by tax drag."""
    today = clock.today()

    # Create target: Equity 40% (4000 bps), Debt 60% (6000 bps)
    rebalancing_service.create_target(
        session,
        user_id=test_user.id,
        target_in=AllocationTargetCreate(
            scope_type="all",
            target_type="asset_class",
            target_key="indian_equity",
            target_bps=4000,
            rebalance_threshold_bps=500,
        ),
    )
    rebalancing_service.create_target(
        session,
        user_id=test_user.id,
        target_in=AllocationTargetCreate(
            scope_type="all",
            target_type="asset_class",
            target_key="debt",
            target_bps=6000,
            rebalance_threshold_bps=500,
        ),
    )

    # ELSS Fund (Mirae Asset Tax Saver)
    elss_inst = Instrument(
        user_id=test_user.id,
        symbol="MIRAE-ELSS",
        name="Mirae Asset ELSS Tax Saver Fund",
        asset_class="indian_equity",
        currency="INR",
        exchange="MFCentral",
        current_nav=Decimal("200.00"),
    )
    # Normal Equity Fund (Nifty 50 ETF)
    nifty_inst = Instrument(
        user_id=test_user.id,
        symbol="NIFTYBEES",
        name="Nippon India Nifty 50 ETF",
        asset_class="indian_equity",
        currency="INR",
        exchange="NSE",
        current_nav=Decimal("250.00"),
    )
    session.add_all([elss_inst, nifty_inst])
    session.flush()

    # Lot 1: ELSS bought 1 year ago (LOCKED: < 3 years old) -> ₹50,000 value
    session.add(
        InvestmentTransaction(
            user_id=test_user.id,
            account_id=test_account.id,
            instrument_id=elss_inst.id,
            date=today - timedelta(days=365),
            transaction_type="buy",
            units=Decimal("250.0"),
            price_per_unit_native=Decimal("150.00"),
            amount_native_paise=3750000,
            fx_rate_to_inr=Decimal("1.0"),
        )
    )
    # Lot 2: Nifty bought 2 years ago (LTCG, unlocked) -> 100 units @ 200 = cost 20k, value 25k
    session.add(
        InvestmentTransaction(
            user_id=test_user.id,
            account_id=test_account.id,
            instrument_id=nifty_inst.id,
            date=today - timedelta(days=730),
            transaction_type="buy",
            units=Decimal("100.0"),
            price_per_unit_native=Decimal("200.00"),
            amount_native_paise=2000000,
            fx_rate_to_inr=Decimal("1.0"),
        )
    )
    # Lot 3: Nifty bought 20 days ago (Loss, unlocked) -> 100 units @ 280 = cost 28k, value 25k
    session.add(
        InvestmentTransaction(
            user_id=test_user.id,
            account_id=test_account.id,
            instrument_id=nifty_inst.id,
            date=today - timedelta(days=20),
            transaction_type="buy",
            units=Decimal("100.0"),
            price_per_unit_native=Decimal("280.00"),
            amount_native_paise=2800000,
            fx_rate_to_inr=Decimal("1.0"),
        )
    )
    session.flush()

    # Rebalance plan Mode B
    plan = rebalancing_service.plan_rebalancing(
        session,
        user_id=test_user.id,
        mode="full_rebalance",
        inflow_paise=0,
        as_of=today,
    )
    assert plan.mode == "full_rebalance"
    assert len(plan.lot_sell_recommendations) > 0

    # Verify that NO ELSS lot is recommended for sale (statutory lock-in guardrail)
    for lot_rec in plan.lot_sell_recommendations:
        assert lot_rec.symbol != "MIRAE-ELSS", "Locked ELSS lot must not be recommended for sale!"

    # Verify that the loss lot (Rank 1) is recommended first before LTCG (Rank 2)
    first_lot = plan.lot_sell_recommendations[0]
    assert first_lot.gain_type == "loss"
    assert first_lot.tax_drag_rank == 1


def test_foreign_usd_assets_included_and_rebalanced(
    session: Session, test_user: User, test_account: InvestmentAccount
) -> None:
    """Verify that foreign USD assets are valued and included in rebalance drift evaluation."""
    today = clock.today()

    session.add(
        FxRateQuote(
            date=today,
            from_currency="USD",
            to_currency="INR",
            rate=Decimal("84.00"),
            source="seed",
        )
    )
    session.flush()

    # Create target: US Equity 50% (5000 bps), Indian Equity 50% (5000 bps)
    rebalancing_service.create_target(
        session,
        user_id=test_user.id,
        target_in=AllocationTargetCreate(
            scope_type="all",
            target_type="asset_class",
            target_key="us_equity",
            target_bps=5000,
            rebalance_threshold_bps=500,
        ),
    )
    rebalancing_service.create_target(
        session,
        user_id=test_user.id,
        target_in=AllocationTargetCreate(
            scope_type="all",
            target_type="asset_class",
            target_key="indian_equity",
            target_bps=5000,
            rebalance_threshold_bps=500,
        ),
    )

    # Add US stock (AAPL): 10 shares @ $150 = $1500. NAV = $200 -> $2000 value.
    # At rate 84, $2000 = ₹1,68,000 (16800000 paise).
    aapl = Instrument(
        user_id=test_user.id,
        symbol="AAPL",
        name="Apple Inc",
        asset_class="us_equity",
        currency="USD",
        exchange="NASDAQ",
        current_nav=Decimal("200.00"),
    )
    # Add Indian stock: 100 shares @ ₹1000 = ₹1,00,000 (10000000 paise).
    infy = Instrument(
        user_id=test_user.id,
        symbol="INFY",
        name="Infosys Ltd",
        asset_class="indian_equity",
        currency="INR",
        exchange="NSE",
        current_nav=Decimal("1000.00"),
    )
    session.add_all([aapl, infy])
    session.flush()

    session.add(
        InvestmentTransaction(
            user_id=test_user.id,
            account_id=test_account.id,
            instrument_id=aapl.id,
            date=today - timedelta(days=100),
            transaction_type="buy",
            units=Decimal("10.0"),
            price_per_unit_native=Decimal("150.00"),
            amount_native_paise=150000,  # 1500 USD cents
            fx_rate_to_inr=Decimal("84.00"),
        )
    )
    session.add(
        InvestmentTransaction(
            user_id=test_user.id,
            account_id=test_account.id,
            instrument_id=infy.id,
            date=today - timedelta(days=100),
            transaction_type="buy",
            units=Decimal("100.0"),
            price_per_unit_native=Decimal("1000.00"),
            amount_native_paise=10000000,
            fx_rate_to_inr=Decimal("1.0"),
        )
    )
    session.flush()

    eval_res = rebalancing_service.evaluate_rebalancing(
        session, user_id=test_user.id, scope_type="all", as_of=today
    )

    # Total portfolio should be AAPL (16800000) + INFY (10000000) = 26800000 paise
    assert eval_res.total_portfolio_value_paise == 26800000
    us_bucket = next(b for b in eval_res.buckets if b.target_key == "us_equity")
    assert us_bucket.actual_value_paise == 16800000
    assert us_bucket.actual_bps == 6269  # 168000 / 268000 = ~62.69%
