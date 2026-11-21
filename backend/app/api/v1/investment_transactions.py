"""Investment transaction routes (PRD §F7).

* ``GET /api/v1/investment-transactions`` — paginated read, offset/limit, flat
  shape. Optional filters: ``instrument_id``, ``transaction_type`` (repeatable),
  ``date_from`` / ``date_to``. Ordering ``(date DESC, id DESC)``.
* ``POST /api/v1/investment-transactions`` — manual entry. Pre-flights the
  instrument (same user, non-archived), server-stamps ``fx_rate_to_inr`` from the
  cached FX rate for the instrument's currency (INR → 1, USD → ``rate_on(date)``;
  422 if no USD rate is cached on/before the date), and persists the row.
  ``pair_id`` is server-managed and never set here.
* ``POST /api/v1/investment-transactions/reinvestment`` — records an Indian MF IDCW
  dividend **reinvestment** as one atomic ``dividend`` + ``buy`` pair linked by
  ``pair_id`` (both directions). The ``buy`` leg opens a real FIFO lot, which is the
  whole point: folding the units onto the dividend row would conflate income with
  acquisition and corrupt holding periods. FX is resolved ONCE for both legs.
* ``PATCH /api/v1/investment-transactions/{id}`` — note only.
* ``DELETE /api/v1/investment-transactions/{id}`` — hard delete (leaf row); nulls a
  pair partner's pointer rather than cascading.

No fingerprint / dedup: manual investment entry is deliberate, and there's no
re-import path yet (CAS dedup lands with the importer). The only IntegrityError
vector is the instrument FK, already pre-flighted on both POSTs.
"""

from __future__ import annotations

from datetime import date as date_t
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from app.api.deps import CurrentUserId, SessionDep
from app.models import (
    Instrument,
    InvestmentAccount,
    InvestmentTransaction,
    InvestmentTxnTypeStr,
    Transaction,
)
from app.schemas import (
    InvestmentTransactionCreate,
    InvestmentTransactionLink,
    InvestmentTransactionRead,
    InvestmentTransactionUpdate,
    ReinvestmentCreate,
    ReinvestmentRead,
)
from app.services.category_service import (
    get_or_create_fallback_category,
    get_or_create_investment_transfer_category,
)
from app.services.fx_service import resolve_fx_rate_to_inr
from app.services.holdings_service import OversellError, rebuild_realised_lots
from app.services.investment_account_service import get_or_create_default_investment_account
from app.services.portfolio_analytics_service import invalidate_portfolio_snapshots

router = APIRouter(prefix="/investment-transactions", tags=["investment-transactions"])


@router.get("", response_model=list[InvestmentTransactionRead])
def list_investment_transactions(
    session: SessionDep,
    user_id: CurrentUserId,
    account_id: Annotated[int | None, Query(gt=0)] = None,
    instrument_id: Annotated[int | None, Query(gt=0)] = None,
    transaction_type: Annotated[list[InvestmentTxnTypeStr] | None, Query()] = None,
    date_from: Annotated[date_t | None, Query()] = None,
    date_to: Annotated[date_t | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[InvestmentTransaction]:
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="date_from must be <= date_to",
        )
    stmt = select(InvestmentTransaction).where(InvestmentTransaction.user_id == user_id)
    if account_id is not None:
        stmt = stmt.where(InvestmentTransaction.account_id == account_id)
    if instrument_id is not None:
        stmt = stmt.where(InvestmentTransaction.instrument_id == instrument_id)
    if transaction_type:
        stmt = stmt.where(InvestmentTransaction.transaction_type.in_(transaction_type))
    if date_from is not None:
        stmt = stmt.where(InvestmentTransaction.date >= date_from)
    if date_to is not None:
        stmt = stmt.where(InvestmentTransaction.date <= date_to)
    stmt = (
        stmt.order_by(InvestmentTransaction.date.desc(), InvestmentTransaction.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(session.scalars(stmt))


def _resolve_account_id(
    session: SessionDep,
    *,
    user_id: UUID,
    account_id: int | None,
) -> int:
    if account_id is not None:
        acc = session.scalar(
            select(InvestmentAccount).where(
                InvestmentAccount.id == account_id,
                InvestmentAccount.user_id == user_id,
                InvestmentAccount.is_active.is_(True),
            )
        )
        if acc is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="investment account not found or inactive",
            )
        return acc.id
    return get_or_create_default_investment_account(session, user_id=user_id).id


def _resolve_instrument_and_fx(
    session: SessionDep,
    *,
    user_id: UUID,
    instrument_id: int,
    on: date_t,
) -> tuple[Instrument, Decimal]:
    """Shared write-path pre-flight: instrument ownership + the FX stamp.

    Both creation routes need exactly this, and the reinvestment pair needs the rate
    resolved ONCE so its two legs cannot be stamped differently — that invariant is
    structural here rather than a comment at the call site.
    """
    # Instrument pre-flight: ownership + non-archived. The FK alone proves the
    # row exists, not who owns it — load-bearing for v2 multi-user.
    instrument = session.scalar(
        select(Instrument).where(
            Instrument.id == instrument_id,
            Instrument.user_id == user_id,
            Instrument.archived_at.is_(None),
        )
    )
    if instrument is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="instrument not found or archived",
        )

    # Server-stamp the FX rate from the instrument's currency: INR → 1 (no fx_rates touch),
    # USD → the historical rate_on(date). A USD instrument with no cached rate at-or-before the
    # date → 422 (seed via POST /fx/refresh) rather than mis-stamp 1.
    fx_rate = resolve_fx_rate_to_inr(session, currency=instrument.currency, on=on)
    if fx_rate is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="no USD/INR rate cached on/before the transaction date — run POST /fx/refresh",
        )
    return instrument, fx_rate


