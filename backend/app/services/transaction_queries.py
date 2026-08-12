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


def board_or_pending_on_batch[S: Select[Any]](stmt: S, batch_id: int) -> S:
    """Restrict to board rows plus this one batch's still-pending rows.

    Second concrete consumer of this module (CLAUDE.md §2):
    ``reconciliation_service.reconcile_batch`` (balance reconciliation,
    PRD §F1/§F4a) needs the row-set decision 1 specifies — everything
    already on the board, **plus** whatever this batch just staged but
    hasn't committed yet, so a check run at upload (before the user has
    confirmed anything) sees this batch's own rows.

    Deliberately not built on :func:`confirmed_only` — that predicate
    excludes exactly the rows this one exists to include. The caller's
    statement must already select from / join ``transactions``.
    """
    return stmt.where(
        Transaction.confirmed_at.is_not(None) | (Transaction.import_batch_id == batch_id)
    )
