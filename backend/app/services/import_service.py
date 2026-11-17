"""Import orchestration: parse → fingerprint → dedup → persist.

Public surface:

* :data:`PARSERS` — dispatch table from ``(issuer, account_type)`` to parser class.
* :class:`ImportResult` — frozen value object returned to the route.
* :func:`import_statement` — the orchestrator: load the account, parse, then persist.
* :func:`load_statement_account`, :func:`parser_for`, :func:`persist_statement` — its three
  steps, public so the bundled sample statements (:mod:`app.services.sample_statements`) can
  reuse the persist half without a file upload.

Caller (the route) owns the commit. This module uses the caller's
session and never calls :meth:`Session.commit` itself, matching the
"routes commit, services use the caller's session" rule from the
project CLAUDE.md.

Decisions locked in for v1 (see :file:`docs/` ADRs when they land):

* Card and bank statements. Parser ``payment`` rows are imported as ``income``; this is the
  input contract for F4a reconciliation (``reconciliation_service.auto_link_cc_bill``
  flips matched CC-bill pairs to ``transfer`` at the commit of either side's batch: the
  card side gates on a positive ``income`` or ``transfer`` row, the bank side finds it).
  Staging keeps ``income``: the commit, not the import, retypes an unmatched uncategorised
  payment to an unpaired ``transfer`` (ADR-0002 rule 7c). Do NOT "fix" this to ``transfer``
  at import time.
* Zero-paise rows are dropped before fingerprinting — a zero-amount
  transaction is not a transaction (ADR-0009 rule 3) — and counted in
  ``zero_amount_count``, never in ``skipped``, which counts duplicates only
  (ADR-0010 rule 5).
* Row dedup is a per-fingerprint **multiset difference**, not set membership
  (ADR-0006): the DB holds ``n_db`` rows for a fingerprint, the file yields
  ``n_file``, and ``max(0, n_file - n_db)`` are staged with ascending
  ``occurrence``. Two identical rows in one statement are two distinct events
  (two auto rides at the same fare on one day), so both import. This reduces to
  the old membership test whenever every count is 0 or 1.
* Re-upload of the same file (matched by ``source_file_hash``) **reconciles**
  the file against what is currently in the DB: it re-parses and reuses the
  prior batch, re-staging (as pending) only the rows whose fingerprint is not
  already present as a ``Transaction``. Rows the user discarded in the review
  queue (or lost to a partial cancel) therefore re-surface; rows still present
  (committed or still-pending) are skipped. If every row is already present the
  batch gains nothing and ``pending_count`` reflects only whatever was already
  pending. ``already_imported`` means "a completed batch for this file hash
  already existed" — the file was seen before — **not** "the upload was a no-op".
  Because we now always re-parse, a password-protected PDF must supply its
  password on re-upload (missing-ness is defined per parsed-row fingerprint, so
  there is no way to reconcile without parsing).
* "Missing" is judged on ``COALESCE(origin_fingerprint, fingerprint)``, not on
  ``fingerprint`` alone (ADR-0007 rule 9). Identity columns are editable, so a row
  whose amount / date / merchant / account the user corrected still carries the
  hash of the statement line it came from and is therefore still accounted for —
  an **edit** does not masquerade as a deletion. A **cancel** still does re-surface
  the row, which is the documented contract above and deliberately unchanged.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.log_config import get_logger
from app.models import Account, ImportBatch, Transaction
from app.models.transaction import TransactionTypeStr
from app.parsers import (
    AxisBank,
    AxisCC,
    HdfcBank,
    IciciBank,
    IciciCC,
    ParsedStatement,
    RawTransaction,
    SbiBank,
    StatementParser,
)
from app.parsers.base import parse_statement
from app.services.category_service import CASHBACK_CATEGORY_NAME, default_category_id
from app.services.fingerprint import transaction_fingerprint
from app.services.merchant import merchant_too_long, normalize_merchant
from app.services.merchant_alias import EMPTY_RESOLVER, load_alias_resolver
from app.services.merchant_labels import prefetch_label_map
from app.services.occurrence import OccurrenceAllocator
from app.services.reconciliation_service import is_card_bill_debit, reconcile_batch
from app.services.tag_service import AUTO_TAGGABLE_TYPES, prefetch_tag_map
from app.services.transaction_labels import link_labels

logger = get_logger(__name__)


class AccountNotFoundError(LookupError):
    """``account_id`` is not found, not owned by the current user, or archived."""


class ParserNotRegisteredError(LookupError):
    """No parser entry in :data:`PARSERS` for ``(account.issuer, account.type)``."""


class NonInrAccountError(ValueError):
    """``account_id`` is a non-INR account — v1 statement import is INR-only.

    Distinct from :class:`AccountNotFoundError` (a USD account is *found*, not
    missing) so the route gives it its own 422 detail.
    """


class StatementMerchantTooLongError(ValueError):
    """A statement row's merchant will not fit a merchant column (ADR-0006 rule 3b).

    ``row`` is the 1-based ordinal in the parser's output. It is the whole payload: the
    message names a position and never statement text, so the route can return and log
    it, as with :class:`~app.parsers.base.RunningBalanceMismatchError` (ADR-0010 rule 11).
    """

    def __init__(self, *, row: int) -> None:
        self.row = row
        super().__init__(f"merchant on statement row {row} is longer than 512 characters")


PARSERS: dict[tuple[str, str], type[StatementParser]] = {
    ("axis", "credit_card"): AxisCC,
    ("icici", "credit_card"): IciciCC,
    ("hdfc", "bank"): HdfcBank,
    ("axis", "bank"): AxisBank,
    ("icici", "bank"): IciciBank,
    ("sbi", "bank"): SbiBank,
}


# CC issuers we have a registered statement parser for. Single source of truth
# for the account-creation guard (app.api.v1.accounts) — derived from PARSERS so
# adding a parser automatically widens the allowed set. Sorted for stable error text.
SUPPORTED_CC_ISSUERS: tuple[str, ...] = tuple(
    sorted(issuer for (issuer, atype) in PARSERS if atype == "credit_card")
)

# Bank issuers with a registered statement parser; the same guard, for bank accounts.
SUPPORTED_BANK_ISSUERS: tuple[str, ...] = tuple(
    sorted(issuer for (issuer, atype) in PARSERS if atype == "bank")
)

# The brand label the UI shows for each parser's issuer id. Beside PARSERS so adding a parser
# means adding its label here and nowhere in the frontend; a test fails if one is missing.
ISSUER_LABELS: dict[str, str] = {"axis": "Axis", "icici": "ICICI", "hdfc": "HDFC", "sbi": "SBI"}


def statement_import_supported(account_type: str, issuer: str | None) -> bool:
    """Whether a statement for an account of this type and issuer has a parser.

    The same ``(issuer, type)`` lookup :func:`import_statement` dispatches on, so the answer
    the API gives is exactly what an upload would find.
    """
    return issuer is not None and (issuer, account_type) in PARSERS


def statement_issuers() -> list[tuple[str, str, str]]:
    """Every ``(account_type, issuer, label)`` a statement parser exists for, sorted."""
    return sorted((atype, issuer, ISSUER_LABELS[issuer]) for (issuer, atype) in PARSERS)


@dataclass(frozen=True, slots=True)
class ImportResult:
    batch_id: int
    imported: int
    skipped: int
    zero_amount_count: int
    already_imported: bool
    # Rows on this batch still awaiting review (confirmed_at IS NULL) after the
    # import. The frontend routes on this: >0 → open the review queue, 0 → nothing
    # to review (all rows already in expenses, or none that are transactions). Counts
    # newly-staged rows plus any that were already pending on the batch.
    pending_count: int
    # AN other account this byte-identical file was already imported into (there may
    # be more than one — this reports one, deterministically the lowest batch id).
    # None when the file is new, or when the only prior import was into THIS account
    # (``already_imported`` owns that case). Surfaces the wrong-account mis-import the
    # per-account F4 dedup scope cannot catch. An id, never a name — account names are
    # user data and this value reaches an HTTP body (see core/log_config masking posture).
    duplicate_of_account_id: int | None = None
    # Whether that other account has since been archived. GET /accounts filters
    # ``archived_at IS NULL``, so the id alone is not resolvable to a label by the
    # frontend in that case — it falls back to "an archived account".
    duplicate_of_account_archived: bool = False
    # Balance reconciliation (PRD §F1/§F4a): None = not checked (no usable
    # statement metadata, or an account-less batch); 0 = reconciled; non-zero =
    # mismatch, this many paise. See reconciliation_service.reconcile_batch.
    reconciliation_delta_paise: int | None = None


def load_statement_account(session: Session, *, user_id: UUID, account_id: int) -> Account:
    """The caller's live INR account a statement may be staged into.

    Raises:
        AccountNotFoundError: account not found, not owned by ``user_id``, or archived.
        NonInrAccountError: account is non-INR (v1 spending is INR-only).
    """
    # ``archived_at IS NULL`` matches the four sibling account pre-flights — transactions.py:204,
    # transactions.py:346, accounts.py:137 and accounts.py:222 — which all refuse an archived
    # account. Without it this was the one transaction-write path that accepted one, reachable
    # through a stale cross-tab ["accounts"] cache: the rows committed onto an account
    # GET /accounts will never return, counted in net worth by design but unselectable in the
    # /expenses filter and rendered with an em-dash.
    account = session.scalar(
        select(Account).where(
            Account.id == account_id,
            Account.user_id == user_id,
            Account.archived_at.is_(None),
        )
    )
    if account is None:
        raise AccountNotFoundError(f"account_id={account_id} not found for user")
    if account.currency != "INR":
        # v1 spending is INR-only; a USD account's cents would be imported as INR
        # paise (the parsers emit INR magnitudes). Fails fast before parser dispatch.
        raise NonInrAccountError(f"account_id={account_id} currency={account.currency}")
    return account


def parser_for(account: Account) -> type[StatementParser]:
    """The registered parser for ``(account.issuer, account.type)``.

    Reads :data:`PARSERS` at call time (tests monkeypatch it).

    Raises:
        ParserNotRegisteredError: no parser registered for the pair.
    """
    parser_cls = PARSERS.get((account.issuer or "", account.type))
    if parser_cls is None:
        raise ParserNotRegisteredError(
            f"no parser for issuer={account.issuer!r} type={account.type!r}"
        )
    return parser_cls


def import_statement(
    *,
    user_id: UUID,
    account_id: int,
    file_bytes: bytes,
    password: str | None,
    session: Session,
) -> ImportResult:
    """Parse a statement, dedup, persist rows + an ImportBatch.

    The bytes and password never leave this call (ADR-0010 rule 13); the batch keeps only
    ``source_file_hash``.

    Raises:
        AccountNotFoundError: account not found, not owned by ``user_id``, or archived.
        NonInrAccountError: account is non-INR (v1 spending is INR-only).
        ParserNotRegisteredError: no parser registered for
            ``(account.issuer, account.type)``.
        ParserError / InvalidPasswordError: propagated from the parser;
            ``parse_statement`` turns any other parser exception into a
            ``ParserError`` that logs only its type (A52).
        StatementMerchantTooLongError: a row's merchant is past 512 characters, raw or
            normalized (ADR-0006 rule 3b); nothing is staged.
    """
    account = load_statement_account(session, user_id=user_id, account_id=account_id)
    parser_cls = parser_for(account)
    statement = parse_statement(parser_cls, file_bytes, password)
    return persist_statement(
        session,
        user_id=user_id,
        account=account,
        parser_name=parser_cls.__name__,
        source_file_hash=hashlib.sha256(file_bytes).hexdigest(),
        statement=statement,
    )


def persist_statement(
    session: Session,
    *,
    user_id: UUID,
    account: Account,
    parser_name: str,
    source_file_hash: str,
    statement: ParsedStatement,
) -> ImportResult:
    """Stage an already-parsed statement: dedup, persist rows + an ImportBatch.

    Shared by the upload path (:func:`import_statement`) and the bundled sample statements,
    which compose the parser's ``interpret_*`` steps themselves. The caller commits.

    Raises:
        StatementMerchantTooLongError: a row's merchant is past 512 characters, raw or
            normalized (ADR-0006 rule 3b); the caller must not commit the flushed batch.
    """
    account_id = account.id

    # Re-upload reconciliation: reuse the prior completed batch instead of
    # short-circuiting, so the dedup loop below re-stages rows missing from the
    # DB (discarded / partially-cancelled) while skipping ones still present.
    existing_batch = session.scalar(
        select(ImportBatch).where(
            ImportBatch.user_id == user_id,
            ImportBatch.account_id == account_id,
            ImportBatch.source_file_hash == source_file_hash,
            ImportBatch.status == "completed",
        )
    )
    already_imported = existing_batch is not None

    # Wrong-account guard (UX-09b). A SECOND, additive probe: the same lookup minus
    # the account_id predicate, so it finds a completed batch for this exact file on
    # a DIFFERENT account of the same user. Deliberately separate from the query
    # above — that one's account scoping is what makes cross-account isolation hold,
    # so it is not relaxed. Not an error: the import proceeds and the frontend warns
    # before the user commits.
    #
    # ORDER BY + LIMIT 1 is required, not cosmetic: a file present in 2+ other
    # accounts would otherwise return an arbitrary row per plan choice.
    other_batch = session.scalar(
        select(ImportBatch)
        .where(
            ImportBatch.user_id == user_id,
            ImportBatch.account_id != account_id,
            ImportBatch.source_file_hash == source_file_hash,
            ImportBatch.status == "completed",
        )
        .order_by(ImportBatch.id)
        .limit(1)
    )
    duplicate_of_account_id = other_batch.account_id if other_batch is not None else None
    duplicate_of_account_archived = False
    if other_batch is not None:
        # Resolve archived-ness so the frontend knows the id won't appear in
        # GET /accounts (which filters archived_at IS NULL) and can say so.
        duplicate_of_account_archived = (
            session.scalar(
                select(Account.archived_at).where(
                    Account.id == other_batch.account_id,
                    Account.user_id == user_id,
                )
            )
            is not None
        )

    if existing_batch is not None:
        batch = existing_batch
    else:
        batch = ImportBatch(
            user_id=user_id,
            account_id=account_id,
            source_file_hash=source_file_hash,
            parser_name=parser_name,
            status="pending",
        )
        session.add(batch)
        session.flush()

    rows = statement.rows

    # Pass 1 — identity only. Normalizing and fingerprinting before the persist loop
    # is what lets the prefetch below scope itself to THIS FILE's fingerprints (see
    # there); the loop used to do both inline, which forced a date-window proxy.
    # Zero-paise rows are dropped here — a zero-amount transaction is not a
    # transaction — and counted in `zero_amount_count`, so they never reach the
    # allocator and never count as duplicates.
    zero_amount_count = 0
    parsed: list[tuple[RawTransaction, str, str]] = []
    for idx, row in enumerate(rows):
        if row.amount_paise == 0:
            logger.debug("import_service: skipping zero-paise row", idx=idx)
            zero_amount_count += 1
            continue
        if merchant_too_long(row.merchant_raw):
            logger.warning("statement_merchant_too_long", row=idx + 1)
            raise StatementMerchantTooLongError(row=idx + 1)
        normalized = normalize_merchant(row.merchant_raw)
        parsed.append(
            (
                row,
                normalized,
                transaction_fingerprint(
                    txn_date=row.date,
                    amount_paise=row.amount_paise,
                    normalized_merchant=normalized,
                    account_id=account_id,
                ),
            )
        )
    file_fps = {fp for _, _, fp in parsed}

    # Pre-fetch, per dedup key, HOW MANY rows already exist and the highest
    # occurrence in use — the persist loop then does a multiset difference
    # (ADR-0006) rather than a set-membership test, so N identical rows in one
    # statement produce N transactions instead of one.
    #
    # TWO KEYS, deliberately (ADR-0007 rule 9 + its implementation notes):
    #
    # * COUNT groups on COALESCE(origin_fingerprint, fingerprint) — the row's
    #   *source line*. That is what makes an edited row still account for the file
    #   line it came from, instead of the importer reading the edit as a deletion.
    # * MAX(occurrence) groups on the CURRENT fingerprint, because that — not the
    #   coalesced value — is what `uq_transactions_user_account_fingerprint`
    #   actually constrains. Grouping both on the coalesced key hides a row from
    #   its own fingerprint's MAX, so the allocator can re-issue an occupied
    #   ordinal; there is no per-row SAVEPOINT here to recover from the resulting
    #   IntegrityError (see :mod:`app.services.occurrence`), so that is a 500 that
    #   fails the whole batch. Reachable whenever an edit moves a row's fingerprint
    #   INTO a group whose coalesced key is elsewhere.
    #
    # SCOPED TO THE FILE'S FINGERPRINTS, not to the account and a date window.
    # Both of those became unsafe the moment identity columns turned editable:
    # origin_fingerprint freezes the row's ORIGINAL date and account while the row
    # now stores the corrected ones, so a date fixed outside the statement's period
    # — or a row moved to another account — would fall outside the old scope, its
    # provenance would go unread, and the file line would re-stage as a duplicate.
    # Matching on the fingerprint set is exact instead of a proxy: the hash already
    # encodes account_id (PRD §F4), so a file fingerprint can only ever belong to
    # this account, and both scoping predicates were redundant given this one. The
    # two IN lists are `COALESCE(...) IN fps OR fingerprint IN fps` written without
    # the COALESCE so both stay sargable; load is bounded by the file (2 binds per
    # parsed row — CC statements run 10-100 rows), not by account history.
    existing_counts: dict[str, tuple[int, int]] = {}
    if file_fps:
        counts: dict[str, int] = {}
        max_occ: dict[str, int] = {}
        for dedup_key, fingerprint, count, row_max_occ in session.execute(
            select(
                func.coalesce(Transaction.origin_fingerprint, Transaction.fingerprint),
                Transaction.fingerprint,
                func.count(),
                func.max(Transaction.occurrence),
            )
            .where(
                Transaction.user_id == user_id,
                or_(
                    Transaction.origin_fingerprint.in_(file_fps),
                    Transaction.fingerprint.in_(file_fps),
                ),
            )
            .group_by(
                func.coalesce(Transaction.origin_fingerprint, Transaction.fingerprint),
                Transaction.fingerprint,
            )
        ):
            counts[dedup_key] = counts.get(dedup_key, 0) + count
            max_occ[fingerprint] = max(max_occ.get(fingerprint, -1), row_max_occ)
        existing_counts = {fp: (counts.get(fp, 0), max_occ.get(fp, -1)) for fp in file_fps}

    # Auto-tag prefetch (PRD §F3 / ADR-0011 merchant-alias layer). One
    # user-scoped SELECT for the alias table (empty guard mirrors the two
    # prefetches below — nothing to resolve when the file yielded no rows),
    # then both prefetches aggregate onto whichever canonical it resolves to.
    # v2 Postgres: could batch with the dedup prefetch in a CTE; not worth it
    # in v1 SQLite.
    resolver = load_alias_resolver(session, user_id=user_id) if rows else EMPTY_RESOLVER
    tag_map = prefetch_tag_map(session, user_id=user_id, resolver=resolver) if rows else {}
    # F3a Phase 2: prefetch the merchant→label map (labels with hit_count ≥
    # LABEL_PREFILL_MIN, summed across every raw descriptor an alias folds
    # together). Same one-SELECT-then-dict-lookup shape as tag_map.
    label_map = prefetch_label_map(session, user_id=user_id, resolver=resolver) if rows else {}
    # The user's active spend "Cashback" (ADR-0013 rule 2), looked up once. None
    # if they archived or renamed it: a cashback credit is then staged like any
    # other positive spend (auto-tag, else spend "Other" at commit).
    cashback_id = (
        default_category_id(session, user_id=user_id, name=CASHBACK_CATEGORY_NAME, kind="spend")
        if rows
        else None
    )
    # Collected (txn, [label_id, ...]) for prefilled rows — the join rows are
    # inserted after the post-loop flush assigns txn.id (below).
    label_prefills: list[tuple[Transaction, list[int]]] = []

    # Pass 2 — allocate + persist, in file order (the allocator's per-file tally
    # depends on it).
    imported = 0
    skipped = 0
    allocator = OccurrenceAllocator(existing_counts)
    for row, normalized, fp in parsed:
        # `is None` — occurrence 0 is a valid first sighting (see the allocator's
        # docstring). `skipped` is this importer's own counter and counts duplicates only.
        occurrence = allocator.allocate(fp)
        if occurrence is None:
            skipped += 1
            continue

        txn_type = _map_type(row)
        # Resolved once per row, reused for both lookups below — the two
        # prefetches are keyed on the canonical, never the raw
        # merchant_normalized (ADR-0011). merchant_normalized itself is never
        # overwritten with the canonical; it still feeds the F4 fingerprint.
        canonical = resolver.canonical(normalized)
        # A flagged card-bill debit on a bank is never prefilled (ADR-0004 rule 8): a rule learned
        # from one bill must not decide the next, which commits as a transfer or Other (ADR-0002
        # rule 7b).
        card_bill_debit = (
            account.type == "bank" and row.amount_paise < 0 and is_card_bill_debit(normalized)
        )
        auto_category_id = (
            tag_map.get(canonical)
            if txn_type in AUTO_TAGGABLE_TYPES and not card_bill_debit
            else None
        )
        category_id = auto_category_id
        if (
            cashback_id is not None
            and txn_type == "spend"
            and row.amount_paise > 0
            and is_cashback_credit(row.merchant_raw)
        ):
            # A credit naming cashback goes to spend "Cashback" whatever the
            # merchant's tag rule says (the credit rarely names the purchase it
            # rewards). It is a keyword default, not an F3 suggestion, so the
            # acceptance metric's auto_category_id is left NULL (ADR-0013).
            category_id = cashback_id
            auto_category_id = None

        txn = Transaction(
            user_id=user_id,
            account_id=account_id,
            date=row.date,
            amount_paise=row.amount_paise,
            transaction_type=txn_type,
            merchant_raw=row.merchant_raw,
            merchant_normalized=normalized,
            category_id=category_id,
            # Freeze the suggestion for the acceptance-rate metric. Mirrors
            # category_id here (except a cashback prefill, above); the two diverge
            # later iff the user edits the category before/after commit (PATCH
            # leaves auto_category_id).
            auto_category_id=auto_category_id,
            fingerprint=fp,
            # ADR-0007 rule 9 — stamped at STAGE time, not at commit, so editing a
            # still-pending row and re-uploading the file before committing it still
            # matches on the coalesced key. Equal to `fingerprint` at birth; the two
            # diverge only when a PATCH edits an identity column. Never rewritten.
            origin_fingerprint=fp,
            occurrence=occurrence,
            source="import",
            import_batch_id=batch.id,
        )
        session.add(txn)
        # F3a Phase 2 prefill: same spend-type gate as the category auto-tag.
        # Labels come from the user's own map, so no get-or-create is needed —
        # just the join inserts, staged until txn.id exists (post-loop flush).
        prefill_label_ids = label_map.get(canonical, []) if txn_type in AUTO_TAGGABLE_TYPES else []
        if prefill_label_ids:
            label_prefills.append((txn, prefill_label_ids))
        imported += 1

    # Batch counters record the FIRST import's outcome. A re-upload leaves them
    # untouched: accumulating would inflate skipped_count on every "nothing new"
    # re-upload and double-count resurfaced rows (both cosmetic-but-misleading on
    # the review screen). The returned ``imported`` is always the newly-staged
    # count so the frontend message stays accurate.
    if not already_imported:
        batch.imported_count = imported
        batch.skipped_count = skipped
        batch.status = "completed"

    # Flush the loop's inserts so the count below sees them — the session is
    # autoflush=False (services flush explicitly), so the count query would
    # otherwise miss the just-added rows. Also assigns txn.id for the F3a
    # label-prefill join inserts below.
    session.flush()

    # F3a Phase 2: write the prefilled labels onto the pending rows now that
    # txn.id exists — insert-only (fresh rows have no existing links) via the
    # shared link_labels primitive. The caller commits.
    # v2-race note: a label hard-deleted between prefetch_label_map and this
    # insert would FK-violate. Impossible in v1 — prefetch + insert share one
    # uncommitted transaction with no second actor, and SQLite has a single
    # writer. Deferred-to-Postgres-v2, same class as the dedup-prefetch race above.
    for txn, prefill_label_ids in label_prefills:
        link_labels(session, txn_id=txn.id, user_id=user_id, label_ids=prefill_label_ids)

    # Balance reconciliation (PRD §F1/§F4a, migration 0030). Stamp the statement's
    # own metadata on EVERY import, including a re-upload — unlike imported_count /
    # skipped_count / status above, this isn't "the first import's outcome": same
    # file in, same metadata out, and a re-upload repairs a batch stamped before
    # this shipped. period_* is stored exactly as printed (None when the PDF has
    # none); reconcile_batch reads these columns, computing any fallback window
    # without writing it back (ADR-0010 rule 8), so this assignment runs first.
    batch.statement_opening_balance_paise = statement.summary.opening_balance_paise
    batch.statement_closing_balance_paise = statement.summary.closing_balance_paise
    batch.period_start = statement.summary.period_start
    batch.period_end = statement.summary.period_end
    batch.reconciliation_delta_paise = reconcile_batch(session, user_id=user_id, batch=batch)

    # Rows on this batch still awaiting review after the import — the frontend's
    # routing signal. user_id guard is defense-in-depth (batch id is already
    # user-scoped), matching the other scoped queries in the imports router.
    pending_count = (
        session.scalar(
            select(func.count())
            .select_from(Transaction)
            .where(
                Transaction.user_id == user_id,
                Transaction.import_batch_id == batch.id,
                Transaction.confirmed_at.is_(None),
            )
        )
        or 0
    )

    # Import telemetry (PRD §Production-grade essentials). request_id / user_id
    # are inherited from the HTTP middleware's contextvars when called via the
    # API (absent for any non-HTTP caller — acceptable).
    logger.info(
        "import_completed",
        import_batch_id=batch.id,
        parser=parser_name,
        rows_in=len(rows),
        rows_imported=imported,
        rows_skipped=skipped,
        # Zero-amount lines are dropped by rule (ADR-0009 rule 3), not duplicates.
        rows_rejected=zero_amount_count,
        already_imported=already_imported,
        pending_count=pending_count,
        # Aggregate rupee figure, not a merchant or account number — PII-safe.
        reconciliation_delta_paise=batch.reconciliation_delta_paise,
    )

    return ImportResult(
        batch_id=batch.id,
        imported=imported,
        skipped=skipped,
        zero_amount_count=zero_amount_count,
        already_imported=already_imported,
        pending_count=pending_count,
        duplicate_of_account_id=duplicate_of_account_id,
        duplicate_of_account_archived=duplicate_of_account_archived,
        reconciliation_delta_paise=batch.reconciliation_delta_paise,
    )


_CASHBACK_RE = re.compile(r"CASHBACK", re.IGNORECASE)


def is_cashback_credit(merchant_raw: str) -> bool:
    """Whether ``merchant_raw`` names this credit as cashback (ADR-0013).

    A plain keyword, not per-issuer regex vocabulary (unlike ``_REFUND_RE`` in
    each parser) — the word appears verbatim across issuers, so one check here
    covers every parser rather than duplicating it per statement format. Picks
    the spend "Cashback" prefill at staging (:func:`import_statement`), and is
    exported so ``app.api.v1.imports.commit_import_batch`` can recognise that
    prefill at commit and not teach it — one rule, not two copies that can drift.
    """
    return bool(_CASHBACK_RE.search(merchant_raw))


def _map_type(row: RawTransaction) -> TransactionTypeStr:
    """Parser TxnType → model transaction_type.

    ``payment`` folds to ``income`` (the F4a input contract; the commit retypes an
    uncategorised flagged one to an unpaired ``transfer``, ADR-0002 rule 7c, so staging
    stays ``income``) and so does a bank ``deposit``, unless its text names cashback, which
    is a rebate and so a positive ``spend`` (ADR-0009 rule 7, ADR-0013 rule 1); **everything
    else is ``spend``**.
    A parser ``refund`` needs no branch of its own — a refund IS a ``spend`` row with a
    positive amount (ADR-0009), and the parser already hands over that positive sign
    (``RawTransaction.__post_init__`` enforces ``refund >= 0``). A cashback credit is a
    positive ``spend`` too: a rebate on purchases, never income (ADR-0013 rule 1). So the
    sign carries refund-ness and this function never has to guess at it.

    Zero-paise rows are filtered out upstream, so the ``other`` branch is
    always strictly signed: negative is a spend the parser's vocabulary didn't
    otherwise match; positive is a refund or cashback credit. Defaulting an
    unmatched positive credit to income would inflate spend by the credit's
    full magnitude (PRD §F4a note 3). Which category a cashback credit lands in
    is :func:`import_statement`'s job, not this one's.
    """
    if row.txn_type == "payment":
        return "income"  # F4a input contract — see module docstring; do NOT change to "transfer"
    if row.txn_type == "deposit" and not is_cashback_credit(row.merchant_raw):
        return "income"
    return "spend"