def _link_spend_transaction(
    session: SessionDep,
    *,
    user_id: UUID,
    spend_id: int,
    inv_txn_type: str,
) -> int:
    """Validate spend transaction and map it to system:investment_transfer category."""
    spend_txn = session.scalar(
        select(Transaction).where(
            Transaction.id == spend_id,
            Transaction.user_id == user_id,
        )
    )
    if spend_txn is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="linked spend transaction not found",
        )
    if spend_txn.amount_paise >= 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="linked spend transaction must be an outgoing debit",
        )
    if inv_txn_type not in ("buy", "sip"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="only buy/sip investment transactions can be linked to a spend debit",
        )
    inv_cat = get_or_create_investment_transfer_category(session, user_id=user_id)
    spend_txn.category_id = inv_cat.id
    return spend_txn.id


def _unlink_spend_transaction(
    session: SessionDep,
    *,
    user_id: UUID,
    inv_txn: InvestmentTransaction,
) -> None:
    """Clear spend link on inv_txn and revert spend transaction category to Other if no
    remaining links."""
    old_spend_id = inv_txn.linked_spend_txn_id
    if old_spend_id is None:
        return
    inv_txn.linked_spend_txn_id = None
    # Check if any other investment transaction still links to old_spend_id
    other_link = session.scalar(
        select(InvestmentTransaction.id).where(
            InvestmentTransaction.user_id == user_id,
            InvestmentTransaction.linked_spend_txn_id == old_spend_id,
            InvestmentTransaction.id != inv_txn.id,
        )
    )
    if other_link is None:
        spend_txn = session.scalar(
            select(Transaction).where(
                Transaction.id == old_spend_id,
                Transaction.user_id == user_id,
            )
        )
        if spend_txn is not None:
            inv_cat = get_or_create_investment_transfer_category(session, user_id=user_id)
            if spend_txn.category_id == inv_cat.id:
                fallback_cat = get_or_create_fallback_category(
                    session, user_id=user_id, kind="spend"
                )
                spend_txn.category_id = fallback_cat.id


