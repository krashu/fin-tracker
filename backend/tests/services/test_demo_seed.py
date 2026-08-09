"""Service-level tests for the direct-DB demo seeder (app.services.demo_seed).

Exercises :func:`seed_demo_data` against an in-memory session — no TestClient,
no lifespan. The empty-check gate that guards it lives in the app lifespan and is
covered separately in ``tests/test_startup_seed.py``.

Locks the behaviours the frontend/demo depend on: correct row counts, labels
written from the shared dataset, PRD §F4 fingerprints unique across seeded rows,
and F3 tag learning fired for spend/refund only (not income).
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.demo_data import BANK_INCOME, CARD_REFUNDS, CARD_SPENDS, INSTRUMENTS
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

_TXN_TOTAL = len(CARD_SPENDS) + len(CARD_REFUNDS) + len(BANK_INCOME)
_INV_TXN_TOTAL = sum(len(spec["txns"]) for spec in INSTRUMENTS)


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
    with #online many times (≥ LABEL_PREFILL_MIN), so its map row must clear the
    prefill bar."""
    seed_demo_data(session, user_id=seeded_user.id)

    online = session.scalar(select(Label).where(Label.name == "online"))
    assert online is not None
    rows = list(
        session.scalars(select(MerchantLabelMap).where(MerchantLabelMap.label_id == online.id))
    )
    assert rows, "merchant→label map not populated — record_label never ran in the seeder"
    assert max(r.hit_count for r in rows) >= LABEL_PREFILL_MIN


def test_seed_is_find_or_create_for_accounts(session: Session, seeded_user: User) -> None:
    """Accounts are matched by name so a pre-existing demo account isn't duplicated
    (the seeder itself only runs on an empty DB, but the guard is still correct)."""
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
