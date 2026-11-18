"""Tests for peer ledger service (PRD §F3b, ADR-0017)."""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.models import Account, Transaction, User
from app.schemas.peer_ledger import (
    PeerContactCreate,
    PeerEntryCreate,
)
from app.services import peer_service


def _make_account(session: Session, user: User) -> Account:
    acct = Account(
        user_id=user.id,
        name="Test Card",
        type="credit_card",
        issuer="axis",
        last4="9999",
    )
    session.add(acct)
    session.flush()
    return acct


def _make_transaction(
    session: Session, user: User, account: Account, amount_paise: int
) -> Transaction:
    txn = Transaction(
        user_id=user.id,
        account_id=account.id,
        date=date(2026, 3, 10),
        amount_paise=amount_paise,
        transaction_type="spend",
        merchant_raw="Group Dinner",
        merchant_normalized="group dinner",
        fingerprint="bb" * 32,
        source="manual",
    )
    session.add(txn)
    session.flush()
    return txn


def test_contact_dynamic_balance_calculation(session: Session, user: User) -> None:
    """Net balance is dynamically computed as Σ(you_gave) − Σ(you_got) (ADR-0017 rule 3, 10)."""
    contact = peer_service.create_contact(
        session,
        user_id=user.id,
        data=PeerContactCreate(name="Alice"),
    )

    # Initially zero
    bal = peer_service.get_contact_with_balance(session, user_id=user.id, contact_id=contact.id)
    assert bal.balance_paise == 0
    assert bal.total_gave_paise == 0
    assert bal.total_got_paise == 0
    assert bal.active_entries_count == 0

    # User lent ₹50 (5000 paise)
    peer_service.create_entry(
        session,
        user_id=user.id,
        data=PeerEntryCreate(
            contact_id=contact.id,
            date=date(2026, 3, 1),
            amount_paise=5000,
            direction="you_gave",
        ),
    )
    bal = peer_service.get_contact_with_balance(session, user_id=user.id, contact_id=contact.id)
    assert bal.balance_paise == 5000
    assert bal.total_gave_paise == 5000
    assert bal.total_got_paise == 0
    assert bal.active_entries_count == 1

    # Alice gave back ₹20 (2000 paise)
    peer_service.create_entry(
        session,
        user_id=user.id,
        data=PeerEntryCreate(
            contact_id=contact.id,
            date=date(2026, 3, 2),
            amount_paise=2000,
            direction="you_got",
        ),
    )
    bal = peer_service.get_contact_with_balance(session, user_id=user.id, contact_id=contact.id)
    assert bal.balance_paise == 3000
    assert bal.total_gave_paise == 5000
    assert bal.total_got_paise == 2000
    assert bal.active_entries_count == 2

    # Alice paid for user ₹40 (4000 paise) -> net flips negative (user owes Alice)
    peer_service.create_entry(
        session,
        user_id=user.id,
        data=PeerEntryCreate(
            contact_id=contact.id,
            date=date(2026, 3, 3),
            amount_paise=4000,
            direction="you_got",
        ),
    )
    bal = peer_service.get_contact_with_balance(session, user_id=user.id, contact_id=contact.id)
    assert bal.balance_paise == -1000
    assert bal.total_gave_paise == 5000
    assert bal.total_got_paise == 6000
    assert bal.active_entries_count == 3


def test_transaction_allocation_guard(session: Session, user: User) -> None:
    """Linked entries sum cannot exceed transaction magnitude (ADR-0017 rule 4)."""
    acct = _make_account(session, user)
    txn = _make_transaction(session, user, acct, amount_paise=-10000)  # ₹100 spend

    bob = peer_service.create_contact(session, user_id=user.id, data=PeerContactCreate(name="Bob"))
    charlie = peer_service.create_contact(
        session, user_id=user.id, data=PeerContactCreate(name="Charlie")
    )

    # Bob allocated ₹60 (6000 paise)
    e1 = peer_service.create_entry(
        session,
        user_id=user.id,
        data=PeerEntryCreate(
            contact_id=bob.id,
            transaction_id=txn.id,
            amount_paise=6000,
            direction="you_gave",
        ),
    )
    # Entry date auto-seeded from transaction date
    assert e1.date == txn.date

    # Charlie allocated remaining ₹40 (4000 paise)
    peer_service.create_entry(
        session,
        user_id=user.id,
        data=PeerEntryCreate(
            contact_id=charlie.id,
            transaction_id=txn.id,
            amount_paise=4000,
            direction="you_gave",
        ),
    )

    # Additional allocation of even 1 paisa fails with 422
    with pytest.raises(HTTPException) as exc_info:
        peer_service.create_entry(
            session,
            user_id=user.id,
            data=PeerEntryCreate(
                contact_id=bob.id,
                transaction_id=txn.id,
                amount_paise=1,
                direction="you_gave",
            ),
        )
    assert exc_info.value.status_code == 422
    assert "exceeds transaction amount" in exc_info.value.detail


