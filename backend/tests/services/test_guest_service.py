"""Tests for guest_service — ephemeral demo sandboxes & lifecycle pruning."""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import clock
from app.models import (
    Account,
    Category,
    Instrument,
    InvestmentTransaction,
    Transaction,
    User,
)
from app.services.guest_service import (
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
    session.commit()

    # Attempting to delete a real user via delete_guest_sandbox must no-op and protect data
    delete_guest_sandbox(session, real_user.id)
    assert session.scalar(select(User.id).where(User.id == real_user.id)) is not None


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
