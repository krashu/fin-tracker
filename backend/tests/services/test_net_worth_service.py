"""Unit tests for Net Worth & Cashflow Waterfall engine (PRD §F8 View 4 & Wealth Analytics)."""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy.orm import Session

from app.core import clock
from app.models import (
    Account,
    Category,
    PeerContact,
    PeerEntry,
    PortfolioDailySnapshot,
    Transaction,
    User,
)
from app.services.category_service import (
    get_or_create_investment_transfer_category,
)
from app.services.net_worth_service import (
    compute_cashflow_waterfall,
    compute_net_worth_summary,
    compute_net_worth_timeline,
)


def _make_account(
    session: Session,
    user: User,
    *,
    name: str = "HDFC Bank",
    account_type: str = "bank",
    opening_balance_paise: int = 0,
) -> Account:
    acct = Account(
        user_id=user.id,
        name=name,
        type=account_type,
        opening_balance_paise=opening_balance_paise,
    )
    session.add(acct)
    session.flush()
    return acct


def _make_txn(
    session: Session,
    user: User,
    account: Account,
    *,
    txn_date: date,
    amount_paise: int,
    txn_type: str = "spend",
    category_id: int | None = None,
    confirmed: bool = True,
) -> Transaction:
    txn = Transaction(
        user_id=user.id,
        account_id=account.id,
        date=txn_date,
        amount_paise=amount_paise,
        transaction_type=txn_type,
        category_id=category_id,
        merchant_raw="Merchant",
        merchant_normalized="merchant",
        fingerprint=uuid.uuid4().hex,
        source="manual",
        confirmed_at=clock.naive_utcnow() if confirmed else None,
    )
    session.add(txn)
    session.flush()
    return txn


def _make_peer_entry(
    session: Session,
    user: User,
    contact: PeerContact,
    *,
    entry_date: date,
    amount_paise: int,
    direction: str,
) -> PeerEntry:
    entry = PeerEntry(
        user_id=user.id,
        contact_id=contact.id,
        date=entry_date,
        amount_paise=amount_paise,
        direction=direction,
    )
    session.add(entry)
    session.flush()
    return entry


def test_empty_user_net_worth_summary(session: Session, user: User) -> None:
    today = date(2026, 6, 1)
    summary = compute_net_worth_summary(session, user_id=user.id, as_of=today)
    assert summary.as_of == today
    assert summary.net_worth_paise == 0
    assert summary.assets_paise == 0
    assert summary.liabilities_paise == 0
    assert summary.delta_30d_paise == 0
    assert summary.delta_30d_pct is None
    assert summary.delta_1y_paise == 0
    assert summary.delta_1y_pct is None
    assert summary.breakdown.bank_cash_paise == 0
    assert summary.breakdown.indian_equity_paise == 0
    assert summary.breakdown.peer_receivables_paise == 0
    assert summary.breakdown.peer_payables_paise == 0


def test_bank_overdraft_correctly_reduces_net_worth(session: Session, user: User) -> None:
    """Overdraft on bank/cash account legitimately reduces net worth and surfaces as liability."""
    today = date(2026, 6, 1)
    acct = _make_account(
        session, user, name="SBI Overdraft", account_type="bank", opening_balance_paise=100_000
    )
    # Spend ₹1500 (150,000 paise) so balance becomes -₹500 (-50,000 paise)
    _make_txn(session, user, acct, txn_date=date(2026, 5, 15), amount_paise=-150_000)

    summary = compute_net_worth_summary(session, user_id=user.id, as_of=today)
    assert summary.net_worth_paise == -50_000
    assert summary.assets_paise == 0
    assert summary.liabilities_paise == 50_000
    assert summary.breakdown.bank_cash_paise == -50_000

    timeline = compute_net_worth_timeline(session, user_id=user.id, range_str="1M", as_of=today)
    last_pt = timeline.points[-1]
    assert last_pt.net_worth_paise == -50_000
    assert last_pt.assets_paise == 0
    assert last_pt.liabilities_paise == 50_000
    assert last_pt.bank_cash_paise == -50_000


def test_credit_card_spending_does_not_reduce_net_worth_as_liability(
    session: Session, user: User
) -> None:
    """Credit cards are spend channels, not liabilities; strictly excluded from net worth."""
    today = date(2026, 6, 1)
    _make_account(
        session, user, name="HDFC Savings", account_type="bank", opening_balance_paise=200_000
    )
    card = _make_account(
        session, user, name="Amazon ICICI", account_type="credit_card", opening_balance_paise=0
    )

    # Card spend ₹500
    _make_txn(session, user, card, txn_date=date(2026, 5, 20), amount_paise=-50_000)

    summary = compute_net_worth_summary(session, user_id=user.id, as_of=today)
    # Bank cash remains ₹2000, card spend does NOT reduce net worth or add liability
    assert summary.net_worth_paise == 200_000
    assert summary.assets_paise == 200_000
    assert summary.liabilities_paise == 0
    assert summary.breakdown.bank_cash_paise == 200_000


