"""Peer debt and Khatabook ledger service (PRD §F3b, ADR-0017).

Provides balance calculation, contact management, entry creation with
allocation guard against transactions, settlement entries, and overall summary.
"""

from __future__ import annotations

from datetime import date as date_t
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import and_, case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import clock
from app.core.db_errors import is_unique_violation
from app.core.log_config import get_logger
from app.models import PeerContact, PeerDirectionStr, PeerEntry, Transaction
from app.schemas.peer_ledger import (
    PeerContactCreate,
    PeerContactUpdate,
    PeerContactWithBalanceRead,
    PeerEntryCreate,
    PeerLedgerSummaryRead,
)

logger = get_logger(__name__)


def _is_contact_name_dup(orig: BaseException | None) -> bool:
    """Detect unique constraint violation on active peer contact name."""
    return is_unique_violation(
        orig,
        index_name="uq_peer_contacts_active_user_name",
        columns=["peer_contacts.user_id", "peer_contacts.name"],
    )


def create_contact(
    session: Session,
    *,
    user_id: UUID,
    data: PeerContactCreate,
) -> PeerContact:
    """Create a new peer contact scoped to user_id.

    Raises 409 if an active contact with the same case-insensitive name exists.
    """
    contact = PeerContact(
        user_id=user_id,
        name=data.name.strip(),
        phone=data.phone,
        upi_id=data.upi_id,
        notes=data.notes,
    )
    session.add(contact)
    try:
        session.flush()
    except IntegrityError as e:
        if _is_contact_name_dup(e.orig):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A contact with this name already exists",
            ) from e
        raise

    logger.info("peer_contact_created", user_id=str(user_id), contact_id=contact.id)
    return contact


def get_contact(
    session: Session,
    *,
    user_id: UUID,
    contact_id: int,
) -> PeerContact:
    """Fetch a single contact by id. 404 if missing or cross-user."""
    contact = session.scalar(
        select(PeerContact).where(
            PeerContact.id == contact_id,
            PeerContact.user_id == user_id,
        )
    )
    if contact is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Contact not found",
        )
    return contact


def update_contact(
    session: Session,
    *,
    user_id: UUID,
    contact_id: int,
    data: PeerContactUpdate,
) -> PeerContact:
    """Update contact attributes. 404 if missing, 409 if name conflicts."""
    contact = get_contact(session, user_id=user_id, contact_id=contact_id)
    updates = data.model_dump(exclude_unset=True)
    if "name" in updates and updates["name"] is not None:
        updates["name"] = updates["name"].strip()
    for field, value in updates.items():
        setattr(contact, field, value)

    try:
        session.flush()
    except IntegrityError as e:
        if _is_contact_name_dup(e.orig):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A contact with this name already exists",
            ) from e
        raise

    logger.info("peer_contact_updated", user_id=str(user_id), contact_id=contact.id)
    return contact


def get_contact_with_balance(
    session: Session,
    *,
    user_id: UUID,
    contact_id: int,
) -> PeerContactWithBalanceRead:
    """Fetch contact and calculate running balance dynamically (ADR-0017 rule 10).

    404 if missing or belonging to another user.
    """
    stmt = (
        select(
            PeerContact,
            func.coalesce(
                func.sum(
                    case((PeerEntry.direction == "you_gave", PeerEntry.amount_paise), else_=0)
                ),
                0,
            ).label("total_gave_paise"),
            func.coalesce(
                func.sum(case((PeerEntry.direction == "you_got", PeerEntry.amount_paise), else_=0)),
                0,
            ).label("total_got_paise"),
            func.count(PeerEntry.id).label("active_entries_count"),
        )
        .outerjoin(
            PeerEntry,
            and_(
                PeerEntry.contact_id == PeerContact.id,
                PeerEntry.user_id == user_id,
            ),
        )
        .where(
            PeerContact.id == contact_id,
            PeerContact.user_id == user_id,
        )
        .group_by(PeerContact.id)
    )
    result = session.execute(stmt).first()
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Contact not found",
        )

    contact, total_gave, total_got, count = result
    total_gave_paise = int(total_gave)
    total_got_paise = int(total_got)
    balance_paise = total_gave_paise - total_got_paise

    return PeerContactWithBalanceRead(
        id=contact.id,
        name=contact.name,
        phone=contact.phone,
        upi_id=contact.upi_id,
        notes=contact.notes,
        archived_at=contact.archived_at,
        created_at=contact.created_at,
        balance_paise=balance_paise,
        total_gave_paise=total_gave_paise,
        total_got_paise=total_got_paise,
        active_entries_count=int(count),
    )


