"""Instrument routes (PRD §F7).

* ``POST /api/v1/instruments`` — create an instrument.
* ``GET /api/v1/instruments`` — list active (non-archived) instruments, symbol-sorted.
* ``PATCH /api/v1/instruments/{id}`` — rename, update ``current_nav`` and the
  ``nav_as_of`` date it is valid for (the manual NAV refresh), or supply a first
  ``isin``. ``symbol`` / ``asset_class`` / ``currency`` / ``exchange`` are locked at
  creation and ``isin`` is write-once — see :class:`InstrumentUpdate`.
* ``POST /api/v1/instruments/refresh-navs`` — auto-refresh NAVs from public price
  feeds (AMFI NAVAll for MFs, Yahoo for equities; PRD §F7 / §F9 "Update NAVs"). The
  bulk, automated sibling of the manual PATCH above. Source failures degrade to a
  warning in the summary, not an error.
* ``DELETE /api/v1/instruments/{id}`` — soft-delete (sets ``archived_at``).
  Investment transactions stay linked to the archived instrument.

``IntegrityError`` on the partial unique index ``uq_instruments_active_user_symbol_currency``
surfaces as 409 with a generic message (no symbol echo) — mirrors ``accounts.py``. A same
symbol in a different currency is *not* a conflict (a cross-listed ticker is two instruments).
The explicit constructor (no ``**model_dump()`` splat) is deliberate.
"""

from __future__ import annotations

from datetime import date, datetime

import httpx
from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.deps import CurrentUserId, SessionDep
from app.core import clock
from app.core.config import get_settings
from app.core.db_errors import is_unique_violation
from app.models import Instrument
from app.schemas import InstrumentCreate, InstrumentRead, InstrumentUpdate, NavRefreshSummary
from app.services.nav_snapshot_service import as_valuation_stamp, refresh_navs

router = APIRouter(prefix="/instruments", tags=["instruments"])


def _valuation_stamp(nav_as_of: date | None) -> datetime:
    """Resolve a client-supplied valuation date into the ``nav_updated_at`` value.

    Defaults to today when the client omits it — a hand-typed price with no stated date
    is a price for today. A **future** date is a 422: it is an HTTP-body error (a typo'd
    year), and left unchecked it would silently suppress the staleness warning for that
    holding forever, since a negative age never crosses the threshold. The clock lives
    here rather than in the schema because no schema in this package imports it.
    """
    today = clock.today()
    resolved = nav_as_of if nav_as_of is not None else today
    if resolved > today:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="nav_as_of must not be in the future",
        )
    return as_valuation_stamp(resolved)


def _is_symbol_currency_dup(e: IntegrityError) -> bool:
    """409 detection mirror of ``accounts._is_name_dup``, for the active
    ``(user_id, symbol, currency)`` partial unique index. Delegates the
    dialect-aware matching to :func:`app.core.db_errors.is_unique_violation`."""
    return is_unique_violation(
        e.orig,
        index_name="uq_instruments_active_user_symbol_currency",
        columns=["instruments.user_id", "instruments.symbol", "instruments.currency"],
    )


@router.post("", response_model=InstrumentRead, status_code=status.HTTP_201_CREATED)
def create_instrument(
    payload: InstrumentCreate,
    session: SessionDep,
    user_id: CurrentUserId,
) -> Instrument:
    instrument = Instrument(
        user_id=user_id,
        symbol=payload.symbol,
        name=payload.name,
        asset_class=payload.asset_class,
        currency=payload.currency,
        exchange=payload.exchange,
        isin=payload.isin,
        current_nav=payload.current_nav,
        # The valuation date the price is effective for — only when a nav is supplied.
        nav_updated_at=(
            _valuation_stamp(payload.nav_as_of) if payload.current_nav is not None else None
        ),
    )
    session.add(instrument)
    try:
        session.commit()
    except IntegrityError as e:
        session.rollback()
        if _is_symbol_currency_dup(e):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="instrument symbol + currency already exists",
            ) from e
        raise
    session.refresh(instrument)
    return instrument


@router.get("", response_model=list[InstrumentRead])
def list_instruments(
    session: SessionDep,
    user_id: CurrentUserId,
) -> list[Instrument]:
    stmt = (
        select(Instrument)
        .where(Instrument.user_id == user_id, Instrument.archived_at.is_(None))
        .order_by(Instrument.symbol.asc())
    )
    return list(session.scalars(stmt))


