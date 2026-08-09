"""Import-time reconciliation rules (PRD §F4a).

Currently exports one rule:

* :func:`auto_link_cc_bill` — F4a-1. Detects a CC-side ``PAYMENT RECEIVED``
  row and links it to a matching bank-side transfer via
  ``Transaction.transfer_pair_id``, flipping both rows'
  ``transaction_type`` to ``"transfer"``. Future F4a-2 (CAS↔manual merge)
  and F4a-4 (re-import supersede) will land alongside it.

Bank-side regex (PRD's `CC PAYMENT|CREDIT CARD PMT|...`) is intentionally
omitted: the matching predicate across the pair is amount + date-window
+ parent_account, not merchant-string equality. The parent_account FK is
the strong signal; the bank-side regex is redundant given that link.

This module is the **first production writer** of ``transfer_pair_id``
and, historically, of ``transaction_type`` after row creation. As of
ADR-0007 ``PATCH /transactions`` also re-types rows — the user must be
able to correct an ``income`` credit the parser could not tell from a
refund — but the spend↔transfer half of the old contract survives in a
narrower form: PATCH refuses ``transfer`` as a *target* (pairs are born
via ``POST /transactions/transfer``) and refuses any identity or type
edit on a row that is currently paired, so only this module and that
route ever mint one. Unlinking first is the sanctioned way out. If a
second system-layer reclassifier emerges (e.g., F4a-4 supersede
detection), consider a transition helper. Not before.
"""

from __future__ import annotations

import re
from datetime import timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.log_config import get_logger
from app.models import Account, Transaction

log = get_logger(__name__)

# PRD §F4a-1 CC-side classification regex. Pattern is lowercase to match
# the lowercase-and-whitespace-collapsed output of normalize_merchant.
_CC_PAYMENT_RE = re.compile(r"payment received|thank you for payment")

# ±2 days per PRD §F4a-1. SQLAlchemy ``Column.between(a, b)`` is inclusive
# on both ends in SQLite + Postgres.
_MATCH_WINDOW = timedelta(days=2)


def auto_link_cc_bill(
    session: Session,
    *,
    user_id: UUID,
    txn: Transaction,
) -> None:
    """F4a-1 CC-bill auto-link (PRD §F4a-1).

    Self-defensive at every *gate* — a missing prerequisite is a silent
    no-op (no exception, no log). That covers the gates only; the write
    below catches nothing. The only log emit is on the ambiguity-skip
    path: ``len(candidates) >= 2`` → ``log.info`` with ids only (no PII).

    Atomicity: when a single match is found, both rows' ``transfer_pair_id``
    and ``transaction_type`` are mutated inside ``session.begin_nested()``.
    The composite FK from migration 0005 is immediate; both UPDATEs land
    in the savepoint, both target rows exist with matching ``user_id``,
    so there's no order-of-operations issue. The savepoint is here for
    **flush attribution**, not error recovery: its entry flush lands prior
    dirty state on the parent and its exit flush fires the FK here, so a
    constraint failure is attributable to this write and not to some later
    one.

    A constraint failure is NOT caught. It propagates and aborts the whole
    batch commit — no row keeps its ``confirmed_at``, pass-3 learning never
    runs — matching the all-or-none policy pass 3 already has. It is
    unreachable on today's inputs: both rows come from one ``user_id``-scoped
    SELECT, and the no-self-pair CHECK cannot fire because this row is
    ``income`` while candidates must be ``spend``/``transfer``. The v2
    concurrent-commit race — the bank row's pair_id flipping between
    candidate search and flush — belongs in the v2 commit with a test, not
    in a pre-emptive swallow nobody has ever seen behave (CLAUDE.md §2).
    The middleware logs the propagated error with ``exc_info``, and
    SQLAlchemy embeds ``[parameters: ...]``; for this statement those are
    ints and a datetime, so nothing PII-bearing reaches the log.

    Caller (POST /imports/{batch_id}/commit pass-2) is responsible for
    the outer commit. This function never commits.
    """
    # 1. Already-paired short-circuit — idempotent under re-commit / retry.
    if txn.transfer_pair_id is not None:
        return

    # 2. Type gate. Parser ``payment`` rows fold to ``income`` via
    # import_service._map_type. Inline check; no module-level constant —
    # one element, no second consumer.
    if txn.transaction_type != "income":
        return

    # 3. Merchant gate. Regex matches against merchant_normalized
    # (lowercased + whitespace-collapsed).
    if _CC_PAYMENT_RE.search(txn.merchant_normalized) is None:
        return

    # 4. Account gate. Load the CC account; require non-null parent.
    cc_account = session.scalar(
        select(Account).where(
            Account.id == txn.account_id,
            Account.user_id == user_id,
            Account.archived_at.is_(None),
        )
    )
    if cc_account is None or cc_account.parent_account_id is None:
        return

    # Parent must be non-archived too — read-time check. PR-A's PATCH
    # validates write-time; archiving the parent later does not cascade-
    # unlink. F4a-1 is the reader; check here.
    parent = session.scalar(
        select(Account).where(
            Account.id == cc_account.parent_account_id,
            Account.user_id == user_id,
            Account.archived_at.is_(None),
        )
    )
    if parent is None:
        return

    # 5. Candidate search in the parent bank account.
    candidates = list(
        session.scalars(
            select(Transaction).where(
                Transaction.user_id == user_id,
                Transaction.account_id == parent.id,
                Transaction.confirmed_at.is_not(None),
                Transaction.transfer_pair_id.is_(None),
                # Exclude refund/income on the bank side — a refund of
                # equal magnitude would otherwise be silently mis-paired.
                Transaction.transaction_type.in_(("spend", "transfer")),
                Transaction.amount_paise == -txn.amount_paise,
                Transaction.date.between(
                    txn.date - _MATCH_WINDOW,
                    txn.date + _MATCH_WINDOW,
                ),
            )
        )
    )

    # 6. Disposition by candidate count.
    if not candidates:
        return

    if len(candidates) >= 2:
        log.info(
            "f4a_skip_ambiguous_candidates",
            cc_transaction_id=txn.id,
            candidate_count=len(candidates),
            parent_account_id=parent.id,
        )
        return

    bank = candidates[0]
    with session.begin_nested():
        txn.transfer_pair_id = bank.id
        bank.transfer_pair_id = txn.id
        txn.transaction_type = "transfer"
        bank.transaction_type = "transfer"
        # begin_nested() flushes on __exit__; composite FK fires here.
