"""Liveness probe.

No DB, no auth — a deliberate sanity check that the FastAPI app and the v1
router aggregator are wired correctly.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/health", tags=["health"])


@router.get("")
def health() -> dict[str, str]:
    return {"status": "ok"}
