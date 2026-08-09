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
from app.models import Instrument, InvestmentTransaction, InvestmentTxnTypeStr
from app.schemas import (
    InvestmentTransactionCreate,
    InvestmentTransactionRead,
    InvestmentTransactionUpdate,
    ReinvestmentCreate,
    ReinvestmentRead,
)
from app.services.fx_service import resolve_fx_rate_to_inr
from app.services.holdings_service import UNIT_SIGN, available_units

router = APIRouter(prefix="/investment-transactions", tags=["investment-transactions"])


@router.get("", response_model=list[InvestmentTransactionRead])
def list_investment_transactions(
    session: SessionDep,
    user_id: CurrentUserId,
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


@router.post("", response_model=InvestmentTransactionRead, status_code=status.HTTP_201_CREATED)
def create_investment_transaction(
    payload: InvestmentTransactionCreate,
    session: SessionDep,
    user_id: CurrentUserId,
) -> InvestmentTransaction:
    _, fx_rate = _resolve_instrument_and_fx(
        session, user_id=user_id, instrument_id=payload.instrument_id, on=payload.date
    )

    # Oversell guard: a unit-removing type (sell / switch_out, per ``UNIT_SIGN``) may
    # not exceed the units currently held.
    # The read-model (_consume_fifo) clamps bad *stored* data so it never crashes,
    # but a manual entry must be rejected at the boundary so holdings / XIRR aren't
    # built on an impossible position. Strict `>` — selling the exact holding is
    # valid (position → 0).
    if UNIT_SIGN.get(payload.transaction_type, 0) < 0:
        held = available_units(session, user_id=user_id, instrument_id=payload.instrument_id)
        if payload.units > held:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="sell/switch_out exceeds available units for this instrument",
            )

    txn = InvestmentTransaction(
        user_id=user_id,
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
    )
    session.add(txn)
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

    No oversell guard — neither leg consumes units (the dividend is unit-neutral, the
    buy adds). No ``IntegrityError`` handling either, unlike ``create_transfer``: manual
    investment rows carry ``fingerprint = NULL`` and NULLs are distinct under the unique
    index, and the only other vector — the instrument FK — is pre-flighted below.

    Reinvestment is economically two events on one date, and XIRR sees them net to zero
    (``+amount`` for the payout, ``-amount`` for the acquisition), which is correct: no
    money entered or left the portfolio. What it is NOT is unit-neutral, which is exactly
    why the ``buy`` leg has to be a real row.
    """
    _, fx_rate = _resolve_instrument_and_fx(
        session, user_id=user_id, instrument_id=payload.instrument_id, on=payload.date
    )

    common = {
        "user_id": user_id,
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
    # Dividend added first so it takes the lower id: the FIFO replay's same-date
    # tie-break is id-ascending, so a date-ordered listing reads income → acquisition.
    session.add_all([dividend, buy])
    session.flush()  # assign ids

    # BOTH directions, in this transaction — the model docstring's writer contract.
    # The delete path keys on ``txn.pair_id``, so a one-directional link would dangle
    # when the pointed-at row is deleted first.
    dividend.pair_id = buy.id
    buy.pair_id = dividend.id
    session.flush()  # composite same-user FK + no-self-pair CHECK validate here

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
    for field, value in updates.items():
        setattr(txn, field, value)
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

    **Nulls the partner, does not cascade.** ``DELETE /{id}`` means "delete this row";
    silently hard-deleting a second row the caller never named, with no undo, is a
    footgun. A surviving lone ``buy`` is still a valid manual buy row and a lone
    ``dividend`` a valid cash payout — the orphan is an *economic* inconsistency the
    user can see in the list, not a referential one. The prescribed correction is
    DELETE both then re-POST, exactly as ``InvestmentTransactionUpdate`` mandates for
    any FIFO-affecting edit. Same choice as the spend-side ``delete_transaction`` /
    ``unlink_transfer``.

    Symmetric only because writers set both directions (see the model docstring): this
    keys on ``txn.pair_id``, so a one-directional link would dangle if the pointed-at
    row were deleted first.
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

    if txn.pair_id is not None:
        partner = session.scalar(
            select(InvestmentTransaction).where(
                InvestmentTransaction.id == txn.pair_id,
                InvestmentTransaction.user_id == user_id,
            )
        )
        if partner is not None:
            partner.pair_id = None

    session.delete(txn)
    session.commit()
    return None
