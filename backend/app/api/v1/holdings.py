"""``/api/v1/holdings`` — current investment positions (PRD §F7).

Read-only. ``GET /holdings`` replays every investment transaction through a FIFO
lot queue (``holdings_service.compute_holdings``) and returns one row per
instrument still held. No commit, no business logic in the route — same shape as
the dashboards aggregate routes. The route is unpaginated: the row count is
bounded by the user's distinct instruments (tens), matching the dashboards
rationale.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import CurrentUserId, SessionDep
from app.core import clock
from app.schemas import HoldingRead, HoldingsResponse
from app.services.fx_service import latest_rate
from app.services.holdings_service import compute_holdings

router = APIRouter(prefix="/holdings", tags=["holdings"])


@router.get("", response_model=HoldingsResponse)
def list_holdings(
    session: SessionDep,
    user_id: CurrentUserId,
) -> HoldingsResponse:
    # Current holdings are a "now" view, which has no as-of date — so read the newest
    # cached rate directly rather than carrying forward from a "today" the host's timezone
    # defines. `rate_on(on=today)` would answer differently on the native (IST) and Docker
    # (UTC) deployments for the same data, and returns None on a cache whose only row is
    # dated local-today — dropping the USD leg out of the rollup entirely.
    #
    # `as_of` below is NOT that argument. It is only the anchor the per-row valuation age
    # is subtracted from, never a cache-lookup key, so it cannot drop a row the way
    # `rate_on(on=today)` can — and `clock.today()` is UTC by construction, so it does not
    # reintroduce the host-timezone split either.
    usd_inr = latest_rate(session)
    holdings = compute_holdings(session, user_id=user_id, usd_inr_rate=usd_inr, as_of=clock.today())
    return HoldingsResponse(holdings=[HoldingRead.model_validate(h) for h in holdings])
