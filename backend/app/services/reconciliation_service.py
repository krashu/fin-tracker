"""Import-time reconciliation rules (PRD §F4a) plus statement balance
reconciliation (PRD §F1/§F4a).

Exports:

* :func:`auto_link_cc_bill` — F4a-1. Detects a CC-side ``PAYMENT RECEIVED``
  row and links it to a matching bank-side transfer via
  ``Transaction.transfer_pair_id``, flipping both rows'
  ``transaction_type`` to ``"transfer"``. Future F4a-2 (CAS↔manual merge)
  and F4a-4 (re-import supersede) will land alongside it.
* :func:`is_cc_payment` — the CC-side merchant gate :func:`auto_link_cc_bill`
  uses, extracted for its second consumer: ``GET /imports/{batch_id}/candidates``
  surfaces it as ``cc_payment_candidate`` so the review queue can offer the
  right action before commit.
* :func:`reconcile_batch` — window-delta check between a statement's own
  closing/opening balance and the transactions we actually hold for that
  window. See ``ImportBatch``'s module docstring for the column contract.
* :func:`rows_removed_since_import` — the discard-noise qualifier: how many
  of a batch's originally-staged rows no longer exist (see its own
  docstring for why this can't be folded into the delta itself).

Bank-side regex (PRD's `CC PAYMENT|CREDIT CARD PMT|...`) is intentionally
omitted: the matching predicate across the pair is amount + date-window
+ parent_account, not merchant-string equality. The parent_account FK is
the strong signal; the bank-side regex is redundant given that link.

This module is the **first production writer** of ``transfer_pair_id``
and, historically, of ``transaction_type`` after row creation. As of
ADR-0007 ``PATCH /transactions`` also re-types rows — the user must be
able to correct an ``income`` credit the parser could not tell from a
refund (since ADR-0009 that correction retypes it to ``spend``, the
positive sign making it a refund) — but the spend↔transfer half of the
old contract survives in a
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

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.log_config import get_logger
from app.models import Account, ImportBatch, Transaction
from app.services.transaction_queries import board_or_pending_on_batch

log = get_logger(__name__)

# PRD §F4a-1 CC-side classification regex. Pattern is lowercase to match
# the lowercase-and-whitespace-collapsed output of normalize_merchant.
_CC_PAYMENT_RE = re.compile(r"payment received|thank you for payment")


def is_cc_payment(merchant_normalized: str) -> bool:
    """Whether this merchant names a credit-card bill payment (PRD §F4a-1).

    The CC-side merchant gate of :func:`auto_link_cc_bill`, extracted for its
    second consumer: ``GET /imports/{batch_id}/candidates`` surfaces it as
    ``cc_payment_candidate`` so the review queue can offer the right action.
    Matches against ``merchant_normalized`` (lowercased + whitespace-collapsed).
    """
    return _CC_PAYMENT_RE.search(merchant_normalized) is not None


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

    # 3. Merchant gate.
    if not is_cc_payment(txn.merchant_normalized):
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
                # Exclude income on the bank side by type, and a REFUND by sign.
                # The sign clause is the load-bearing half: since ADR-0009 a
                # refund is a `spend` row with a positive amount, so the type
                # filter alone no longer excludes one, and a refund of equal
                # magnitude would be silently mis-paired with this CC payment.
                #
                # `amount_paise < 0` is not redundant with the equality below.
                # That equality only pins the sign while `txn` is guaranteed
                # positive — which holds for an income row created through the
                # API (sign_error enforces `income > 0`) but NOT for one restored
                # by POST /backup/import, which writes type and amount verbatim
                # without sign_error. A hand-edited zip is that path's declared
                # threat model, and it is a file-upload boundary, so the guard is
                # stated here rather than derived.
                Transaction.transaction_type.in_(("spend", "transfer")),
                Transaction.amount_paise < 0,
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


def reconcile_batch(session: Session, *, user_id: UUID, batch: ImportBatch) -> int | None:
    """actual − expected in paise, or None when the batch carries no usable metadata.

    ``expected = closing − opening``, both read off the statement (our sign
    convention: negative = owed). ``actual`` sums ``Transaction.amount_paise``
    for this account, in the statement's own date window, over
    :func:`board_or_pending_on_batch` — the board rows the account already
    has, plus whatever THIS batch just staged but hasn't been committed yet
    (decision 1: the check runs at upload, before the user has confirmed
    anything).

    Returns ``None`` when the batch is account-less (investment / backup-
    restore batches have no window to check), when either statement balance
    is missing, or when no date window is available even after the fallback
    below. A tolerance of zero is intentional (AGENTS.md §Simplicity first)
    — a mismatch of one paise is still a mismatch.

    **Window fallback, applied and stamped here:** when the parser read both
    balances but not the period (``batch.period_start``/``period_end`` are
    still ``None``), the window becomes ``min(date)``/``max(date)`` over this
    batch's own ``Transaction`` rows, written back onto those same columns —
    so a re-read (``GET .../reconciliation``, or the PRD §3.5 coverage-window
    feature) sees a window either way, not just the ones with a printed period.

    **Not the ``/overview`` absolute balance** (``opening_balance_paise +
    Σ(confirmed rows, all time)``, in ``api/v1/dashboards.py``) — this is a
    **window delta**, deliberately a different quantity so a first-ever
    import reconciles without needing the account's whole history. A test
    pins the one case where they must agree: a single-statement account
    whose ``opening_balance_paise`` equals that statement's own opening
    balance.

    **Known false-positive classes, accepted — see the ``ImportBatch``
    module docstring:** a manual F2 row for a real card transaction, and a
    row discarded at review (pair with :func:`rows_removed_since_import` for
    the latter).
    """
    if batch.account_id is None:
        return None

    opening = batch.statement_opening_balance_paise
    closing = batch.statement_closing_balance_paise
    if opening is None or closing is None:
        return None

    period_start = batch.period_start
    period_end = batch.period_end
    if period_start is None or period_end is None:
        period_start, period_end = session.execute(
            select(func.min(Transaction.date), func.max(Transaction.date)).where(
                Transaction.import_batch_id == batch.id
            )
        ).one()
        if period_start is None or period_end is None:
            return None
        batch.period_start = period_start
        batch.period_end = period_end

    expected = closing - opening
    actual = (
        session.scalar(
            board_or_pending_on_batch(
                select(func.coalesce(func.sum(Transaction.amount_paise), 0)).where(
                    Transaction.user_id == user_id,
                    Transaction.account_id == batch.account_id,
                    Transaction.date >= period_start,
                    Transaction.date <= period_end,
                ),
                batch.id,
            )
        )
        or 0
    )
    return actual - expected


def rows_removed_since_import(session: Session, *, batch: ImportBatch) -> int:
    """How many of this batch's originally-staged rows no longer exist.

    ``imported_count`` is frozen at the batch's first import — a re-upload
    never updates it (see ``import_service`` module docstring) — and nothing
    else in ``app/`` decrements it. So a live count of ``Transaction`` rows
    still carrying this ``import_batch_id`` that sits below ``imported_count``
    means rows were hard-deleted since: most commonly an investment-transfer
    debit discarded at review because it belongs to F7, not the F1 spend
    board it landed on by parsing.

    A hard delete leaves no trace of the removed row's amount, so this
    cannot correct :func:`reconcile_batch`'s delta — that stays an exact,
    signed figure. This is a **qualifier** to show alongside a mismatch
    ("N row(s) were removed from this import after it was staged"), not a
    correction to it.

    ``max(0, ...)``: a re-upload re-stages rows onto this SAME batch_id,
    which can push the live count back above ``imported_count`` — that is
    rows returning, not more being removed, so this floors at zero rather
    than going negative.
    """
    live_count = session.scalar(select(func.count()).where(Transaction.import_batch_id == batch.id))
    return max(0, batch.imported_count - (live_count or 0))
