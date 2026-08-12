"""Service-level tests for the direct-DB demo seeder (app.services.demo_seed).

Exercises :func:`seed_demo_data` against an in-memory session — no TestClient,
no lifespan. The lifespan wiring itself is covered separately in
``tests/test_startup_seed.py``.

Locks the behaviours the frontend/demo depend on: correct row counts, labels
written from the shared dataset, PRD §F4 fingerprints unique across seeded rows,
F3 tag learning fired for spend/refund only (not income), and — the point of
the rolling-window redesign — that calling the seeder again with a later
``clock.today()`` rolls the transaction window forward instead of accumulating.
``clock.today`` is frozen for every test in this file so the generated dataset
(and its expected counts) are deterministic.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import clock
from app.core.config import get_settings
from app.core.demo_data import INSTRUMENTS, build_demo_dataset
from app.models import (
    Account,
    Instrument,
    InvestmentTransaction,
    Label,
    MerchantLabelMap,
    MerchantTagMap,
    Transaction,
    User,
)
from app.services.demo_seed import seed_demo_data
from app.services.merchant_labels import LABEL_PREFILL_MIN
from app.services.provisioning import provision_default_categories

# Fixed anchor so the generated dataset (and its expected counts) is
# deterministic regardless of when the suite runs. Day 20 is comfortably mid-
# month so the current-month truncation includes most of that slot's rows.
_ANCHOR = date(2026, 8, 20)
_SPENDS, _REFUNDS, _INCOME = build_demo_dataset(_ANCHOR)
_TXN_TOTAL = len(_SPENDS) + len(_REFUNDS) + len(_INCOME)
_INV_TXN_TOTAL = sum(len(spec["txns"]) for spec in INSTRUMENTS)


@pytest.fixture(autouse=True)
def _frozen_anchor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Freeze ``clock.today()`` to ``_ANCHOR`` for every test in this module —
    ``seed_demo_data`` calls it internally, and must agree with the expected
    dataset computed above."""
    monkeypatch.setattr(clock, "today", lambda: _ANCHOR)


@pytest.fixture
def seeded_user(session: Session) -> User:
    """User + default categories — the runtime state right after migrations on a
    fresh DB (categories come from the same provisioning source the seeder reads)."""
    user = User(id=get_settings().v1_user_id)
    session.add(user)
    session.flush()  # user row must exist before its categories' FK
    provision_default_categories(session, user.id)
    session.commit()
    return user


def test_seed_creates_expected_counts(session: Session, seeded_user: User) -> None:
    counts = seed_demo_data(session, user_id=seeded_user.id)

    assert counts.accounts == 2
    assert counts.transactions == _TXN_TOTAL
    assert counts.instruments == len(INSTRUMENTS)
    assert counts.investment_transactions == _INV_TXN_TOTAL

    assert session.scalar(select(func.count()).select_from(Account)) == 2
    assert session.scalar(select(func.count()).select_from(Transaction)) == _TXN_TOTAL
    assert session.scalar(select(func.count()).select_from(Instrument)) == len(INSTRUMENTS)
    assert session.scalar(select(func.count()).select_from(InvestmentTransaction)) == _INV_TXN_TOTAL


def test_seed_writes_labels(session: Session, seeded_user: User) -> None:
    seed_demo_data(session, user_id=seeded_user.id)

    # A spend row with labels from the dataset (MakeMyTrip → #travel #goa).
    trip = session.scalar(select(Transaction).where(Transaction.merchant_raw == "MakeMyTrip"))
    assert trip is not None
    assert {lab.name for lab in trip.labels} == {"travel", "goa"}

    # A refund row carries its labels too.
    refund = session.scalar(select(Transaction).where(Transaction.merchant_raw == "Myntra refund"))
    assert refund is not None
    assert {lab.name for lab in refund.labels} == {"festive"}

    # An unlabeled row has no links.
    metro = session.scalar(select(Transaction).where(Transaction.merchant_raw == "Metro"))
    assert metro is not None
    assert list(metro.labels) == []

    # Investment txn note still round-trips (investments keep their free-text note).
    inv_note = session.scalar(
        select(InvestmentTransaction.note).where(InvestmentTransaction.transaction_type == "sell")
    )
    assert inv_note == "Booked partial profit"


