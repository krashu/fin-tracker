"""Shared query-shaping helpers for the ``transactions`` table.

Extracted once the second concrete consumer landed (CLAUDE.md §2): the
``confirmed_at IS NOT NULL`` board predicate is needed by both
``GET /api/v1/transactions`` (the board read) and the F8 dashboard
aggregates. The ``TODO(F8)`` in the transactions route named exactly this
trigger.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Select

from app.models import Transaction


def confirmed_only[S: Select[Any]](stmt: S) -> S:
    """Restrict a Transaction query to board rows (committed, not pending).

    ``confirmed_at IS NULL`` = pending in the per-batch review queue;
    non-NULL = on the board (F2 manual rows stamp ``now()`` at create, import
    rows stay NULL until ``POST /imports/{batch_id}/commit``). The caller's
    statement must already select from / join ``transactions``.
    """
    return stmt.where(Transaction.confirmed_at.is_not(None))