def test_peer_debt_net_receivables_and_payables(session: Session, user: User) -> None:
    """Net receivables add to assets; net payables count as liabilities (ADR-0017)."""
    today = date(2026, 6, 1)
    _make_account(session, user, name="HDFC", account_type="bank", opening_balance_paise=500_000)

    contact_alice = PeerContact(user_id=user.id, name="Alice")
    contact_bob = PeerContact(user_id=user.id, name="Bob")
    session.add_all([contact_alice, contact_bob])
    session.flush()

    # User gave Alice ₹1,000 (100,000 paise) -> Alice owes user (Receivable)
    _make_peer_entry(
        session,
        user,
        contact_alice,
        entry_date=date(2026, 5, 10),
        amount_paise=100_000,
        direction="you_gave",
    )

    # Bob gave user ₹400 (40,000 paise) -> User owes Bob (Payable)
    _make_peer_entry(
        session,
        user,
        contact_bob,
        entry_date=date(2026, 5, 12),
        amount_paise=40_000,
        direction="you_got",
    )

    summary = compute_net_worth_summary(session, user_id=user.id, as_of=today)
    # Bank = ₹5000, Receivables = ₹1000, Payables = ₹400
    # Assets = 5000 + 1000 = ₹6000 (600,000 paise)
    # Liabilities = ₹400 (40,000 paise)
    # Net Worth = 600,000 - 40,000 = 560,000 paise
    assert summary.breakdown.peer_receivables_paise == 100_000
    assert summary.breakdown.peer_payables_paise == 40_000
    assert summary.assets_paise == 600_000
    assert summary.liabilities_paise == 40_000
    assert summary.net_worth_paise == 560_000


def test_investment_snapshots_rolled_up_into_net_worth(session: Session, user: User) -> None:
    """Investment forward-sweep valuation snapshots persist and roll up into net worth."""
    today = date(2026, 6, 1)
    _make_account(session, user, name="HDFC", account_type="bank", opening_balance_paise=300_000)

    # Persist daily snapshot for investments
    snap = PortfolioDailySnapshot(
        user_id=user.id,
        scope_type="all",
        scope_id=0,
        snapshot_date=today,
        portfolio_value_paise=1_000_000,  # ₹10,000
        invested_capital_paise=800_000,
        net_cashflow_paise=0,
        twr_index_micros=1_000_000,
    )
    session.add(snap)
    session.flush()

    summary = compute_net_worth_summary(session, user_id=user.id, as_of=today)
    # Bank 300,000 + Investment 1,000,000 = 1,300,000 paise
    assert summary.net_worth_paise == 1_300_000
    assert summary.assets_paise == 1_300_000
    assert summary.liabilities_paise == 0


def test_multi_year_timeline_decimation_preserves_end_of_period_closing(
    session: Session, user: User
) -> None:
    """Decimation for multi-year ranges (3Y, 5Y, ALL) preserves exact closing values."""
    as_of = date(2026, 12, 31)
    bank = _make_account(
        session, user, name="HDFC Bank", account_type="bank", opening_balance_paise=100_000
    )

    # Add transactions spanning 3 years
    _make_txn(session, user, bank, txn_date=date(2024, 1, 15), amount_paise=50_000)
    _make_txn(session, user, bank, txn_date=date(2025, 6, 30), amount_paise=75_000)
    _make_txn(session, user, bank, txn_date=date(2026, 12, 31), amount_paise=25_000)

    # 3Y request -> weekly interval
    resp_3y = compute_net_worth_timeline(session, user_id=user.id, range_str="3Y", as_of=as_of)
    assert resp_3y.interval == "weekly"
    # End point must be exact closing date and amount
    assert resp_3y.points[-1].date == as_of
    assert resp_3y.points[-1].bank_cash_paise == 250_000
    assert resp_3y.points[-1].net_worth_paise == 250_000

    # 5Y request -> monthly interval
    resp_5y = compute_net_worth_timeline(session, user_id=user.id, range_str="5Y", as_of=as_of)
    assert resp_5y.interval == "monthly"
    assert resp_5y.points[-1].date == as_of
    assert resp_5y.points[-1].bank_cash_paise == 250_000
    assert resp_5y.points[-1].net_worth_paise == 250_000