@router.patch("/{instrument_id}", response_model=InstrumentRead)
def update_instrument(
    instrument_id: int,
    payload: InstrumentUpdate,
    session: SessionDep,
    user_id: CurrentUserId,
) -> Instrument:
    instrument = session.scalar(
        select(Instrument).where(
            Instrument.id == instrument_id,
            Instrument.user_id == user_id,
            Instrument.archived_at.is_(None),
        )
    )
    if instrument is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="instrument not found",
        ) from None
    updates = payload.model_dump(exclude_unset=True)
    # `nav_as_of` is NOT a column — it resolves into `nav_updated_at` below. Pop it before
    # anything iterates the dump: `getattr(instrument, "nav_as_of")` raises, and the
    # `setattr` loop would hang a stray attribute off the mapped instance.
    nav_as_of_supplied = "nav_as_of" in payload.model_fields_set
    nav_as_of = updates.pop("nav_as_of", None)
    # `isin` IS a column, but it is write-once — so it must not ride the setattr loop
    # below, which would clobber a stored identity key with no diagnostic anywhere.
    isin = updates.pop("isin", None)
    if (
        "isin" in payload.model_fields_set
        and instrument.isin is not None
        and isin != instrument.isin
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="isin is write-once — delete and re-create the instrument to change it",
        )
    isin_fills = isin is not None and instrument.isin is None

    # Reasons this PATCH is a real write even when every supplied column already matches.
    forced = nav_as_of_supplied or isin_fills
    if not updates and not forced:
        # Empty body — no DB round-trip, no spurious updated_at bump.
        return instrument
    # Idempotency short-circuit: every supplied field already equals current. An explicit
    # `nav_as_of` skips it — re-sending an unchanged price with a corrected valuation date
    # is a real edit, and short-circuiting it would 200 while changing nothing, which is
    # exactly the correction path this field exists to provide. A first `isin` skips it for
    # the plainer reason that the field is no longer in `updates` to be compared.
    if not forced and all(getattr(instrument, k) == v for k, v in updates.items()):
        return instrument

    if isin_fills:
        instrument.isin = isin

    nav_changed = "current_nav" in updates and updates["current_nav"] != instrument.current_nav
    nav_cleared = "current_nav" in updates and updates["current_nav"] is None
    for field, value in updates.items():
        setattr(instrument, field, value)
    if nav_cleared:
        # A valuation date for a price that no longer exists is incoherent.
        instrument.nav_updated_at = None
    elif nav_changed or nav_as_of_supplied:
        # The "manual NAV refresh" (PRD §F7 / §F9) — restamp on a real change, or when the
        # client restates the valuation date of a price it is leaving alone.
        instrument.nav_updated_at = _valuation_stamp(nav_as_of)

    try:
        session.commit()
    except IntegrityError as e:
        session.rollback()
        if _is_symbol_currency_dup(e):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="instrument symbol + currency already exists",
            ) from e
        raise
    session.refresh(instrument)
    return instrument


@router.post("/refresh-navs", response_model=NavRefreshSummary)
def refresh_navs_endpoint(
    session: SessionDep,
    user_id: CurrentUserId,
) -> NavRefreshSummary:
    """Auto-refresh NAVs from public price feeds (PRD §F7 / §F9 "Update NAVs").

    Indian-MF NAVs from AMFI NAVAll (matched by ISIN); Indian-equity, US-equity and
    US-ETF prices from Yahoo (``nav_snapshot_service._QUOTE_CLASSES``). Only the
    classes with no auto source — fd / bond / nps / gold / other — stay
    user-entered, and for those a refresh is a no-op however stale they are. Writes the
    source's NAV *date* to ``nav_updated_at`` — the same valuation-date meaning the manual
    path writes, per :class:`app.models.instrument.Instrument` — skipping any holding whose
    existing NAV is already newer. Synchronous + manual-trigger (no scheduler in v1); a slow
    source becomes a counted warning, never a request error.
    """
    settings = get_settings()
    with httpx.Client(timeout=settings.nav_fetch_timeout_secs) as client:
        result = refresh_navs(
            session,
            user_id=user_id,
            client=client,
            amfi_url=settings.amfi_navall_url,
            yahoo_base_url=settings.yahoo_quote_base_url,
            as_of=clock.today(),
        )
    session.commit()
    return NavRefreshSummary.model_validate(result)


@router.delete("/{instrument_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_instrument(
    instrument_id: int,
    session: SessionDep,
    user_id: CurrentUserId,
) -> None:
    """Soft-delete: set ``archived_at = now()``. Idempotent via the
    ``archived_at IS NULL`` loader filter — a 2nd DELETE returns 404.

    Investment transactions stay linked to the archived instrument (mirrors
    accounts); the holdings read-model simply skips archived instruments.
    """
    instrument = session.scalar(
        select(Instrument).where(
            Instrument.id == instrument_id,
            Instrument.user_id == user_id,
            Instrument.archived_at.is_(None),
        )
    )
    if instrument is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="instrument not found",
        ) from None
    instrument.archived_at = clock.utcnow()
    session.commit()
    return None
