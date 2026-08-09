"""FX routes (PRD §F7 FX layer).

* ``POST /api/v1/fx/refresh`` — backfill the ``fx_rates`` USD→INR cache from frankfurter.
  The seed-time / manual-trigger **cold** sibling of the holdings/portfolio/ingest reads
  (which only read the cache). No ``start`` ⇒ today's rate; ``?start&end`` ⇒ a historical
  range to seed before importing USD transaction history. A source failure degrades to a
  counted warning, not an error.

FX rates are global reference data — not per-user (a date's rate is the same for everyone),
exactly like the benchmark catalog. The refresh still requires an authenticated caller (any
logged-in user) since it's a state-changing external-HTTP + DB write — "must be signed in",
not per-user scoping. This endpoint has no in-app caller (it's manual / seed-time only): the
dev seeder logs in before calling it, and any operator triggering it via ``curl`` must send an
authenticated cookie.
"""

from __future__ import annotations

from datetime import date

import httpx
from fastapi import APIRouter

from app.api.deps import CurrentUserId, SessionDep
from app.core.config import get_settings
from app.schemas import FxRefreshSummary
from app.services.fx_service import refresh_fx_rates

router = APIRouter(prefix="/fx", tags=["fx"])


@router.post("/refresh", response_model=FxRefreshSummary)
def refresh_fx_endpoint(
    session: SessionDep,
    _: CurrentUserId,
    start: date | None = None,
    end: date | None = None,
) -> FxRefreshSummary:
    """Backfill USD→INR rates from frankfurter (PRD §F7). Seed-time / manual trigger.

    Synchronous + manual (no scheduler in v1). No ``start`` fetches the latest rate; ``start``
    (+ optional ``end``) backfills a single date or an inclusive range — run this once for the
    span of your USD transaction history before importing it. An unreachable source becomes a
    counted warning (the expected outcome behind a corporate TLS proxy).
    """
    settings = get_settings()
    with httpx.Client(timeout=settings.fx_fetch_timeout_secs) as client:
        result = refresh_fx_rates(
            session, client=client, base_url=settings.frankfurter_base_url, start=start, end=end
        )
    session.commit()
    return FxRefreshSummary.model_validate(result)