def test_standalone_entry_date_handling(session: Session, user: User) -> None:
    """Standalone entry requires date; linked entry can omit date."""
    contact = peer_service.create_contact(
        session, user_id=user.id, data=PeerContactCreate(name="Dan")
    )

    # Standalone with date succeeds
    entry = peer_service.create_entry(
        session,
        user_id=user.id,
        data=PeerEntryCreate(
            contact_id=contact.id,
            date=date(2026, 3, 5),
            amount_paise=1000,
            direction="you_gave",
        ),
    )
    assert entry.date == date(2026, 3, 5)

    # Standalone without date fails validation
    with pytest.raises(ValidationError) as exc:
        PeerEntryCreate(
            contact_id=contact.id,
            transaction_id=None,
            date=None,
            amount_paise=1000,
            direction="you_gave",
        )
    assert "date is required for standalone entries" in str(exc.value)


def test_settle_positive_balance(session: Session, user: User) -> None:
    """Settling positive balance generates 'you_got' entry and zeroes net balance."""
    contact = peer_service.create_contact(
        session, user_id=user.id, data=PeerContactCreate(name="Dave")
    )
    peer_service.create_entry(
        session,
        user_id=user.id,
        data=PeerEntryCreate(
            contact_id=contact.id,
            date=date(2026, 3, 1),
            amount_paise=3000,
            direction="you_gave",
        ),
    )

    settlement = peer_service.settle_contact(
        session,
        user_id=user.id,
        contact_id=contact.id,
        settle_date=date(2026, 3, 5),
        notes="UPI repayment",
    )
    assert settlement.direction == "you_got"
    assert settlement.amount_paise == 3000
    assert settlement.is_settlement is True
    assert settlement.date == date(2026, 3, 5)
    assert settlement.notes == "UPI repayment"

    bal = peer_service.get_contact_with_balance(session, user_id=user.id, contact_id=contact.id)
    assert bal.balance_paise == 0


def test_settle_negative_balance(session: Session, user: User) -> None:
    """Settling negative balance generates 'you_gave' entry and zeroes net balance."""
    contact = peer_service.create_contact(
        session, user_id=user.id, data=PeerContactCreate(name="Eve")
    )
    peer_service.create_entry(
        session,
        user_id=user.id,
        data=PeerEntryCreate(
            contact_id=contact.id,
            date=date(2026, 3, 1),
            amount_paise=4500,
            direction="you_got",
        ),
    )

    settlement = peer_service.settle_contact(
        session,
        user_id=user.id,
        contact_id=contact.id,
    )
    assert settlement.direction == "you_gave"
    assert settlement.amount_paise == 4500
    assert settlement.is_settlement is True

    bal = peer_service.get_contact_with_balance(session, user_id=user.id, contact_id=contact.id)
    assert bal.balance_paise == 0


def test_settle_zero_balance_rejected(session: Session, user: User) -> None:
    """Settling a zero balance contact raises 400."""
    contact = peer_service.create_contact(
        session, user_id=user.id, data=PeerContactCreate(name="Frank")
    )
    with pytest.raises(HTTPException) as exc_info:
        peer_service.settle_contact(session, user_id=user.id, contact_id=contact.id)
    assert exc_info.value.status_code == 400
    assert "already settled" in exc_info.value.detail


def test_archive_contact_with_balance_requires_confirmation(session: Session, user: User) -> None:
    """Archiving a contact with non-zero balance requires confirmation (ADR-0017 rule 9)."""
    contact = peer_service.create_contact(
        session, user_id=user.id, data=PeerContactCreate(name="Grace")
    )
    peer_service.create_entry(
        session,
        user_id=user.id,
        data=PeerEntryCreate(
            contact_id=contact.id,
            date=date(2026, 3, 1),
            amount_paise=10000,
            direction="you_gave",
        ),
    )

    # Without confirmation -> 400
    with pytest.raises(HTTPException) as exc_info:
        peer_service.archive_contact(
            session,
            user_id=user.id,
            contact_id=contact.id,
            confirm_with_balance=False,
        )
    assert exc_info.value.status_code == 400
    assert (
        "Contact has outstanding balance of 10000 paise. Confirmation required."
        in exc_info.value.detail
    )

    # With confirmation -> succeeds
    archived = peer_service.archive_contact(
        session,
        user_id=user.id,
        contact_id=contact.id,
        confirm_with_balance=True,
    )
    assert archived.archived_at is not None