def test_cashflow_waterfall_decomposition(session: Session, user: User) -> None:
    """Cashflow waterfall decomposes income -> expenses -> peer net -> investments -> surplus."""
    bank = _make_account(
        session, user, name="HDFC Bank", account_type="bank", opening_balance_paise=0
    )

    # Categories
    cat_salary = Category(user_id=user.id, name="Salary", kind="income", color="#10b981")
    cat_food = Category(user_id=user.id, name="Food & Dining", kind="spend", color="#f43f5e")
    cat_rent = Category(user_id=user.id, name="Housing Rent", kind="spend", color="#e11d48")
    inv_transfer_cat = get_or_create_investment_transfer_category(session, user_id=user.id)
    session.add_all([cat_salary, cat_food, cat_rent])
    session.flush()

    year = 2026
    # 1. Income: ₹1,00,000
    _make_txn(
        session,
        user,
        bank,
        txn_date=date(2026, 1, 10),
        amount_paise=10_000_000,
        txn_type="income",
        category_id=cat_salary.id,
    )

    # 2. Living Expenses: Food ₹20,000, Rent ₹30,000
    _make_txn(
        session,
        user,
        bank,
        txn_date=date(2026, 2, 1),
        amount_paise=-2_000_000,
        txn_type="spend",
        category_id=cat_food.id,
    )
    _make_txn(
        session,
        user,
        bank,
        txn_date=date(2026, 2, 5),
        amount_paise=-3_000_000,
        txn_type="spend",
        category_id=cat_rent.id,
    )

    # 3. Peer lending: Lent ₹5,000, got back ₹2,000 -> Net lent -₹3,000
    contact = PeerContact(user_id=user.id, name="Dave")
    session.add(contact)
    session.flush()
    _make_peer_entry(
        session,
        user,
        contact,
        entry_date=date(2026, 3, 1),
        amount_paise=500_000,
        direction="you_gave",
    )
    _make_peer_entry(
        session,
        user,
        contact,
        entry_date=date(2026, 3, 15),
        amount_paise=200_000,
        direction="you_got",
    )

    # 4. Investment transfer: ₹25,000
    _make_txn(
        session,
        user,
        bank,
        txn_date=date(2026, 4, 1),
        amount_paise=-2_500_000,
        txn_type="spend",
        category_id=inv_transfer_cat.id,
    )

    waterfall = compute_cashflow_waterfall(
        session, user_id=user.id, year=year, year_type="calendar"
    )

    assert waterfall.gross_income_paise == 10_000_000
    assert waterfall.total_living_expenses_paise == -5_000_000
    assert waterfall.peer_net_paise == -300_000
    assert waterfall.investment_transfers_paise == -2_500_000
    # Expected liquid surplus: 10,000,000 - 5,000,000 - 300,000 - 2,500,000 = 2,200,000 paise
    assert waterfall.net_liquid_surplus_paise == 2_200_000

    # Steps check
    step_labels = [s.label for s in waterfall.steps]
    assert "Gross Income" in step_labels
    assert "Housing Rent" in step_labels
    assert "Food & Dining" in step_labels
    assert "Peer Lending / Dues" in step_labels
    assert "Investment Additions" in step_labels
    assert "Net Liquid Surplus" in step_labels
    assert waterfall.steps[-1].cumulative_paise == 2_200_000


def test_cashflow_waterfall_fiscal_year(session: Session, user: User) -> None:
    """Fiscal year bounds apply correctly (1 Apr to 31 Mar)."""
    bank = _make_account(
        session, user, name="HDFC Bank", account_type="bank", opening_balance_paise=0
    )
    cat_salary = Category(user_id=user.id, name="Salary", kind="income", color="#10b981")
    session.add(cat_salary)
    session.flush()

    # Txn in March 2026 (FY 2025-26) vs April 2026 (FY 2026-27)
    _make_txn(
        session,
        user,
        bank,
        txn_date=date(2026, 3, 20),
        amount_paise=5_000_000,
        txn_type="income",
        category_id=cat_salary.id,
    )
    _make_txn(
        session,
        user,
        bank,
        txn_date=date(2026, 4, 10),
        amount_paise=7_000_000,
        txn_type="income",
        category_id=cat_salary.id,
    )

    # FY 2026 means 2026-04-01 to 2027-03-31
    wf_fy26 = compute_cashflow_waterfall(session, user_id=user.id, year=2026, year_type="fiscal")
    assert wf_fy26.start_date == date(2026, 4, 1)
    assert wf_fy26.end_date == date(2027, 3, 31)
    assert wf_fy26.gross_income_paise == 7_000_000

    # FY 2025 means 2025-04-01 to 2026-03-31
    wf_fy25 = compute_cashflow_waterfall(session, user_id=user.id, year=2025, year_type="fiscal")
    assert wf_fy25.start_date == date(2025, 4, 1)
    assert wf_fy25.end_date == date(2026, 3, 31)
    assert wf_fy25.gross_income_paise == 5_000_000
