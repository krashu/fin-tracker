"""Import orchestration: parse → fingerprint → dedup → persist.

Public surface:

* :data:`PARSERS` — dispatch table from ``(issuer, account_type)`` to parser class.
* :class:`ImportResult` — frozen value object returned to the route.
* :func:`import_statement` — the orchestrator.

Caller (the route) owns the commit. This module uses the caller's
session and never calls :meth:`Session.commit` itself, matching the
"routes commit, services use the caller's session" rule from the
project CLAUDE.md.

Decisions locked in for v1 (see :file:`docs/` ADRs when they land):

* CC-only. Parser ``payment`` rows are imported as ``income``; this is the
  input contract for F4a reconciliation (``reconciliation_service.auto_link_cc_bill``
  gates on ``transaction_type == "income"`` and flips matched CC-bill pairs
  to ``transfer`` at commit). An unmatched payment correctly stays ``income``
  — a lone ``transfer`` leg would violate ADR-0002's exactly-two-pairing
  invariant. Do NOT "fix" this to ``transfer`` at import time.
* Zero-paise rows are skipped before fingerprinting — a zero-amount
  transaction is not a transaction.
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
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.log_config import get_logger
from app.models import Account, ImportBatch, Transaction
from app.models.transaction import TransactionTypeStr
from app.parsers import AxisCC, IciciCC, RawTransaction, StatementParser
from app.services.fingerprint import transaction_fingerprint
from app.services.merchant import normalize_merchant
from app.services.merchant_labels import prefetch_label_map
from app.services.occurrence import OccurrenceAllocator
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
    missing) so the route can map it to 422, not 404.
    """


PARSERS: dict[tuple[str, str], type[StatementParser]] = {
    ("axis", "credit_card"): AxisCC,
    ("icici", "credit_card"): IciciCC,
}


# CC issuers we have a registered statement parser for. Single source of truth
# for the account-creation guard (app.api.v1.accounts) — derived from PARSERS so
# adding a parser automatically widens the allowed set. Sorted for stable error text.
SUPPORTED_CC_ISSUERS: tuple[str, ...] = tuple(
    sorted(issuer for (issuer, atype) in PARSERS if atype == "credit_card")
)


@dataclass(frozen=True, slots=True)
class ImportResult:
    batch_id: int
    imported: int
    skipped: int
    already_imported: bool
    # Rows on this batch still awaiting review (confirmed_at IS NULL) after the
    # import. The frontend routes on this: >0 → open the review queue, 0 → "all
    # transactions already in expenses". Counts newly-staged rows plus any that
    # were already pending on the batch.
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


def import_statement(
    *,
    user_id: UUID,
    account_id: int,
    file_bytes: bytes,
    password: str | None,
    session: Session,
) -> ImportResult:
    """Parse a statement, dedup, persist rows + an ImportBatch.

    Raises:
        AccountNotFoundError: account not found, not owned by ``user_id``, or archived.
        NonInrAccountError: account is non-INR (v1 spending is INR-only).
        ParserNotRegisteredError: no parser registered for
            ``(account.issuer, account.type)``.
        ParserError / InvalidPasswordError: propagated from the parser.
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

    parser_cls = PARSERS.get((account.issuer or "", account.type))
    if parser_cls is None:
        raise ParserNotRegisteredError(
            f"no parser for issuer={account.issuer!r} type={account.type!r}"
        )

    source_file_hash = hashlib.sha256(file_bytes).hexdigest()

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
            parser_name=parser_cls.__name__,
            status="pending",
        )
        session.add(batch)
        session.flush()

    rows = parser_cls.parse(file_bytes, password)

    # Pass 1 — identity only. Normalizing and fingerprinting before the persist loop
    # is what lets the prefetch below scope itself to THIS FILE's fingerprints (see
    # there); the loop used to do both inline, which forced a date-window proxy.
    # Zero-paise rows are dropped here — a zero-amount transaction is not a
    # transaction — so they never reach the allocator, exactly as before.
    skipped = 0
    parsed: list[tuple[RawTransaction, str, str]] = []
    for idx, row in enumerate(rows):
        if row.amount_paise == 0:
            logger.debug("import_service: skipping zero-paise row", idx=idx)
            skipped += 1
            continue
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

    # Auto-tag prefetch (PRD §F3). One SELECT, dict lookup per row. Mirrors the
    # dedup prefetch above, including its empty-parse guard — nothing to look up
    # when the file yielded no rows. v2 Postgres: could batch with the dedup
    # prefetch in a CTE; not worth it in v1 SQLite.
    tag_map = prefetch_tag_map(session, user_id=user_id) if rows else {}
    # F3a Phase 2: prefetch the merchant→label map (labels with hit_count ≥
    # LABEL_PREFILL_MIN). Same one-SELECT-then-dict-lookup shape as tag_map.
    label_map = prefetch_label_map(session, user_id=user_id) if rows else {}
    # Collected (txn, [label_id, ...]) for prefilled rows — the join rows are
    # inserted after the post-loop flush assigns txn.id (below).
    label_prefills: list[tuple[Transaction, list[int]]] = []

    # Pass 2 — allocate + persist, in file order (the allocator's per-file tally
    # depends on it).
    imported = 0
    allocator = OccurrenceAllocator(existing_counts)
    for row, normalized, fp in parsed:
        # `is None` — occurrence 0 is a valid first sighting (see the allocator's
        # docstring). `skipped` is this importer's own counter and already carries the
        # zero-paise rows from pass 1, which is why it doesn't move into the allocator.
        occurrence = allocator.allocate(fp)
        if occurrence is None:
            skipped += 1
            continue

        txn_type = _map_type(row)
        auto_category_id = tag_map.get(normalized) if txn_type in AUTO_TAGGABLE_TYPES else None

        txn = Transaction(
            user_id=user_id,
            account_id=account_id,
            date=row.date,
            amount_paise=row.amount_paise,
            transaction_type=txn_type,
            merchant_raw=row.merchant_raw,
            merchant_normalized=normalized,
            category_id=auto_category_id,
            # Freeze the suggestion for the acceptance-rate metric. Mirrors
            # category_id here; the two diverge later iff the user edits the
            # category before/after commit (PATCH leaves auto_category_id).
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
        # F3a Phase 2 prefill: same spend/refund gate as the category auto-tag.
        # Labels come from the user's own map, so no get-or-create is needed —
        # just the join inserts, staged until txn.id exists (post-loop flush).
        prefill_label_ids = label_map.get(normalized, []) if txn_type in AUTO_TAGGABLE_TYPES else []
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
        parser=parser_cls.__name__,
        rows_in=len(rows),
        rows_imported=imported,
        rows_skipped=skipped,
        already_imported=already_imported,
        pending_count=pending_count,
    )

    return ImportResult(
        batch_id=batch.id,
        imported=imported,
        skipped=skipped,
        already_imported=already_imported,
        pending_count=pending_count,
        duplicate_of_account_id=duplicate_of_account_id,
        duplicate_of_account_archived=duplicate_of_account_archived,
    )


def _map_type(row: RawTransaction) -> TransactionTypeStr:
    """Parser TxnType → model transaction_type (CC-only, v1).

    Zero-paise rows are filtered out upstream, so the ``other`` branch
    always lands on a strictly-signed comparison.
    """
    if row.txn_type == "purchase":
        return "spend"
    if row.txn_type == "payment":
        return "income"  # F4a input contract — see module docstring; do NOT change to "transfer"
    if row.txn_type == "refund":
        return "refund"
    return "spend" if row.amount_paise < 0 else "income"
