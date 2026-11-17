"""Import-time reconciliation rules (PRD §F4a) plus statement balance
reconciliation (PRD §F1/§F4a).

Exports:

* :func:`auto_link_cc_bill` — F4a-1, run at the commit of either side's batch. Detects a
  CC-side ``PAYMENT RECEIVED`` row (``income``, or the ``transfer`` an uncategorised one commits
  as, ADR-0002 rule 7c) or the bank debit that paid it, and links it to its match via
  ``Transaction.transfer_pair_id``, flipping both rows'
  ``transaction_type`` to ``"transfer"`` and moving each row's prior type and category into
  ``pre_link_type`` / ``pre_link_category_id`` so an unlink can restore them (ADR-0002
  rule 9). Future F4a-2 (CAS↔manual merge) and F4a-4 (re-import supersede) will land
  alongside it.
* :func:`pair_existing_rows` and :func:`release_leg` — the pair write shared by the
  auto-link and the manual link, and its inverse (unlink, partner-delete).
* :func:`is_cc_payment` — the CC-side merchant gate :func:`auto_link_cc_bill`
  uses, extracted for its second consumer: ``GET /imports/{batch_id}/candidates``
  surfaces it as ``cc_payment_candidate`` so the review queue can offer the
  right action before commit.
* :func:`is_card_bill_debit_row` — the row test behind ``cc_payment_candidate`` on a bank
  batch and behind :func:`card_bill_transfer_gate`.
* :func:`is_card_payment_row` — its card-side twin, behind ``cc_payment_candidate`` on a card
  batch and the card branch of :func:`card_bill_transfer_gate`.
* :func:`card_bill_link_offered_ids` — which rows the dialog offers the manual card-bill link
  on (ADR-0002 rule 11b): the placement behind ``TransactionRead.card_bill_link_offered``.
* :func:`statement_window` — the dates a statement batch covers (ADR-0010 rule 8), shared by
  :func:`reconcile_batch` and :func:`card_bill_transfer_gate`.
* :func:`card_bill_transfer_gate` — ADR-0002 rules 7b and 7c: which of a batch's flagged rows
  (a bank batch's debits, a card batch's payment credits) commit as an unpaired ``transfer`` when
  left uncategorised.
* :func:`reconcile_batch` — window-delta check between a statement's own
  closing/opening balance and the transactions we actually hold for that
  window. See ``ImportBatch``'s module docstring for the column contract.
* :func:`rows_removed_since_import` — the discard-noise qualifier: how many
  of a batch's originally-staged rows no longer exist (see its own
  docstring for why this can't be folded into the delta itself).

The bank-side vocabulary (PRD's `CC PAYMENT|CREDIT CARD PMT|...`,
:func:`is_card_bill_debit`) does not decide a link: the matching predicate across the pair is
amount + date-window + parent_account, not merchant-string equality, and the parent_account
FK is the strong signal. It flags a bank debit for the review queue, and
:func:`card_bill_transfer_gate` uses it to decide whether the commit keeps an unlinked one out
of spend (ADR-0002 rule 7b).

This module is the **first production writer** of ``transfer_pair_id``
and, historically, of ``transaction_type`` after row creation. As of
ADR-0007 ``PATCH /transactions`` also re-types rows — the user must be
able to correct an ``income`` credit the parser could not tell from a
refund (since ADR-0009 that correction retypes it to ``spend``, the
positive sign making it a refund). A row becomes a ``transfer`` in four ways:
``POST /transactions/transfer`` (born as a pair), a link (this module: the auto-link and the
manual link), backup restore, and the import commit, which retypes an unlinked card-bill debit or
card payment credit when :func:`card_bill_transfer_gate` says so (ADR-0002 rules 7b, 7c). That
retype is one assignment in the commit's pre-flight, beside the category defaulting it pre-empts;
the decision lives here.
``PATCH /transactions`` re-types rows (ADR-0007) and may type an unpaired row to or from
``transfer`` (ADR-0002 rule 7d), but refuses any identity or type edit on a paired row; unlinking
first is the sanctioned way out.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from datetime import date, timedelta
from uuid import UUID

from sqlalchemy import Select, and_, exists, func, select
from sqlalchemy.orm import Session, aliased

from app.core.log_config import get_logger
from app.models import Account, ImportBatch, Transaction
from app.services.transaction_queries import board_or_pending_on_batch

log = get_logger(__name__)

# PRD §F4a-1 CC-side classification regex. Pattern is lowercase to match
# the lowercase output of normalize_merchant.
_CC_PAYMENT_RE = re.compile(r"payment received|thank you for payment")


def is_cc_payment(merchant_normalized: str) -> bool:
    """Whether this merchant names a credit-card bill payment (PRD §F4a-1).

    The CC-side merchant gate of :func:`auto_link_cc_bill`, extracted for its
    second consumer: ``GET /imports/{batch_id}/candidates`` surfaces it as
    ``cc_payment_candidate`` so the review queue can offer the right action.
    Matches against ``merchant_normalized`` (lowercased).
    """
    return _CC_PAYMENT_RE.search(merchant_normalized) is not None


# PRD §F4a-1 bank-side vocabulary, plus the wordings bank narrations use for an autopay or a
# card-bill service. Bare BBPS and "IB BILLPAY" are deliberately absent: they carry every
# utility bill, so flagging them would offer the link on rows that are not card bills.
_CARD_BILL_DEBIT_RE = re.compile(
    r"\bcc\s*payment\b|\bcredit\s*card\s*(?:pmt|payment|bill)\b|\b(?:hdfc|icici|axis|sbi)\s*cc\b"
    r"|\bcc\s*\S*\s*autopay\b|\bcard\s*bill\b|\bcred[\s.]club\b",
    re.IGNORECASE,
)


def is_card_bill_debit(merchant_normalized: str) -> bool:
    """Whether this merchant names a credit-card bill payment made from a bank account.

    The bank-side counterpart of :func:`is_cc_payment`, behind the same
    ``cc_payment_candidate`` flag (``GET /imports/{batch_id}/candidates``) so the review queue
    can offer "Card bill payment" on a bank debit. It never decides a link
    (:func:`auto_link_cc_bill` matches by amount, date and parent account, never by this text),
    but it does decide how an unlinked debit commits (ADR-0002 rule 7b).
    """
    return _CARD_BILL_DEBIT_RE.search(merchant_normalized) is not None


def is_card_bill_debit_row(txn: Transaction) -> bool:
    """A negative ``spend`` naming a card-bill payment: the bank clause of
    ``cc_payment_candidate`` and the row test of :func:`card_bill_transfer_gate`."""
    return (
        txn.transaction_type == "spend"
        and txn.amount_paise < 0
        and is_card_bill_debit(txn.merchant_normalized)
    )


def is_card_payment_row(txn: Transaction) -> bool:
    """A positive ``income`` naming a bill payment: the card clause of ``cc_payment_candidate``
    and the row test of :func:`card_bill_transfer_gate` on a card batch (ADR-0002 rule 7c)."""
    return (
        txn.transaction_type == "income"
        and txn.amount_paise > 0
        and is_cc_payment(txn.merchant_normalized)
    )


def pair_existing_rows(session: Session, a: Transaction, b: Transaction) -> None:
    """Link two EXISTING rows as a transfer pair (ADR-0002 rules 8 and 9).

    Each leg remembers its own pre-link type and category, so unlink and partner-delete can
    put them back; a pair belongs to no category, so the category is cleared. Pair, type and
    category change together, one UPDATE per row, which CHECKs ``paired_is_transfer`` (rule 8)
    and ``pre_link_category_while_linked`` (rule 9) both require. The caller has decided the
    rows are eligible and owns the commit.

    The savepoint is here for **flush attribution**, not error recovery: its entry flush lands
    prior dirty state on the parent and its exit flush fires the composite FK here, so a
    constraint failure is attributable to this write. Nothing is caught.
    """
    with session.begin_nested():
        for leg, other in ((a, b), (b, a)):
            leg.pre_link_type = leg.transaction_type
            leg.pre_link_category_id = leg.category_id
            leg.category_id = None
            leg.transfer_pair_id = other.id
            leg.transaction_type = "transfer"
        # begin_nested() flushes on __exit__; composite FK fires here.


def release_leg(leg: Transaction) -> None:
    """Unpair one leg and give it back what a link took (ADR-0002 rule 9): its pre-link type
    and its pre-link category. A leg born as a transfer (F2) has neither and stays one.

    Pair, type and category change together, so the row's UPDATE satisfies CHECKs
    ``paired_is_transfer`` (rule 8) and ``pre_link_category_while_linked`` (rule 9). A saved
    category that is still set proves nobody categorised the leg since the link (every category
    write on a linked leg clears it), so the newer choice is never overwritten. A spend category
    the user set on the leg survives even when the restored type is income: its kind no longer
    matching is not ours to clear.

    ``backup_import_service`` keeps its own variant for rows that are not flushed yet; this is
    the canonical rule.
    """
    leg.transfer_pair_id = None
    if leg.pre_link_type is not None:
        leg.transaction_type, leg.pre_link_type = leg.pre_link_type, None
    if leg.pre_link_category_id is not None:
        leg.category_id, leg.pre_link_category_id = leg.pre_link_category_id, None


CANNOT_LINK_TO_ITSELF = "a transaction cannot be linked to itself"
NOT_CONFIRMED = "both transactions must be confirmed"
ALREADY_LINKED = "a transaction that is already linked must be unlinked first"
SAME_ACCOUNT = "the two transactions must be on different accounts"
NOT_EQUAL_AND_OPPOSITE = "the two amounts must be exactly equal and opposite"
BAD_ACCOUNT = "both accounts must be live INR accounts, not investment accounts"
REFUND_LINKED = "a transaction in a refund link cannot be linked"


def manual_link_problem(
    session: Session, *, user_id: UUID, txn: Transaction, partner: Transaction
) -> str | None:
    """Why ``txn`` and ``partner`` cannot be manually linked, or ``None`` when they can
    (ADR-0002 rule 11b). Both rows are the caller's: ownership is the route's concern.

    The one eligibility rule behind ``POST /transactions/{id}/link`` and
    ``GET /transactions/{id}/link-candidates``, so the two cannot drift. In-memory checks run
    before the queries. Dates may differ; there is no account-type pairing rule beyond the
    account gate.
    """
    if txn.id == partner.id:
        return CANNOT_LINK_TO_ITSELF
    if txn.confirmed_at is None or partner.confirmed_at is None:
        return NOT_CONFIRMED
    if txn.transfer_pair_id is not None or partner.transfer_pair_id is not None:
        return ALREADY_LINKED
    if txn.account_id == partner.account_id:
        return SAME_ACCOUNT
    if txn.amount_paise != -partner.amount_paise:
        return NOT_EQUAL_AND_OPPOSITE
    live_accounts = session.scalar(
        select(func.count())
        .select_from(Account)
        .where(
            Account.id.in_({txn.account_id, partner.account_id}),
            Account.user_id == user_id,
            Account.archived_at.is_(None),
            Account.type != "investment",
            Account.currency == "INR",
        )
    )
    if live_accounts != 2:
        return BAD_ACCOUNT
    # ADR-0009 rule 13: a refund's original, or a refund with its link set, stays as it is.
    if (
        txn.refund_of_id is not None
        or partner.refund_of_id is not None
        or session.scalar(
            select(
                exists().where(
                    Transaction.user_id == user_id,
                    Transaction.refund_of_id.in_((txn.id, partner.id)),
                )
            )
        )
    ):
        return REFUND_LINKED
    return None


# The most rows `GET /transactions/{id}/link-candidates` returns.
LINK_CANDIDATES_LIMIT = 20


def link_candidates(session: Session, *, user_id: UUID, txn: Transaction) -> list[Transaction]:
    """The closest rows ``txn`` can be manually linked to, at most :data:`LINK_CANDIDATES_LIMIT`.

    One SQL prefilter (same user, confirmed, unpaired, the opposite amount, another account),
    sorted nearest date first in Python, then each row is vetted by
    :func:`manual_link_problem` until the cap, so the list and the POST agree by construction.
    A row that cannot be linked itself returns nothing.
    """
    if txn.confirmed_at is None or txn.transfer_pair_id is not None:
        return []
    rows = session.scalars(
        select(Transaction).where(
            Transaction.user_id == user_id,
            Transaction.confirmed_at.is_not(None),
            Transaction.transfer_pair_id.is_(None),
            Transaction.amount_paise == -txn.amount_paise,
            Transaction.account_id != txn.account_id,
        )
    ).all()
    found: list[Transaction] = []
    for row in sorted(rows, key=lambda r: (abs((r.date - txn.date).days), r.id)):
        if manual_link_problem(session, user_id=user_id, txn=txn, partner=row) is None:
            found.append(row)
            if len(found) == LINK_CANDIDATES_LIMIT:
                break
    return found


# The bank-side types a CC bill debit may have; a refund is excluded by sign, not type.
_PAIRABLE_BANK_TYPES = ("spend", "transfer")

# The card-side types a bill payment credit may have: ``income`` when it was categorised as income
# in review, ``transfer`` when it committed uncategorised (ADR-0002 rule 7c). A refund is a
# positive ``spend``, never a payment.
_PAIRABLE_CARD_TYPES = ("income", "transfer")

# ±2 days per PRD §F4a-1. SQLAlchemy ``Column.between(a, b)`` is inclusive
# on both ends in SQLite + Postgres.
_MATCH_WINDOW = timedelta(days=2)


def card_bill_link_offered_ids(
    session: Session, *, user_id: UUID, txns: Sequence[Transaction]
) -> set[int]:
    """Ids of ``txns`` the dialog offers "Paid your card bill elsewhere?" on (ADR-0002 rule 11b).

    A confirmed, unpaired, positive ``income`` or ``transfer`` row on a live card: the card side
    of a bill payment, whether categorised as income in review or committed as an unpaired
    transfer (rule 7c). Placement only: which rows can be linked stays with
    :func:`manual_link_problem`. The row filters run in memory; the accounts are read once for
    the whole response, never per row, and not at all when no row could be offered.
    """
    eligible = [
        t
        for t in txns
        if t.confirmed_at is not None
        and t.transfer_pair_id is None
        and t.amount_paise > 0
        and t.transaction_type in _PAIRABLE_CARD_TYPES
    ]
    if not eligible:
        return set()
    live_cards = set(
        session.scalars(
            select(Account.id).where(
                Account.id.in_({t.account_id for t in eligible}),
                Account.user_id == user_id,
                Account.type == "credit_card",
                Account.archived_at.is_(None),
            )
        )
    )
    return {t.id for t in eligible if t.account_id in live_cards}


def _live_child_cards(*, user_id: UUID, bank_account_id: int) -> Select[int]:
    """Ids of the user's live cards that name this bank account as "Paid from"."""
    return select(Account.id).where(
        Account.user_id == user_id,
        Account.parent_account_id == bank_account_id,
        Account.archived_at.is_(None),
    )


