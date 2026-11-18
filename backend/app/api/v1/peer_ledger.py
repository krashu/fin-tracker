"""Peer debt & Khata API routes (PRD §F3b, ADR-0017)."""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from app.api.deps import CurrentUserId, SessionDep
from app.models import PeerContact, PeerEntry
from app.schemas.peer_ledger import (
    PeerContactCreate,
    PeerContactRead,
    PeerContactUpdate,
    PeerContactWithBalanceRead,
    PeerEntryCreate,
    PeerEntryRead,
    PeerLedgerSummaryRead,
    PeerSettleRequest,
)
from app.services import peer_service

router = APIRouter(prefix="/friends", tags=["friends"])


@router.get("/summary", response_model=PeerLedgerSummaryRead)
def get_peer_summary(
    session: SessionDep,
    user_id: CurrentUserId,
) -> PeerLedgerSummaryRead:
    """Compute overall receivables, payables, and net balance across active contacts."""
    return peer_service.get_peer_summary(session, user_id=user_id)


@router.get("/contacts", response_model=list[PeerContactWithBalanceRead])
def list_contacts(
    session: SessionDep,
    user_id: CurrentUserId,
    include_archived: bool = False,
) -> list[PeerContactWithBalanceRead]:
    """List peer contacts with their running balances."""
    return peer_service.list_contacts_with_balances(
        session,
        user_id=user_id,
        include_archived=include_archived,
    )


@router.post("/contacts", response_model=PeerContactRead, status_code=status.HTTP_201_CREATED)
def create_contact(
    payload: PeerContactCreate,
    session: SessionDep,
    user_id: CurrentUserId,
) -> PeerContact:
    """Create a new peer contact."""
    contact = peer_service.create_contact(session, user_id=user_id, data=payload)
    session.commit()
    session.refresh(contact)
    return contact


@router.get("/contacts/{contact_id}", response_model=PeerContactWithBalanceRead)
def get_contact_with_balance(
    contact_id: int,
    session: SessionDep,
    user_id: CurrentUserId,
) -> PeerContactWithBalanceRead:
    """Fetch a single contact with running balance."""
    return peer_service.get_contact_with_balance(session, user_id=user_id, contact_id=contact_id)


@router.get("/contacts/{contact_id}/export")
def export_contact_statement(
    contact_id: int,
    session: SessionDep,
    user_id: CurrentUserId,
) -> Response:
    """Export contact ledger statement as an itemised CSV."""
    csv_content = peer_service.export_contact_statement_csv(
        session, user_id=user_id, contact_id=contact_id
    )
    filename = f"peer_contact_{contact_id}_statement.csv"
    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.patch("/contacts/{contact_id}", response_model=PeerContactRead)
def update_contact(
    contact_id: int,
    payload: PeerContactUpdate,
    session: SessionDep,
    user_id: CurrentUserId,
) -> PeerContact:
    """Update contact details."""
    contact = peer_service.update_contact(
        session, user_id=user_id, contact_id=contact_id, data=payload
    )
    session.commit()
    session.refresh(contact)
    return contact


@router.delete("/contacts/{contact_id}", response_model=PeerContactRead)
def archive_contact(
    contact_id: int,
    session: SessionDep,
    user_id: CurrentUserId,
    confirm_with_balance: bool = False,
) -> PeerContact:
    """Soft-archive a contact."""
    contact = peer_service.archive_contact(
        session,
        user_id=user_id,
        contact_id=contact_id,
        confirm_with_balance=confirm_with_balance,
    )
    session.commit()
    session.refresh(contact)
    return contact


@router.post("/contacts/{contact_id}/settle", response_model=PeerEntryRead)
def settle_contact(
    contact_id: int,
    session: SessionDep,
    user_id: CurrentUserId,
    payload: PeerSettleRequest | None = None,
) -> PeerEntry:
    """Record a settlement entry for the contact's outstanding net balance."""
    req = payload or PeerSettleRequest()
    entry = peer_service.settle_contact(
        session,
        user_id=user_id,
        contact_id=contact_id,
        settle_date=req.date,
        notes=req.notes,
    )
    session.commit()
    session.refresh(entry)
    return entry


@router.get("/contacts/{contact_id}/entries", response_model=list[PeerEntryRead])
def get_contact_entries(
    contact_id: int,
    session: SessionDep,
    user_id: CurrentUserId,
) -> list[PeerEntry]:
    """List ledger entries for a contact chronologically."""
    return peer_service.get_contact_entries(session, user_id=user_id, contact_id=contact_id)


@router.post("/entries", response_model=PeerEntryRead, status_code=status.HTTP_201_CREATED)
def create_entry(
    payload: PeerEntryCreate,
    session: SessionDep,
    user_id: CurrentUserId,
) -> PeerEntry:
    """Create a new debt or repayment entry."""
    entry = peer_service.create_entry(session, user_id=user_id, data=payload)
    session.commit()
    session.refresh(entry)
    return entry


@router.delete("/entries/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_entry(
    entry_id: int,
    session: SessionDep,
    user_id: CurrentUserId,
) -> Response:
    """Delete an entry from the ledger."""
    peer_service.delete_entry(session, user_id=user_id, entry_id=entry_id)
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
