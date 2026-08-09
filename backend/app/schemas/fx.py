"""FX refresh response schema (PRD §F7 FX layer).

``FxRefreshSummary`` is the ``POST /api/v1/fx/refresh`` body, built from
``fx_service.FxRefreshResult`` (``from_attributes``).
"""

from __future__ import annotations

from datetime import date as date_t

from pydantic import BaseModel, ConfigDict


class FxRefreshSummary(BaseModel):
    """Response body of ``POST /api/v1/fx/refresh`` (USD→INR backfill).

    Counts from one frankfurter backfill run + PII-safe warnings. ``range_start`` /
    ``range_end`` bound the dates the source returned (``None`` if the fetch failed).
    """

    model_config = ConfigDict(from_attributes=True)

    rates_inserted: int
    range_start: date_t | None
    range_end: date_t | None
    fetch_errors: int
    warnings: list[str]