def statement_window(
    *,
    period_start: date | None,
    period_end: date | None,
    rows_first: date | None,
    rows_last: date | None,
) -> tuple[date, date] | None:
    """The dates a statement batch covers (ADR-0010 rule 8): its printed period, else the first
    and last dates of its own rows, else ``None``. Computed, never stored. The one rule behind
    :func:`reconcile_batch`'s window and :func:`card_bill_transfer_gate`'s coverage."""
    if period_start is not None and period_end is not None:
        return period_start, period_end
    if rows_first is not None and rows_last is not None:
        return rows_first, rows_last
    return None


def _never(txn: Transaction) -> bool:
    return False


def card_bill_transfer_gate(
    session: Session, *, user_id: UUID, batch: ImportBatch
) -> Callable[[Transaction], bool]:
    """ADR-0002 rules 7b and 7c: which of ``batch``'s rows commit as an unpaired ``transfer``
    when committed with no category.

    On a card batch the predicate is true for a card payment credit
    (:func:`is_card_payment_row`) on the batch's own account, whatever "Paid from" says and
    whether or not the paying bank is tracked (rule 7c): paying a card is never income, and the
    purchases it settles are spend on the card's own statements.

    On a bank batch it is true for a card-bill debit (:func:`is_card_bill_debit_row`) on the
    batch's own account when that account is a live bank, a live card names it "Paid from", and
    a committed statement of such a card covers the row's date (:func:`statement_window`;
    rule 7b). A statement is committed once one of its rows is confirmed: a pending batch has
    none, and a fully cancelled one is deleted.

    It ignores the row's category; the commit adds that test. Everything loads here in two
    queries per batch (one on a card batch), never per row. ``list_candidates`` and
    ``commit_import_batch`` both call it, so the review copy and the commit cannot drift.
    """
    if batch.account_id is None:
        return _never
    bank_id = batch.account_id
    bank = session.execute(
        select(Account.type, Account.archived_at).where(
            Account.id == bank_id, Account.user_id == user_id
        )
    ).one_or_none()
    if bank is not None and bank.type == "credit_card":
        return lambda txn: txn.account_id == bank_id and is_card_payment_row(txn)
    if bank is None or bank.type != "bank" or bank.archived_at is not None:
        return _never
    spans = session.execute(
        select(
            ImportBatch.period_start,
            ImportBatch.period_end,
            func.min(Transaction.date),
            func.max(Transaction.date),
        )
        .join(
            Transaction,
            and_(Transaction.import_batch_id == ImportBatch.id, Transaction.user_id == user_id),
        )
        .where(
            ImportBatch.user_id == user_id,
            ImportBatch.account_id.in_(_live_child_cards(user_id=user_id, bank_account_id=bank_id)),
        )
        .group_by(ImportBatch.id, ImportBatch.period_start, ImportBatch.period_end)
        .having(func.count(Transaction.confirmed_at) > 0)
    ).all()
    windows = [
        window
        for start, end, first, last in spans
        if (
            window := statement_window(
                period_start=start, period_end=end, rows_first=first, rows_last=last
            )
        )
        is not None
    ]

    def commits_as_transfer(txn: Transaction) -> bool:
        return (
            txn.account_id == bank_id
            and is_card_bill_debit_row(txn)
            and any(start <= txn.date <= end for start, end in windows)
        )

    return commits_as_transfer


