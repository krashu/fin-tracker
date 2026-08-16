"""Tests for guest_service — ephemeral demo sandboxes & lifecycle pruning."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import clock
from app.core.config import Settings
from app.models import (
    Account,
    Category,
    Instrument,
    InvestmentTransaction,
    Transaction,
    User,
)
from app.services.guest_service import (
    GuestCapReachedError,
    cleanup_expired_guests,
    create_guest_sandbox,
    delete_guest_sandbox,
)


def test_create_guest_sandbox_populates_isolated_data(session: Session) -> None:
    guest, refresh_token = create_guest_sandbox(session, ttl_hours=2)

    assert guest.is_guest is True
    assert guest.guest_expires_at is not None
    assert guest.guest_expires_at > clock.naive_utcnow()
    assert refresh_token is not None

    # Check categories seeded
    categories = session.scalars(select(Category).where(Category.user_id == guest.id)).all()
    assert len(categories) > 0

    # Check accounts seeded
    accounts = session.scalars(select(Account).where(Account.user_id == guest.id)).all()
    assert len(accounts) == 2

    # Check transactions seeded
    transactions = session.scalars(select(Transaction).where(Transaction.user_id == guest.id)).all()
    assert len(transactions) > 0

    # Check instruments & investment transactions seeded
    instruments = session.scalars(select(Instrument).where(Instrument.user_id == guest.id)).all()
    assert len(instruments) > 0
    inv_txns = session.scalars(
        select(InvestmentTransaction).where(InvestmentTransaction.user_id == guest.id)
    ).all()
    assert len(inv_txns) > 0


def test_delete_guest_sandbox_topological_cleanup(session: Session) -> None:
    guest, _ = create_guest_sandbox(session, ttl_hours=2)
    guest_id = guest.id

    # Verify rows exist
    assert session.scalar(select(User.id).where(User.id == guest_id)) is not None

    # Perform clean topological deletion
    delete_guest_sandbox(session, guest_id)

    # Verify all guest-owned rows are removed
    assert session.scalar(select(User.id).where(User.id == guest_id)) is None
    assert session.scalars(select(Account).where(Account.user_id == guest_id)).all() == []
    assert session.scalars(select(Transaction).where(Transaction.user_id == guest_id)).all() == []
    assert session.scalars(select(Category).where(Category.user_id == guest_id)).all() == []
    assert session.scalars(select(Instrument).where(Instrument.user_id == guest_id)).all() == []


def test_delete_guest_sandbox_refuses_non_guest_user(session: Session) -> None:
    real_user = User(
        id=uuid4(),
        email="real_owner@example.com",
        display_name="Real Owner",
        is_guest=False,
    )
    session.add(real_user)
    session.flush()

    acct = Account(
        user_id=real_user.id,
        name="Real Savings",
        type="bank",
        issuer="hdfc",
        currency="INR",
        opening_balance_paise=100000,
    )
    session.add(acct)
    session.commit()

    # Attempting to delete a real user via delete_guest_sandbox must no-op and protect data
    delete_guest_sandbox(session, real_user.id)
    assert session.scalar(select(User.id).where(User.id == real_user.id)) is not None
    assert len(session.scalars(select(Account).where(Account.user_id == real_user.id)).all()) == 1


def test_cleanup_expired_guests_prunes_only_expired(session: Session) -> None:
    # 1. Active guest (expires in 2 hours)
    active_guest, _ = create_guest_sandbox(session, ttl_hours=2)

    # 2. Expired guest (expired 1 hour ago)
    expired_guest, _ = create_guest_sandbox(session, ttl_hours=-1)

    # Run cleanup
    cleaned_count = cleanup_expired_guests(session, batch_size=10)

    assert cleaned_count >= 1
    assert session.scalar(select(User.id).where(User.id == active_guest.id)) is not None
    assert session.scalar(select(User.id).where(User.id == expired_guest.id)) is None


def test_create_guest_sandbox_enforces_cap(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Set cap to 1
    monkeypatch.setattr(
        "app.services.guest_service.get_settings", lambda: Settings(MAX_GUEST_ACCOUNTS=1)
    )

    # First guest succeeds
    create_guest_sandbox(session, ttl_hours=2)

    # Second guest fails with GuestCapReachedError
    with pytest.raises(GuestCapReachedError):
        create_guest_sandbox(session, ttl_hours=2)


def test_cleanup_expired_guests_handles_poisoned_row(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Create two expired guests
    expired1, _ = create_guest_sandbox(session, ttl_hours=-2)
    expired2, _ = create_guest_sandbox(session, ttl_hours=-2)

    # Make delete_guest_sandbox fail for expired1 only
    original_delete = delete_guest_sandbox

    def faulty_delete(s: Session, uid: UUID) -> None:
        if uid == expired1.id:
            raise RuntimeError("simulated DB failure during deletion")
        original_delete(s, uid)

    monkeypatch.setattr("app.services.guest_service.delete_guest_sandbox", faulty_delete)

    now = clock.naive_utcnow()
    cleaned = cleanup_expired_guests(session, batch_size=10)

    # One succeeded (expired2)
    assert cleaned == 1
    assert session.scalar(select(User.id).where(User.id == expired2.id)) is None

    # Poisoned row (expired1) still exists but its guest_expires_at was bumped forward ~1h
    user1 = session.get(User, expired1.id)
    assert user1 is not None
    assert user1.guest_expires_at is not None
    assert user1.guest_expires_at > now