def list_contacts_with_balances(
    session: Session,
    *,
    user_id: UUID,
    include_archived: bool = False,
) -> list[PeerContactWithBalanceRead]:
    """List contacts with dynamically computed balances in a single query."""
    stmt = (
        select(
            PeerContact,
            func.coalesce(
                func.sum(
                    case((PeerEntry.direction == "you_gave", PeerEntry.amount_paise), else_=0)
                ),
                0,
            ).label("total_gave_paise"),
            func.coalesce(
                func.sum(case((PeerEntry.direction == "you_got", PeerEntry.amount_paise), else_=0)),
                0,
            ).label("total_got_paise"),
            func.count(PeerEntry.id).label("active_entries_count"),
        )
        .outerjoin(
            PeerEntry,
            and_(
                PeerEntry.contact_id == PeerContact.id,
                PeerEntry.user_id == user_id,
            ),
        )
        .where(PeerContact.user_id == user_id)
    )
    if not include_archived:
        stmt = stmt.where(PeerContact.archived_at.is_(None))

    stmt = stmt.group_by(PeerContact.id).order_by(PeerContact.name.asc())

    rows = session.execute(stmt).all()
    out: list[PeerContactWithBalanceRead] = []
    for contact, total_gave, total_got, count in rows:
        gave_paise = int(total_gave)
        got_paise = int(total_got)
        out.append(
            PeerContactWithBalanceRead(
                id=contact.id,
                name=contact.name,
                phone=contact.phone,
                upi_id=contact.upi_id,
                notes=contact.notes,
                archived_at=contact.archived_at,
                created_at=contact.created_at,
                balance_paise=gave_paise - got_paise,
                total_gave_paise=gave_paise,
                total_got_paise=got_paise,
                active_entries_count=int(count),
            )
        )
    return out


def archive_contact(
    session: Session,
    *,
    user_id: UUID,
    contact_id: int,
    confirm_with_balance: bool = False,
) -> PeerContact:
    """Soft-archive a contact.

    Raises 400 if net balance != 0 and confirm_with_balance is False.
    """
    contact = get_contact(session, user_id=user_id, contact_id=contact_id)

    balance_paise = (
        session.scalar(
            select(
                func.coalesce(
                    func.sum(
                        case(
                            (PeerEntry.direction == "you_gave", PeerEntry.amount_paise),
                            else_=-PeerEntry.amount_paise,
                        )
                    ),
                    0,
                )
            ).where(
                PeerEntry.contact_id == contact_id,
                PeerEntry.user_id == user_id,
            )
        )
        or 0
    )

    if balance_paise != 0 and not confirm_with_balance:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Contact has outstanding balance of {balance_paise} paise. Confirmation required."
            ),
        )

    contact.archived_at = clock.naive_utcnow()
    session.flush()
    logger.info(
        "peer_contact_archived",
        user_id=str(user_id),
        contact_id=contact_id,
        balance_paise=balance_paise,
    )
    return contact


def create_entry(
    session: Session,
    *,
    user_id: UUID,
    data: PeerEntryCreate,
) -> PeerEntry:
    """Create a new debt/repayment entry in the peer ledger.

    Enforces cross-tenant validation (422) and Transaction Allocation Guard (422).
    """
    contact = session.scalar(
        select(PeerContact).where(
            PeerContact.id == data.contact_id,
            PeerContact.user_id == user_id,
        )
    )
    if contact is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Contact not found",
        )

    entry_date = data.date
    if data.transaction_id is not None:
        txn = session.scalar(
            select(Transaction).where(
                Transaction.id == data.transaction_id,
                Transaction.user_id == user_id,
            )
        )
        if txn is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Transaction not found",
            )

        existing_sum = (
            session.scalar(
                select(func.coalesce(func.sum(PeerEntry.amount_paise), 0)).where(
                    PeerEntry.user_id == user_id,
                    PeerEntry.transaction_id == data.transaction_id,
                )
            )
            or 0
        )

        max_amount = abs(txn.amount_paise)
        if existing_sum + data.amount_paise > max_amount:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    f"Total allocated paise ({existing_sum + data.amount_paise}) "
                    f"exceeds transaction amount ({max_amount})"
                ),
            )

        if entry_date is None:
            entry_date = txn.date
    elif entry_date is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="date is required for standalone entries",
        )

    entry = PeerEntry(
        user_id=user_id,
        contact_id=data.contact_id,
        transaction_id=data.transaction_id,
        date=entry_date,
        amount_paise=data.amount_paise,
        direction=data.direction,
        is_settlement=data.is_settlement,
        notes=data.notes,
    )
    session.add(entry)
    session.flush()

    logger.info(
        "peer_entry_created",
        user_id=str(user_id),
        entry_id=entry.id,
        contact_id=entry.contact_id,
        amount_paise=entry.amount_paise,
        direction=entry.direction,
    )
    return entry