def auto_link_cc_bill(
    session: Session,
    *,
    user_id: UUID,
    txn: Transaction,
) -> None:
    """F4a-1 CC-bill auto-link at either side's batch commit (PRD §F4a-1, ADR-0002 rule 7).

    ``txn`` was just confirmed. A card-side payment (positive ``income`` or ``transfer``, rule
    7c) looks for its bank debit; a bank-side debit (negative ``spend`` / ``transfer``) looks for
    the card payment it paid, and hands the pair to the card-side rule, so both directions apply
    the same gates, refund exclusion and ambiguity rule. Whichever statement is committed second
    forms the link. Idempotent under re-commit; never commits.
    """
    if txn.transfer_pair_id is not None:
        return
    if txn.transaction_type in _PAIRABLE_CARD_TYPES and txn.amount_paise > 0:
        _link_from_card(session, user_id=user_id, txn=txn)
    elif txn.transaction_type in _PAIRABLE_BANK_TYPES and txn.amount_paise < 0:
        _link_from_bank(session, user_id=user_id, bank_row=txn)


def _link_from_bank(session: Session, *, user_id: UUID, bank_row: Transaction) -> None:
    """Find the confirmed card payment this bank debit paid, and link through the card side.

    The card payments searched are on live child cards of ``bank_row``'s account, confirmed,
    unpaired, ``income`` or ``transfer``, equal and opposite, inside the date window, and named
    like a CC payment (:func:`is_cc_payment`). No bank-side merchant regex: the
    ``parent_account_id`` link is the strong signal. Zero matches is a silent no-op; two or more
    is ambiguous, skipped with a PII-free ``log.info``. Exactly one is handed to
    :func:`_link_from_card`, which reruns its gates and checks it has exactly one bank candidate
    (the match predicate is symmetric, so that candidate is ``bank_row``); ambiguity on either
    side means no link. One indexed query per negative row; it never matches on a card batch,
    since only a ``credit_card`` account can have a parent.
    """
    child_cards = _live_child_cards(user_id=user_id, bank_account_id=bank_row.account_id)
    payments = [
        row
        for row in session.scalars(
            select(Transaction).where(
                Transaction.user_id == user_id,
                Transaction.account_id.in_(child_cards),
                Transaction.confirmed_at.is_not(None),
                Transaction.transfer_pair_id.is_(None),
                Transaction.transaction_type.in_(_PAIRABLE_CARD_TYPES),
                Transaction.amount_paise > 0,
                Transaction.amount_paise == -bank_row.amount_paise,
                Transaction.date.between(
                    bank_row.date - _MATCH_WINDOW, bank_row.date + _MATCH_WINDOW
                ),
            )
        )
        if is_cc_payment(row.merchant_normalized)
    ]
    if not payments:
        return
    if len(payments) >= 2:
        log.info(
            "f4a_skip_ambiguous_card_candidates",
            bank_transaction_id=bank_row.id,
            candidate_count=len(payments),
        )
        return
    _link_from_card(session, user_id=user_id, txn=payments[0])


