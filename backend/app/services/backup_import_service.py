"""Backup import — additive, deduped load of a spend backup zip (PRD §F10).

The load side of :mod:`app.services.export_service`. Deliberately **additive**, not a
wipe-restore: re-importing a backup that overlaps the current DB skips the duplicates and adds
only what's new, so it's non-destructive by construction and fits "I backed up, added more
rows, now I re-import the backup". Mirrors :mod:`app.services.investment_import_service`
structurally (parse → upsert refs → dedup → persist, route owns the commit), but the
money/identity details differ — see the two load-bearing rules below.

* **Fingerprint is recomputed here, never trusted from the file.** The dedup key bakes in
  ``account_id`` (:func:`app.services.fingerprint.transaction_fingerprint`), which is
  install-specific. Recomputing it from the *resolved* target ``account_id`` (over a re-normalized
  ``merchant_normalized``, never the file's cell as-is) reproduces exactly what a native row
  hashed, so a backup row dedups against native rows and survives a fresh-DB restore.
  ``occurrence`` (ADR-0006) is likewise **re-derived, never carried in the file**, for the same
  install-local reason — and it need not be exported, because round-trip identity is the
  multiset *cardinality* per fingerprint, not the specific integers: a source DB holding
  occurrences ``{0, 5, 9}`` restores as ``{0, 1, 2}`` with the same rows and the same
  fingerprints. Dedup is therefore a multiset difference, so a backup carrying two
  genuinely-distinct identical rows restores both.
* **Accounts/categories are match-or-create, never mutate.** Active account names are unique
  per user, so a name match with a *different* type/currency is rejected (a counted warning),
  never rebound; ``opening_balance_paise`` / ``type`` / ``currency`` on a matched account are
  left untouched (mutating opening balance would shift every historical rollup — the reason
  ``AccountUpdate`` locks them).

Transfer pairs are re-linked in a second pass over a backup-local ``transfer_group`` token
(NULL-then-update, the shape the self-referential composite FK requires) so restored transfers
don't become ADR-0002-violating orphans.

Row-level dedup is the only idempotency backstop — there is **no** whole-zip hash short-circuit
(the exporter stamps a timestamp, so byte-identical re-uploads are not a reliable signal). The
zip's ``source_file_hash`` is still recorded on the batch for the audit trail.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.log_config import get_logger
from app.models import Account, Category, ImportBatch, Transaction
from app.parsers.backup_csv import (
    ACCOUNTS_CSV,
    CATEGORIES_CSV,
    TRANSACTIONS_CSV,
    ParsedBackup,
    ParsedBackupAccount,
    ParsedBackupCategory,
    ParsedBackupTransaction,
    parse_backup_zip,
)
from app.services.fingerprint import transaction_fingerprint
from app.services.merchant import normalize_merchant
from app.services.occurrence import OccurrenceAllocator
from app.services.transaction_labels import (
    link_labels,
    normalize_label_name,
    resolve_label_names,
)

logger = get_logger(__name__)

_PARSER_NAME = "backup_csv"


@dataclass(frozen=True, slots=True)
class BackupImportResult:
    batch_id: int
    accounts_new: int
    accounts_matched: int
    categories_new: int
    categories_matched: int
    txns_imported: int
    txns_skipped_dupe: int
    rows_rejected: int
    transfers_relinked: int
    warnings: list[str]


def import_backup_zip(session: Session, *, user_id: UUID, file_bytes: bytes) -> BackupImportResult:
    """Parse a backup zip and persist it. Thin wrapper — the route owns the commit.

    Raises:
        BackupParseError: propagated from :func:`parse_backup_zip` (bad zip, missing member,
            non-UTF-8 CSV, missing required column).
    """
    source_file_hash = hashlib.sha256(file_bytes).hexdigest()
    parsed = parse_backup_zip(file_bytes)
    return persist_backup(
        session, user_id=user_id, parsed=parsed, source_file_hash=source_file_hash
    )


def persist_backup(
    session: Session, *, user_id: UUID, parsed: ParsedBackup, source_file_hash: str
) -> BackupImportResult:
    """Persist already-parsed backup rows: upsert accounts/categories → dedup+insert txns → relink.

    ``parsed.warnings`` (the parser's per-row skips) are merged into the returned warnings;
    ``rows_rejected`` is the total dropped across parse + resolution — a user-facing total
    spanning accounts, categories and transactions, deliberately NOT the ``rows_rejected`` in
    the ``import_completed`` event below, which counts dropped TRANSACTIONS only so its counts
    satisfy ``rows_in == imported + skipped + rejected``.
    """
    batch = ImportBatch(
        user_id=user_id,
        account_id=None,
        source_file_hash=source_file_hash,
        parser_name=_PARSER_NAME,
        status="pending",
    )
    session.add(batch)
    session.flush()

    accounts_new, accounts_matched, accounts_by_name, account_warnings = _upsert_accounts(
        session, user_id=user_id, rows=parsed.accounts
    )
    categories_new, categories_matched, categories_by_key, category_warnings = _upsert_categories(
        session, user_id=user_id, rows=parsed.categories
    )
    imported, skipped, relinked, txn_warnings = _persist_transactions(
        session,
        user_id=user_id,
        batch_id=batch.id,
        rows=parsed.transactions,
        accounts_by_name=accounts_by_name,
        categories_by_key=categories_by_key,
    )

    warnings = [*parsed.warnings, *account_warnings, *category_warnings, *txn_warnings]
    batch.imported_count = imported
    batch.skipped_count = skipped
    # Informational only (no hash short-circuit reads it): "failed" carries the documented
    # "needs attention" sense when any row was dropped.
    batch.status = "failed" if warnings else "completed"
    session.flush()

    # Import telemetry (PRD §Production-grade essentials), same field set as
    # import_service.import_statement. There is no short-circuit path here (see the module
    # docstring), so one event per restore.
    #
    # rows_in/rows_rejected are about TRANSACTIONS only: every row in _persist_transactions
    # leaves the loop as exactly one of imported / skipped_dupe / one txn_warning, so
    # rows_in == imported + skipped + rejected holds and a reader can sanity-check the
    # event. That is deliberately NOT BackupImportResult.rows_rejected, which also folds in
    # the three parsers' own skips plus account_warnings — neither of which is a
    # transaction row. Account/category upserts are counted separately for the same reason.
    logger.info(
        "import_completed",
        import_batch_id=batch.id,
        parser=_PARSER_NAME,
        rows_in=len(parsed.transactions),
        rows_imported=imported,
        rows_skipped=skipped,
        rows_rejected=len(txn_warnings),
        rows_parse_skipped=len(parsed.warnings),
        accounts_new=accounts_new,
        categories_new=categories_new,
        transfers_relinked=relinked,
        status=batch.status,
    )

    return BackupImportResult(
        batch_id=batch.id,
        accounts_new=accounts_new,
        accounts_matched=accounts_matched,
        categories_new=categories_new,
        categories_matched=categories_matched,
        txns_imported=imported,
        txns_skipped_dupe=skipped,
        rows_rejected=len(warnings),
        transfers_relinked=relinked,
        warnings=warnings,
    )


def _upsert_accounts(
    session: Session, *, user_id: UUID, rows: list[ParsedBackupAccount]
) -> tuple[int, int, dict[str, Account], list[str]]:
    """Resolve each backup account name to an ``Account``, creating missing ones.

    Matches active accounts by name (their uniqueness scope). Active backup rows are processed
    first so a name shared by an archived + an active account (soft-delete + recreate)
    collapses onto the active one — a documented restore limitation. Never mutates a matched
    account. Returns ``(created, matched, {name: Account}, warnings)``.
    """
    existing_active: dict[str, Account] = {
        a.name: a
        for a in session.scalars(
            select(Account).where(Account.user_id == user_id, Account.archived_at.is_(None))
        )
    }
    resolved: dict[str, Account] = {}
    warnings: list[str] = []
    created = 0
    matched = 0
    # Active first (archived_at is None sorts before a set value) so active wins a name clash.
    for row in sorted(rows, key=lambda r: r.archived_at is not None):
        if row.name in resolved:
            continue
        existing = existing_active.get(row.name)
        if existing is not None:
            if existing.type != row.type or existing.currency != row.currency:
                warnings.append(
                    f"{ACCOUNTS_CSV} row {row.line_no}: account {row.name!r} exists with a "
                    "different type/currency — not rebinding"
                )
                continue
            resolved[row.name] = existing  # reuse as-is; never overwrite locked fields
            matched += 1
            continue
        account = Account(
            user_id=user_id,
            name=row.name,
            type=row.type,
            issuer=row.issuer,
            last4=row.last4,
            opening_balance_paise=row.opening_balance_paise,
            currency=row.currency,
            archived_at=row.archived_at,
        )
        session.add(account)
        resolved[row.name] = account
        created += 1
    session.flush()  # assign ids for the transaction FK
    return created, matched, resolved, warnings


def _upsert_categories(
    session: Session, *, user_id: UUID, rows: list[ParsedBackupCategory]
) -> tuple[int, int, dict[tuple[str, str], Category], list[str]]:
    """Resolve each backup ``(name, kind)`` to a ``Category``, creating missing ones.

    Restored categories carry ``is_seeded=False`` (user data, not app seeds). Subcategories
    link to their parent category via ``parent_id`` when ``parent_name`` is present — but a
    ``parent_name`` that cannot be resolved to a ROOT category flattens the row to a root
    instead (ADR-0012 caps depth at 2), and that flattening is reported in the returned
    warnings rather than silently counted as a success. Mirrors ``_upsert_accounts``' shape.
    Returns ``(created, matched, {(name, kind): Category}, warnings)``.
    """
    existing_active: dict[tuple[str, str], Category] = {
        (c.name, c.kind): c
        for c in session.scalars(
            select(Category).where(Category.user_id == user_id, Category.archived_at.is_(None))
        )
    }
    resolved: dict[tuple[str, str], Category] = {}
    warnings: list[str] = []
    created = 0
    matched = 0

    roots = [r for r in rows if not r.parent_name]
    subcats = [r for r in rows if r.parent_name]

    for row in sorted(roots, key=lambda r: r.archived_at is not None):
        key = (row.name, row.kind)
        if key in resolved:
            continue
        existing = existing_active.get(key)
        if existing is not None:
            resolved[key] = existing
            matched += 1
            continue
        category = Category(
            user_id=user_id,
            name=row.name,
            kind=row.kind,
            color=row.color,
            archived_at=row.archived_at,
            parent_id=None,
        )
        session.add(category)
        resolved[key] = category
        created += 1

    session.flush()  # assign root ids so subcategories can link to them

    for row in sorted(subcats, key=lambda r: r.archived_at is not None):
        key = (row.name, row.kind)
        if key in resolved:
            continue
        existing = existing_active.get(key)
        if existing is not None:
            resolved[key] = existing
            matched += 1
            continue
        parent = (
            resolved.get((row.parent_name, row.kind))
            or existing_active.get((row.parent_name, row.kind))
            if row.parent_name
            else None
        )
        if parent is not None and parent.parent_id is not None:
            # The named parent is itself a subcategory. Linking under it would build a
            # third level, which ADR-0012 caps out — the router enforces this on every
            # other write path, and the importer must hold the same line rather than
            # trusting a (possibly hand-edited) backup CSV. Flatten instead of nesting.
            warnings.append(
                f"{CATEGORIES_CSV} row {row.line_no}: category {row.name!r} — parent category "
                f"{row.parent_name!r} is itself a subcategory, restored as a root category "
                "(depth is capped at 2)"
            )
            parent = None
        elif row.parent_name and parent is None:
            warnings.append(
                f"{CATEGORIES_CSV} row {row.line_no}: category {row.name!r} — parent category "
                f"{row.parent_name!r} not found, restored as a root category"
            )
        parent_id = parent.id if parent is not None else None
        category = Category(
            user_id=user_id,
            name=row.name,
            kind=row.kind,
            color=row.color,
            archived_at=row.archived_at,
            parent_id=parent_id,
        )
        session.add(category)
        resolved[key] = category
        created += 1

    session.flush()  # assign ids for the transaction FK
    return created, matched, resolved, warnings


def _persist_transactions(
    session: Session,
    *,
    user_id: UUID,
    batch_id: int,
    rows: list[ParsedBackupTransaction],
    accounts_by_name: dict[str, Account],
    categories_by_key: dict[tuple[str, str], Category],
) -> tuple[int, int, int, list[str]]:
    """Recompute fingerprint → dedup → insert → relink transfer pairs.

    Returns ``(imported, skipped_dupe, transfers_relinked, warnings)``.
    """
    # How many rows already exist per dedup key, and the highest occurrence in use.
    # Dedup is a multiset difference, not set membership (ADR-0006): a backup holding two
    # genuinely-distinct identical rows restores both, where the old set-membership check
    # collapsed them to one. Unwindowed by design — the whole user history is the scope here,
    # unlike import_service, which bounds itself to the parsed file's fingerprints.
    #
    # TWO KEYS, for the reasons import_service's prefetch spells out at length (ADR-0007
    # rule 9): COUNT groups on COALESCE(origin_fingerprint, fingerprint) so an edited row
    # still accounts for the backup line it came from, while MAX(occurrence) groups on the
    # CURRENT fingerprint because that is what the uniqueness constraint actually holds —
    # coalescing both would hide a row from its own group's MAX and let the allocator
    # re-issue an occupied ordinal.
    #
    # The key is the bare fingerprint, no longer (account_id, fingerprint): the hash already
    # encodes account_id (PRD §F4), so the pair was redundant — and worse, it re-broke the
    # rule-9 match for a row the user moved to another account, whose stored account_id no
    # longer agrees with the account its origin_fingerprint was computed for.
    counts: dict[str, int] = {}
    max_occ: dict[str, int] = {}
    for dedup_key, fingerprint, count, row_max_occ in session.execute(
        select(
            func.coalesce(Transaction.origin_fingerprint, Transaction.fingerprint),
            Transaction.fingerprint,
            func.count(),
            func.max(Transaction.occurrence),
        )
        .where(Transaction.user_id == user_id)
        .group_by(
            func.coalesce(Transaction.origin_fingerprint, Transaction.fingerprint),
            Transaction.fingerprint,
        )
    ):
        counts[dedup_key] = counts.get(dedup_key, 0) + count
        max_occ[fingerprint] = max(max_occ.get(fingerprint, -1), row_max_occ)
    # Union of both key sets, not just `counts`: a row whose fingerprint was edited
    # contributes to max_occ under a key that carries no coalesced count, and dropping it
    # would hand the allocator a (0, -1) default for an ordinal that is already taken.
    existing_counts: dict[str, tuple[int, int]] = {
        key: (counts.get(key, 0), max_occ.get(key, -1)) for key in counts.keys() | max_occ.keys()
    }
    allocator = OccurrenceAllocator(existing_counts)
    imported = 0
    skipped_dupe = 0
    warnings: list[str] = []
    # transfer_group token → the rows inserted for it, for the second-pass re-link.
    inserted_by_group: dict[str, list[Transaction]] = {}
    # (txn, raw label names) for rows carrying F3a labels — linked after the flush
    # below assigns ids.
    pending_labels: list[tuple[Transaction, tuple[str, ...]]] = []

    for row in rows:
        account = accounts_by_name.get(row.account_name)
        if account is None:
            warnings.append(f"{TRANSACTIONS_CSV} row {row.line_no}: account not found — skipped")
            continue

        category_id: int | None = None
        if row.category_name is not None and row.category_kind is not None:
            category = categories_by_key.get((row.category_name, row.category_kind))
            category_id = category.id if category is not None else None

        # Re-normalize: this is the ONLY write path in the app whose merchant_normalized
        # arrives from outside (a hand-edited zip is backup_csv's declared threat model), and
        # every other producer routes through normalize_merchant. Skipping it let one
        # capitalised cell fingerprint differently from the native twin (so the row staged as
        # a duplicate instead of deduping) AND blocked auto-tagging forever, because both
        # prefetch maps key on the lowercase form. The stored column is normalized too, which
        # is what ADR-0006's recompute procedure reads (migration 0025 keys on the column, not
        # on merchant_raw — which is nullable here).
        merchant_normalized = normalize_merchant(row.merchant_normalized)
        fingerprint = transaction_fingerprint(
            txn_date=row.date,
            amount_paise=row.amount_paise,
            normalized_merchant=merchant_normalized,
            account_id=account.id,
        )
        # `is None` — occurrence 0 is a valid first sighting (see the allocator).
        occurrence = allocator.allocate(fingerprint)
        if occurrence is None:
            skipped_dupe += 1
            continue

        txn = Transaction(
            user_id=user_id,
            account_id=account.id,
            date=row.date,
            amount_paise=row.amount_paise,
            transaction_type=row.transaction_type,
            merchant_raw=row.merchant_raw,
            merchant_normalized=merchant_normalized,
            category_id=category_id,
            auto_category_id=None,  # the F3 acceptance-rate metric field is not restored
            fingerprint=fingerprint,
            # origin_fingerprint stays NULL — deliberately, and even when `row.source`
            # replays as "import" (ADR-0007 rule 9). A backup zip is a snapshot of our
            # own data, not an immutable external artifact, so the honest dedup key for
            # a restored row is its own current assertion, which is what COALESCE gives
            # a NULL origin. Stamping it here would freeze a hash the file never
            # promised and make a later edit un-matchable against a fresh statement.
            occurrence=occurrence,
            source=row.source,
            import_batch_id=batch_id,
            confirmed_at=row.confirmed_at,
            transfer_pair_id=None,  # linked in the second pass below
        )
        session.add(txn)
        imported += 1
        if row.labels:
            pending_labels.append((txn, row.labels))
        if row.transfer_group is not None:
            inserted_by_group.setdefault(row.transfer_group, []).append(txn)

    session.flush()  # assign transaction ids before the re-link + label links
    # F3a labels — now that ids exist, link each row's labels. Resolve the union
    # of every row's names in ONE get-or-create pass (keyed by normalized name),
    # then map each row's names back to ids. link_labels is insert-only: these
    # rows were just inserted, so there is no existing (txn, label) row to diff.
    # Backup restore does NOT learn — it replays the user's own export, which is a
    # reconstruction, not a fresh merchant→label decision.
    if pending_labels:
        all_names = {name for _, names in pending_labels for name in names}
        labels_by_name = {
            label.name: label
            for label in resolve_label_names(session, user_id=user_id, names=all_names)
        }
        for txn, names in pending_labels:
            label_ids = [
                labels_by_name[norm].id
                for name in names
                if (norm := normalize_label_name(name)) in labels_by_name
            ]
            link_labels(session, txn_id=txn.id, user_id=user_id, label_ids=label_ids)
    relinked = _relink_transfers(inserted_by_group)
    session.flush()
    return imported, skipped_dupe, relinked, warnings


def _relink_transfers(groups: dict[str, list[Transaction]]) -> int:
    """Second pass: point each transfer pair's two legs at each other by resolved id.

    A group with anything other than two inserted legs (a lone leg because its partner deduped,
    or malformed data) is left unlinked — ``transfer_pair_id`` stays NULL rather than forming a
    half-pair. Returns the number of legs re-linked.
    """
    relinked = 0
    for legs in groups.values():
        if len(legs) != 2:
            continue
        first, second = legs
        first.transfer_pair_id = second.id
        second.transfer_pair_id = first.id
        relinked += 2
    return relinked