def test_seed_cashback_posts_to_the_card_not_the_bank(session: Session, seeded_user: User) -> None:
    """Card cashback is credited on the card statement itself (real Axis
    Flipkart behaviour), not the linked bank account — see IncomeRow.account."""
    seed_demo_data(session, user_id=seeded_user.id)

    cashback = session.scalars(
        select(Transaction).where(Transaction.merchant_raw == "Card Cashback")
    ).all()
    assert cashback  # dataset seeds at least one
    card = session.scalar(select(Account).where(Account.name == "Axis Flipkart"))
    assert card is not None
    assert all(txn.account_id == card.id for txn in cashback)


def test_seed_fingerprints_unique_and_present(session: Session, seeded_user: User) -> None:
    seed_demo_data(session, user_id=seeded_user.id)

    fingerprints = list(session.scalars(select(Transaction.fingerprint)))
    assert len(fingerprints) == _TXN_TOTAL
    assert all(fp is not None for fp in fingerprints)
    assert len(set(fingerprints)) == _TXN_TOTAL  # no collisions across the dataset


def test_seed_records_tags_for_spend_refund_only(session: Session, seeded_user: User) -> None:
    seed_demo_data(session, user_id=seeded_user.id)

    tagged = set(session.scalars(select(MerchantTagMap.merchant_normalized)))
    assert tagged  # F3 learning populated from spend/refund rows
    # Income merchants must NOT feed the spend→category map (AUTO_TAGGABLE_TYPES).
    assert not any("acme payroll" in m for m in tagged)


def test_seed_records_labels_for_prefill(session: Session, seeded_user: User) -> None:
    """demo_seed must learn merchant→label (not just link labels), else the shipped
    F3a Phase-2 prefill never fires in the 'Try the demo' account. Swiggy is seeded
    with #online many times (≥ LABEL_PREFILL_MIN) across the rolling window, so its
    map row must clear the prefill bar."""
    seed_demo_data(session, user_id=seeded_user.id)

    online = session.scalar(select(Label).where(Label.name == "online"))
    assert online is not None
    rows = list(
        session.scalars(select(MerchantLabelMap).where(MerchantLabelMap.label_id == online.id))
    )
    assert rows, "merchant→label map not populated — record_label never ran in the seeder"
    assert max(r.hit_count for r in rows) >= LABEL_PREFILL_MIN


def test_seed_is_find_or_create_for_accounts(session: Session, seeded_user: User) -> None:
    """Accounts are matched by name so a pre-existing demo account isn't duplicated."""
    session.add(
        Account(
            user_id=seeded_user.id,
            name="Axis Flipkart",
            type="credit_card",
            issuer="axis",
            last4="4321",
            currency="INR",
        )
    )
    session.commit()

    counts = seed_demo_data(session, user_id=seeded_user.id)

    assert counts.accounts == 2  # reused Axis Flipkart, created HDFC Savings
    axis_rows = session.scalar(
        select(func.count()).select_from(Account).where(Account.name == "Axis Flipkart")
    )
    assert axis_rows == 1


def test_seed_same_day_rerun_does_not_duplicate(session: Session, seeded_user: User) -> None:
    """Calling the seeder twice with an unchanged ``clock.today()`` must land on
    the same transaction count, not double it — the wipe-and-regenerate in
    ``seed_demo_data`` is what replaces the old empty-DB-only gate."""
    first = seed_demo_data(session, user_id=seeded_user.id)
    second = seed_demo_data(session, user_id=seeded_user.id)

    assert second.accounts == first.accounts == 2
    assert second.transactions == first.transactions == _TXN_TOTAL
    assert session.scalar(select(func.count()).select_from(Transaction)) == _TXN_TOTAL


def test_seed_rolls_the_window_forward(
    session: Session, seeded_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A boot a month later must replace the window, not accumulate on top of
    it: the oldest transaction moves forward and the total matches a fresh
    ``build_demo_dataset`` at the new anchor, not the old total plus the new one."""
    seed_demo_data(session, user_id=seeded_user.id)
    oldest_before = session.scalar(select(func.min(Transaction.date)))
    assert oldest_before is not None

    later_anchor = date(2026, 9, 20)
    monkeypatch.setattr(clock, "today", lambda: later_anchor)
    expected_spends, expected_refunds, expected_income = build_demo_dataset(later_anchor)
    expected_total = len(expected_spends) + len(expected_refunds) + len(expected_income)

    counts = seed_demo_data(session, user_id=seeded_user.id)

    assert counts.transactions == expected_total
    assert session.scalar(select(func.count()).select_from(Transaction)) == expected_total
    oldest_after = session.scalar(select(func.min(Transaction.date)))
    assert oldest_after is not None
    assert oldest_after > oldest_before  # window rolled forward, not accumulated