def _link_from_card(session: Session, *, user_id: UUID, txn: Transaction) -> None:
    """The card-side rule: link a CC "payment received" row (``income`` or ``transfer``) to its
    parent-bank debit.

    Self-defensive at every *gate* — a missing prerequisite is a silent
    no-op (no exception, no log). That covers the gates only; the write
    below catches nothing. The only log emit is on the ambiguity-skip
    path: ``len(candidates) >= 2`` → ``log.info`` with ids only (no PII).

    Atomicity: when a single match is found, :func:`pair_existing_rows` mutates both rows'
    pair, type and category columns inside ``session.begin_nested()``. The composite FK from
    migration 0005 is immediate; both UPDATEs land in the savepoint, both target rows exist
    with matching ``user_id``, so there's no order-of-operations issue. The savepoint is
    there for **flush attribution**, not error recovery (see its docstring).

    A constraint failure is NOT caught. It propagates and aborts the whole
    batch commit — no row keeps its ``confirmed_at``, pass-3 learning never
    runs — matching the all-or-none policy pass 3 already has. It is
    unreachable on today's inputs: both rows come from one ``user_id``-scoped
    SELECT, and the no-self-pair CHECK cannot fire because this row is positive on a card
    while candidates are negative on its parent bank, so they are different rows on
    different accounts. The v2
    concurrent-commit race — the bank row's pair_id flipping between
    candidate search and flush — belongs in the v2 commit with a test, not
    in a pre-emptive swallow nobody has ever seen behave (AGENTS.md §Simplicity first).
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
    # import_service._map_type; the commit retypes an uncategorised one to ``transfer``
    # (ADR-0002 rule 7c), so both are card payments here.
    if txn.transaction_type not in _PAIRABLE_CARD_TYPES:
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
    refund = aliased(Transaction)
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
                # `amount_paise < 0` is not redundant with the equality below
                # (ADR-0009 rule 6). That equality pins the sign only while `txn`
                # is positive, and a NEGATIVE income row is an ordinary write: an
                # income reversal (ADR-0009 rule 2), accepted by the API like any
                # other non-zero sign. Against one, the equality would select a
                # positive refund, so the guard is stated here, never derived
                # from a type.
                Transaction.transaction_type.in_(_PAIRABLE_BANK_TYPES),
                Transaction.amount_paise < 0,
                # ADR-0002 rule 7: skip a refund's original (ADR-0009 rule 13). Turning it
                # into a transfer would break the refund link. A candidate is negative, so it
                # cannot itself be a refund (a refund is a positive `spend`).
                ~exists().where(refund.user_id == user_id, refund.refund_of_id == Transaction.id),
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

    pair_existing_rows(session, txn, candidates[0])


def reconcile_batch(session: Session, *, user_id: UUID, batch: ImportBatch) -> int | None:
    """actual − expected in paise, or None when the batch carries no usable metadata.

    ``expected = closing − opening``, both read off the statement in the user's position
    (ADR-0010 rule 7: positive = money held, negative = owed), so a card and a bank
    statement take the same arithmetic. ``actual`` sums ``Transaction.amount_paise``
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

    **Window fallback, computed here and never stored (ADR-0010 rule 8):**
    when the parser read both balances but not the period
    (``batch.period_start``/``period_end`` are ``None``), the window becomes
    ``min(date)``/``max(date)`` over this batch's own ``Transaction`` rows.
    It is not written back: ``period_*`` holds only the period the statement
    printed, so every call recomputes the fallback. The rule lives in :func:`statement_window`,
    which :func:`card_bill_transfer_gate` shares.

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

    rows_first: date | None = None
    rows_last: date | None = None
    if batch.period_start is None or batch.period_end is None:
        rows_first, rows_last = session.execute(
            select(func.min(Transaction.date), func.max(Transaction.date)).where(
                Transaction.user_id == user_id,
                Transaction.import_batch_id == batch.id,
            )
        ).one()
    window = statement_window(
        period_start=batch.period_start,
        period_end=batch.period_end,
        rows_first=rows_first,
        rows_last=rows_last,
    )
    if window is None:
        return None
    period_start, period_end = window

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
    live_count = session.scalar(
        select(func.count()).where(
            Transaction.user_id == batch.user_id, Transaction.import_batch_id == batch.id
        )
    )
    return max(0, batch.imported_count - (live_count or 0))