def test_archive_zero_balance_contact(session: Session, user: User) -> None:
    """Archiving a contact with zero balance succeeds without confirmation."""
    contact = peer_service.create_contact(
        session, user_id=user.id, data=PeerContactCreate(name="Heidi")
    )
    archived = peer_service.archive_contact(
        session,
        user_id=user.id,
        contact_id=contact.id,
        confirm_with_balance=False,
    )
    assert archived.archived_at is not None


def test_peer_summary_calculation(session: Session, user: User) -> None:
    """Summary aggregates active contacts' positive (receivable) and negative (payable) balances."""
    # Contact 1: owes user ₹50 (5000 paise)
    c1 = peer_service.create_contact(
        session, user_id=user.id, data=PeerContactCreate(name="Contact1")
    )
    peer_service.create_entry(
        session,
        user_id=user.id,
        data=PeerEntryCreate(
            contact_id=c1.id,
            date=date(2026, 3, 1),
            amount_paise=5000,
            direction="you_gave",
        ),
    )

    # Contact 2: user owes ₹20 (2000 paise)
    c2 = peer_service.create_contact(
        session, user_id=user.id, data=PeerContactCreate(name="Contact2")
    )
    peer_service.create_entry(
        session,
        user_id=user.id,
        data=PeerEntryCreate(
            contact_id=c2.id,
            date=date(2026, 3, 1),
            amount_paise=2000,
            direction="you_got",
        ),
    )

    # Contact 3: zero balance
    peer_service.create_contact(session, user_id=user.id, data=PeerContactCreate(name="Contact3"))

    # Contact 4: archived with positive balance (must be excluded from active summary)
    c4 = peer_service.create_contact(
        session, user_id=user.id, data=PeerContactCreate(name="Contact4")
    )
    peer_service.create_entry(
        session,
        user_id=user.id,
        data=PeerEntryCreate(
            contact_id=c4.id,
            date=date(2026, 3, 1),
            amount_paise=8000,
            direction="you_gave",
        ),
    )
    peer_service.archive_contact(
        session, user_id=user.id, contact_id=c4.id, confirm_with_balance=True
    )

    summary = peer_service.get_peer_summary(session, user_id=user.id)
    assert summary.active_contacts_count == 3
    assert summary.total_receivable_paise == 5000
    assert summary.total_payable_paise == 2000
    assert summary.net_balance_paise == 3000


def test_delete_entry_updates_balance(session: Session, user: User) -> None:
    """Deleting an entry updates the dynamically computed balance."""
    contact = peer_service.create_contact(
        session, user_id=user.id, data=PeerContactCreate(name="Ivan")
    )
    entry = peer_service.create_entry(
        session,
        user_id=user.id,
        data=PeerEntryCreate(
            contact_id=contact.id,
            date=date(2026, 3, 1),
            amount_paise=7000,
            direction="you_gave",
        ),
    )
    bal = peer_service.get_contact_with_balance(session, user_id=user.id, contact_id=contact.id)
    assert bal.balance_paise == 7000

    peer_service.delete_entry(session, user_id=user.id, entry_id=entry.id)
    bal_after = peer_service.get_contact_with_balance(
        session, user_id=user.id, contact_id=contact.id
    )
    assert bal_after.balance_paise == 0
    assert bal_after.active_entries_count == 0


def test_contact_name_collision(session: Session, user: User) -> None:
    """Duplicate active contact name raises 409 conflict."""
    peer_service.create_contact(session, user_id=user.id, data=PeerContactCreate(name="John Doe"))
    with pytest.raises(HTTPException) as exc:
        peer_service.create_contact(
            session, user_id=user.id, data=PeerContactCreate(name="  john doe  ")
        )
    assert exc.value.status_code == 409


def test_cross_tenant_isolation(session: Session, user: User) -> None:
    """Cannot fetch or manipulate another user's contact or entries."""
    other_user_id = uuid.uuid4()
    other_user = User(id=other_user_id)
    session.add(other_user)
    session.flush()

    contact = peer_service.create_contact(
        session, user_id=user.id, data=PeerContactCreate(name="My Friend")
    )

    # Other user cannot access contact
    with pytest.raises(HTTPException) as exc:
        peer_service.get_contact_with_balance(session, user_id=other_user.id, contact_id=contact.id)
    assert exc.value.status_code == 404

    # Other user cannot link an entry to user's contact (422)
    with pytest.raises(HTTPException) as exc:
        peer_service.create_entry(
            session,
            user_id=other_user.id,
            data=PeerEntryCreate(
                contact_id=contact.id,
                date=date(2026, 3, 1),
                amount_paise=1000,
                direction="you_gave",
            ),
        )
    assert exc.value.status_code == 422