@router.post("", response_model=InvestmentTransactionRead, status_code=status.HTTP_201_CREATED)
def create_investment_transaction(
    payload: InvestmentTransactionCreate,
    session: SessionDep,
    user_id: CurrentUserId,
) -> InvestmentTransaction:
    _, fx_rate = _resolve_instrument_and_fx(
        session, user_id=user_id, instrument_id=payload.instrument_id, on=payload.date
    )
    target_account_id = _resolve_account_id(session, user_id=user_id, account_id=payload.account_id)

    linked_spend_id: int | None = None
    if payload.linked_spend_txn_id is not None:
        linked_spend_id = _link_spend_transaction(
            session,
            user_id=user_id,
            spend_id=payload.linked_spend_txn_id,
            inv_txn_type=payload.transaction_type,
        )

    txn = InvestmentTransaction(
        user_id=user_id,
        account_id=target_account_id,
        instrument_id=payload.instrument_id,
        date=payload.date,
        transaction_type=payload.transaction_type,
        units=payload.units,
        price_per_unit_native=payload.price_per_unit_native,
        amount_native_paise=payload.amount_native_paise,
        fees_native_paise=payload.fees_native_paise,
        fx_rate_to_inr=fx_rate,
        note=payload.note,
        pair_id=None,
        linked_spend_txn_id=linked_spend_id,
    )
    session.add(txn)
    # ADR-0015 rules 5 and 9: rebuild the instrument's realised lots, and refuse the write
    # if any sell/switch_out (this one, or a later one it strips of lots) is left uncovered.
    # Strict: selling exactly what is held is valid (position goes to 0).
    try:
        rebuild_realised_lots(session, user_id=user_id, instrument_id=payload.instrument_id)
    except OversellError:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="sell/switch_out exceeds available units for this instrument",
        ) from None
    invalidate_portfolio_snapshots(session, user_id=user_id, from_date=payload.date)
    session.commit()
    session.refresh(txn)
    return txn


@router.post(
    "/reinvestment",
    response_model=ReinvestmentRead,
    status_code=status.HTTP_201_CREATED,
)
def create_reinvestment(
    payload: ReinvestmentCreate,
    session: SessionDep,
    user_id: CurrentUserId,
) -> dict[str, InvestmentTransaction]:
    """Record an IDCW dividend-reinvestment as a linked ``dividend`` + ``buy`` pair.

    Mirrors ``POST /transactions/transfer``: one atomic call writes both legs, so the
    pair invariant is enforceable and ``pair_id`` never becomes client-settable.

    Neither leg consumes units (the dividend is unit-neutral, the buy adds), so the
    rule 9 coverage check inside the rebuild cannot fail here. No ``IntegrityError`` handling
    either, unlike ``create_transfer``: manual investment rows carry ``fingerprint = NULL``
    and NULLs are distinct under the unique index, and the only other vector — the
    instrument FK — is pre-flighted below.

    Reinvestment is economically two events on one date, and XIRR sees them net to zero
    (``+amount`` for the payout, ``-amount`` for the acquisition), which is correct: no
    money entered or left the portfolio. What it is NOT is unit-neutral, which is exactly
    why the ``buy`` leg has to be a real row.
    """
    _, fx_rate = _resolve_instrument_and_fx(
        session, user_id=user_id, instrument_id=payload.instrument_id, on=payload.date
    )
    target_account_id = _resolve_account_id(session, user_id=user_id, account_id=payload.account_id)

    common = {
        "user_id": user_id,
        "account_id": target_account_id,
        "instrument_id": payload.instrument_id,
        "date": payload.date,
        "amount_native_paise": payload.amount_native_paise,
        # A reinvestment carries no brokerage; the schema forbids a client fee.
        "fees_native_paise": 0,
        "fx_rate_to_inr": fx_rate,
        "note": payload.note,
    }
    dividend = InvestmentTransaction(
        **common,
        transaction_type="dividend",
        units=Decimal(0),
        price_per_unit_native=None,
    )
    buy = InvestmentTransaction(
        **common,
        transaction_type="buy",
        units=payload.units,
        price_per_unit_native=payload.price_per_unit_native,
    )
    # Dividend added first so it takes the lower id: same-date order is source order,
    # carried by insertion order (ADR-0015 rule 3), so a listing reads income → acquisition.
    session.add_all([dividend, buy])
    session.flush()  # assign ids

    # BOTH directions, in this transaction — the model docstring's writer contract.
    # The delete path keys on ``txn.pair_id``, so a one-directional link would dangle
    # when the pointed-at row is deleted first.
    dividend.pair_id = buy.id
    buy.pair_id = dividend.id
    session.flush()  # composite same-user FK + no-self-pair CHECK validate here

    # Adds a dividend and a buy, so it cannot uncover a sell; same refusal path regardless.
    try:
        rebuild_realised_lots(session, user_id=user_id, instrument_id=payload.instrument_id)
    except OversellError:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="sell/switch_out exceeds available units for this instrument",
        ) from None
    invalidate_portfolio_snapshots(session, user_id=user_id, from_date=payload.date)
    session.commit()
    session.refresh(dividend)
    session.refresh(buy)
    return {"dividend": dividend, "buy": buy}


