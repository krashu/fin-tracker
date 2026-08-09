"""v1 API router aggregator.

Each domain router (one file per `app/api/v1/<domain>.py`) is registered
here with a single :func:`include_router` line. Mounted under ``/api/v1``
by :mod:`app.main`.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import (
    accounts,
    auth,
    backup,
    benchmarks,
    categories,
    dashboards,
    fx,
    health,
    holdings,
    imports,
    instruments,
    investment_transactions,
    labels,
    portfolio,
    rules,
    transactions,
)

api_router = APIRouter()
api_router.include_router(accounts.router)
api_router.include_router(auth.router)
api_router.include_router(backup.router)
api_router.include_router(benchmarks.router)
api_router.include_router(categories.router)
api_router.include_router(dashboards.router)
api_router.include_router(fx.router)
api_router.include_router(health.router)
api_router.include_router(holdings.router)
api_router.include_router(imports.router)
api_router.include_router(instruments.router)
api_router.include_router(investment_transactions.router)
api_router.include_router(labels.router)
api_router.include_router(portfolio.router)
api_router.include_router(rules.router)
api_router.include_router(transactions.router)