def get_contact_entries(
    session: Session,
    *,
    user_id: UUID,
    contact_id: int,
) -> list[PeerEntry]:
    """List ledger entries for a contact in chronological order."""
    get_contact(session, user_id=user_id, contact_id=contact_id)
    stmt = (
        select(PeerEntry)
        .where(
            PeerEntry.contact_id == contact_id,
            PeerEntry.user_id == user_id,
        )
        .order_by(PeerEntry.date.asc(), PeerEntry.id.asc())
    )
    return list(session.scalars(stmt).all())


def delete_entry(
    session: Session,
    *,
    user_id: UUID,
    entry_id: int,
) -> None:
    """Delete an entry from the ledger. 404 if missing or cross-user."""
    entry = session.scalar(
        select(PeerEntry).where(
            PeerEntry.id == entry_id,
            PeerEntry.user_id == user_id,
        )
    )
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Entry not found",
        )
    session.delete(entry)
    session.flush()
    logger.info("peer_entry_deleted", user_id=str(user_id), entry_id=entry_id)


def settle_contact(
    session: Session,
    *,
    user_id: UUID,
    contact_id: int,
    settle_date: date_t | None = None,
    notes: str | None = None,
) -> PeerEntry:
    """Record an atomic full settlement entry for the contact's current net balance.

    Raises 400 if balance is already 0.
    """
    get_contact(session, user_id=user_id, contact_id=contact_id)

    balance_paise = (
        session.scalar(
            select(
                func.coalesce(
                    func.sum(
                        case(
                            (PeerEntry.direction == "you_gave", PeerEntry.amount_paise),
                            else_=-PeerEntry.amount_paise,
                        )
                    ),
                    0,
                )
            ).where(
                PeerEntry.contact_id == contact_id,
                PeerEntry.user_id == user_id,
            )
        )
        or 0
    )

    if balance_paise == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Contact is already settled",
        )

    direction: PeerDirectionStr = "you_got" if balance_paise > 0 else "you_gave"
    amount_paise = abs(balance_paise)
    entry_date = settle_date if settle_date is not None else clock.today()

    entry = PeerEntry(
        user_id=user_id,
        contact_id=contact_id,
        transaction_id=None,
        date=entry_date,
        amount_paise=amount_paise,
        direction=direction,
        is_settlement=True,
        notes=notes,
    )
    session.add(entry)
    session.flush()

    logger.info(
        "peer_contact_settled",
        user_id=str(user_id),
        contact_id=contact_id,
        balance_paise=balance_paise,
        settlement_entry_id=entry.id,
    )
    return entry


def get_peer_summary(
    session: Session,
    *,
    user_id: UUID,
) -> PeerLedgerSummaryRead:
    """Compute overall receivables, payables, and net balance across all active contacts."""
    contact_balances_subquery = (
        select(
            PeerContact.id.label("contact_id"),
            func.coalesce(
                func.sum(
                    case(
                        (PeerEntry.direction == "you_gave", PeerEntry.amount_paise),
                        else_=-PeerEntry.amount_paise,
                    )
                ),
                0,
            ).label("net_paise"),
        )
        .outerjoin(
            PeerEntry,
            and_(
                PeerEntry.contact_id == PeerContact.id,
                PeerEntry.user_id == user_id,
            ),
        )
        .where(
            PeerContact.user_id == user_id,
            PeerContact.archived_at.is_(None),
        )
        .group_by(PeerContact.id)
        .subquery()
    )

    summary_stmt = select(
        func.count().label("active_contacts_count"),
        func.coalesce(
            func.sum(
                case(
                    (
                        contact_balances_subquery.c.net_paise > 0,
                        contact_balances_subquery.c.net_paise,
                    ),
                    else_=0,
                )
            ),
            0,
        ).label("total_receivable_paise"),
        func.coalesce(
            func.sum(
                case(
                    (
                        contact_balances_subquery.c.net_paise < 0,
                        -contact_balances_subquery.c.net_paise,
                    ),
                    else_=0,
                )
            ),
            0,
        ).label("total_payable_paise"),
    ).select_from(contact_balances_subquery)

    row = session.execute(summary_stmt).first()
    if row is not None:
        active_count = int(row[0]) if row[0] is not None else 0
        receivable = int(row[1]) if row[1] is not None else 0
        payable = int(row[2]) if row[2] is not None else 0
    else:
        active_count = 0
        receivable = 0
        payable = 0

    return PeerLedgerSummaryRead(
        total_receivable_paise=receivable,
        total_payable_paise=payable,
        net_balance_paise=receivable - payable,
        active_contacts_count=active_count,
    )