@router.patch("/{transaction_id}", response_model=InvestmentTransactionRead)
def update_investment_transaction(
    transaction_id: int,
    payload: InvestmentTransactionUpdate,
    session: SessionDep,
    user_id: CurrentUserId,
) -> InvestmentTransaction:
    txn = session.scalar(
        select(InvestmentTransaction).where(
            InvestmentTransaction.id == transaction_id,
            InvestmentTransaction.user_id == user_id,
        )
    )
    if txn is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="investment transaction not found",
        )
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        # Empty body — no DB round-trip, no spurious updated_at bump.
        return txn

    if "linked_spend_txn_id" in updates:
        target_spend_id = updates.pop("linked_spend_txn_id")
        if target_spend_id is not None:
            _link_spend_transaction(
                session,
                user_id=user_id,
                spend_id=target_spend_id,
                inv_txn_type=txn.transaction_type,
            )
            txn.linked_spend_txn_id = target_spend_id
        else:
            _unlink_spend_transaction(session, user_id=user_id, inv_txn=txn)

    for field, value in updates.items():
        setattr(txn, field, value)
    invalidate_portfolio_snapshots(session, user_id=user_id, from_date=txn.date)
    session.commit()
    session.refresh(txn)
    return txn


@router.post("/{transaction_id}/link", response_model=InvestmentTransactionRead)
def link_investment_transaction(
    transaction_id: int,
    payload: InvestmentTransactionLink,
    session: SessionDep,
    user_id: CurrentUserId,
) -> InvestmentTransaction:
    """Explicitly link an investment buy/sip transaction to an outgoing bank debit."""
    txn = session.scalar(
        select(InvestmentTransaction).where(
            InvestmentTransaction.id == transaction_id,
            InvestmentTransaction.user_id == user_id,
        )
    )
    if txn is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="investment transaction not found",
        )
    linked_id = _link_spend_transaction(
        session,
        user_id=user_id,
        spend_id=payload.spend_transaction_id,
        inv_txn_type=txn.transaction_type,
    )
    txn.linked_spend_txn_id = linked_id
    session.commit()
    session.refresh(txn)
    return txn


@router.post("/{transaction_id}/unlink", response_model=InvestmentTransactionRead)
def unlink_investment_transaction(
    transaction_id: int,
    session: SessionDep,
    user_id: CurrentUserId,
) -> InvestmentTransaction:
    """Explicitly unlink an investment transaction from any linked bank debit."""
    txn = session.scalar(
        select(InvestmentTransaction).where(
            InvestmentTransaction.id == transaction_id,
            InvestmentTransaction.user_id == user_id,
        )
    )
    if txn is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="investment transaction not found",
        )
    _unlink_spend_transaction(session, user_id=user_id, inv_txn=txn)
    session.commit()
    session.refresh(txn)
    return txn


@router.delete("/{transaction_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_investment_transaction(
    transaction_id: int,
    session: SessionDep,
    user_id: CurrentUserId,
) -> None:
    """Hard delete (leaf row — corrections are DELETE + re-create).

    ``pair_id`` null-out mirrors the spend ``delete_transaction``: if this row is half
    of a pair, null the partner first so the composite FK isn't tripped.
    Spend seam link is unlinked before row deletion.
    """
    txn = session.scalar(
        select(InvestmentTransaction).where(
            InvestmentTransaction.id == transaction_id,
            InvestmentTransaction.user_id == user_id,
        )
    )
    if txn is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="investment transaction not found",
        )

    _unlink_spend_transaction(session, user_id=user_id, inv_txn=txn)

    if txn.pair_id is not None:
        partner = session.scalar(
            select(InvestmentTransaction).where(
                InvestmentTransaction.id == txn.pair_id,
                InvestmentTransaction.user_id == user_id,
            )
        )
        if partner is not None:
            partner.pair_id = None

    instrument_id = txn.instrument_id
    txn_date = txn.date
    session.delete(txn)
    # The rebuild flushes first, so the partner UPDATE and this DELETE go out (and the DB
    # cascade clears this row's realised lots) before the replay. Deleting an acquisition
    # can strip a later sell of its lots (ADR-0015 rule 9): refuse, nothing persists.
    try:
        rebuild_realised_lots(session, user_id=user_id, instrument_id=instrument_id)
    except OversellError:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "deleting this transaction would leave a sell/switch_out "
                "without enough units at its date"
            ),
        ) from None
    invalidate_portfolio_snapshots(session, user_id=user_id, from_date=txn_date)
    session.commit()
    return None
