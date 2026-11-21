"""API tests for /api/v1/rebalancing (Milestone 4 Phase 1).

Tests CRUD, batch-set, evaluate, and plan endpoints.
"""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core import clock
from app.models import Instrument, InvestmentAccount, InvestmentTransaction, User


def test_rebalancing_targets_api_crud(client: TestClient, seeded_user: User) -> None:
    # 1. GET targets (empty)
    res = client.get("/api/v1/rebalancing/targets")
    assert res.status_code == 200
    assert res.json() == []

    # 2. POST create target
    res_create = client.post(
        "/api/v1/rebalancing/targets",
        json={
            "scope_type": "all",
            "target_type": "asset_class",
            "target_key": "indian_equity",
            "target_bps": 6000,
            "rebalance_threshold_bps": 500,
        },
    )
    assert res_create.status_code == 201
    target_id = res_create.json()["id"]
    assert res_create.json()["target_bps"] == 6000

    # 3. POST target exceeding 10000 bps -> 422
    res_exceed = client.post(
        "/api/v1/rebalancing/targets",
        json={
            "scope_type": "all",
            "target_type": "asset_class",
            "target_key": "debt",
            "target_bps": 5000,  # 6000 + 5000 = 11000 > 10000
        },
    )
    assert res_exceed.status_code == 422

    # 4. POST duplicate target -> 409
    res_dup = client.post(
        "/api/v1/rebalancing/targets",
        json={
            "scope_type": "all",
            "target_type": "asset_class",
            "target_key": "indian_equity",
            "target_bps": 1000,
        },
    )
    assert res_dup.status_code == 409

    # 5. GET single target
    res_get = client.get(f"/api/v1/rebalancing/targets/{target_id}")
    assert res_get.status_code == 200
    assert res_get.json()["id"] == target_id

    # GET non-existent target -> 404
    assert client.get("/api/v1/rebalancing/targets/99999").status_code == 404

    # 6. PATCH target
    res_patch = client.patch(
        f"/api/v1/rebalancing/targets/{target_id}",
        json={"target_bps": 7000, "rebalance_threshold_bps": 400},
    )
    assert res_patch.status_code == 200
    assert res_patch.json()["target_bps"] == 7000
    assert res_patch.json()["rebalance_threshold_bps"] == 400

    # 7. DELETE target
    res_del = client.delete(f"/api/v1/rebalancing/targets/{target_id}")
    assert res_del.status_code == 204
    assert client.get(f"/api/v1/rebalancing/targets/{target_id}").status_code == 404


def test_rebalancing_targets_batch_and_evaluation(
    client: TestClient, seeded_user: User, session: Session
) -> None:
    # 1. Batch set targets: Equity 70%, Debt 30%
    res_batch = client.post(
        "/api/v1/rebalancing/targets/batch",
        json={
            "scope_type": "all",
            "targets": [
                {
                    "scope_type": "all",
                    "target_type": "asset_class",
                    "target_key": "indian_equity",
                    "target_bps": 7000,
                    "rebalance_threshold_bps": 500,
                },
                {
                    "scope_type": "all",
                    "target_type": "asset_class",
                    "target_key": "debt",
                    "target_bps": 3000,
                    "rebalance_threshold_bps": 500,
                },
            ],
        },
    )
    assert res_batch.status_code == 200
    assert len(res_batch.json()) == 2

    # 2. Add investment data:
    # Get user default investment account
    inv_acc = session.query(InvestmentAccount).filter_by(user_id=seeded_user.id).first()
    if not inv_acc:
        inv_acc = InvestmentAccount(
            user_id=seeded_user.id,
            name="Main Demat",
            account_type="demat",
            provider="zerodha",
            external_account_ref="DEMAT-MAIN",
            currency="INR",
        )
        session.add(inv_acc)
        session.flush()

    equity_inst = Instrument(
        user_id=seeded_user.id,
        symbol="NIFTY",
        name="Nifty 50 Fund",
        asset_class="indian_equity",
        currency="INR",
        exchange="NSE",
        current_nav=Decimal("100.00"),
    )
    session.add(equity_inst)
    session.flush()

    session.add(
        InvestmentTransaction(
            user_id=seeded_user.id,
            account_id=inv_acc.id,
            instrument_id=equity_inst.id,
            date=clock.today(),
            transaction_type="buy",
            units=Decimal("100.0"),  # 100 * 100 = ₹10,000 (100% of portfolio)
            price_per_unit_native=Decimal("100.00"),
            amount_native_paise=1000000,
            fx_rate_to_inr=Decimal("1.0"),
        )
    )
    session.commit()

    # 3. POST /rebalancing/evaluate
    res_eval = client.post("/api/v1/rebalancing/evaluate", json={"scope_type": "all"})
    assert res_eval.status_code == 200
    eval_data = res_eval.json()
    assert eval_data["total_portfolio_value_paise"] == 1000000
    assert eval_data["is_out_of_tolerance"] is True
    assert len(eval_data["buckets"]) == 2

    # 4. POST /rebalancing/plan (Mode A: Inflow-Only)
    # Portfolio is ₹10,000 in equity (100%), debt is ₹0 (0%).
    # Targets: Equity 70%, Debt 30%.
    # Add ₹5,000 fresh inflow -> New total ₹15,000.
    # Target Debt: 30% of ₹15,000 = ₹4,500.
    # Target Equity: 70% of ₹15,000 = ₹10,500.
    # Fresh cash should deploy ₹4,500 into Debt and ₹500 into Equity!
    res_plan = client.post(
        "/api/v1/rebalancing/plan",
        json={"scope_type": "all", "mode": "inflow_only", "inflow_paise": 500000},
    )
    assert res_plan.status_code == 200
    plan_data = res_plan.json()
    assert plan_data["mode"] == "inflow_only"
    assert plan_data["post_rebalance_portfolio_value_paise"] == 1500000

    b_actions = {a["target_key"]: a for a in plan_data["bucket_actions"]}
    assert b_actions["debt"]["recommended_cashflow_paise"] == 450000
    assert b_actions["indian_equity"]["recommended_cashflow_paise"] == 50000
    # Zero sales in Mode A
    assert len(plan_data["lot_sell_recommendations"]) == 0
