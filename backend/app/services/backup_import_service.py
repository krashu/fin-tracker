"""Backup import — additive, deduped load of a spend backup zip (PRD §F10).

The load side of :mod:`app.services.export_service`. Deliberately **additive**, not a
wipe-restore: re-importing a backup that overlaps the current DB skips the duplicates and adds
only what's new, so it's non-destructive by construction and fits "I backed up, added more
rows, now I re-import the backup". Mirrors :mod:`app.services.investment_import_service`
structurally (parse → upsert refs → dedup → persist, route owns the commit), but the
money/identity details differ — see the two load-bearing rules below.

* **Fingerprints are recomputed here, never trusted from the file.** The dedup key bakes in
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
* **Provenance is carried as inputs, then recomputed** (ADR-0006 rule 17). A row's
  ``origin_*`` cells are the inputs of the statement line it came from; restore resolves the
  origin account by name and recomputes ``origin_fingerprint`` on the target, storing the inputs
  only when the origin differs from the row's own identity. The incoming dedup key is
  ``COALESCE(origin, fingerprint)`` — the key the prefetch counts stored rows under — so an
  edited imported row dedups against its statement line, and a restored one cannot re-stage on
  the next statement upload. The ordinal is allocated against the *current* fingerprint
  (``OccurrenceAllocator``'s ``slot``).
* **Accounts/categories are match-or-create, never mutate.** Active account names are unique
  per user, so a name match with a *different* type/currency is rejected (a counted warning),
  never rebound; ``opening_balance_paise`` / ``type`` / ``currency`` on a matched account are
  left untouched (mutating opening balance would shift every historical rollup — the reason
  ``AccountUpdate`` locks them).

Transfer pairs are re-linked in a second pass over a backup-local ``transfer_group`` token
(NULL-then-update, the shape the self-referential composite FK requires). Both legs must be
confirmed transfers this restore inserted, of equal and opposite amount on different accounts
(ADR-0002 rules 7-8; a pending leg is deleted by import cancel, so it is never paired); a lone leg
or an invalid group is kept unpaired with a warning and given back its pre-link type. Refund →
original links (ADR-0009 rule 13) are restored the same way over ``row_ref`` /
``refund_of_ref``, only between rows this restore inserted; one that cannot be restored is a
warning counted in ``links_not_restored``, not a dropped row.

Row-level dedup is the only idempotency backstop — there is **no** whole-zip hash short-circuit
(the exporter stamps a timestamp, so byte-identical re-uploads are not a reliable signal). The
zip's ``source_file_hash`` is still recorded on the batch for the audit trail.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass
from datetime import date
from typing import get_args
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.log_config import get_logger
from app.models import Account, Category, CategoryKindStr, ImportBatch, Transaction
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
from app.services.category_colors import active_root_colors, next_root_color
from app.services.fingerprint import transaction_fingerprint
from app.services.merchant import merchant_too_long, normalize_merchant
from app.services.occurrence import OccurrenceAllocator
from app.services.refund_link import refund_link_problem
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
    links_not_restored: int
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

    ``parsed.warnings`` (the parser's per-row skips) are merged into the returned warnings.
    ``rows_rejected`` is the total rows dropped across parse + account/transaction resolution —
    a user-facing total spanning accounts and transactions, deliberately NOT the
    ``rows_rejected`` in the ``import_completed`` event below, which counts dropped
    TRANSACTIONS only so its counts satisfy ``rows_in == imported + skipped + rejected``.
    ``links_not_restored`` counts the transfer-pair and refund links that could not be restored
    on rows that DID land; those warnings are not in ``rows_rejected``, and neither are
    category-flatten warnings (the category landed). ``warnings`` holds every message.
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
    imported, skipped, relinked, txn_warnings, link_warnings = _persist_transactions(
        session,
        user_id=user_id,
        batch_id=batch.id,
        rows=parsed.transactions,
        accounts_by_name=accounts_by_name,
        categories_by_key=categories_by_key,
    )

    warnings = [
        *parsed.warnings,
        *account_warnings,
        *category_warnings,
        *txn_warnings,
        *link_warnings,
    ]
    # A row dropped (parse, account, or resolution) vs a link not restored on a row that
    # landed (transfer pair, refund) are counted apart; a flattened category landed too, so it
    # is in neither.
    rows_rejected = len(parsed.warnings) + len(account_warnings) + len(txn_warnings)
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
        links_not_restored=len(link_warnings),
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
        rows_rejected=rows_rejected,
        links_not_restored=len(link_warnings),
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
    warnings rather than silently counted as a success. A restored root without a colour gets
    the least-used palette slot of its kind (ADR-0012 rule 7). Mirrors ``_upsert_accounts``' shape.
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
    used = {
        k: active_root_colors(session, user_id=user_id, kind=k) for k in get_args(CategoryKindStr)
    }

    def _root_color(row: ParsedBackupCategory) -> str:
        color = row.color or next_root_color(used[row.kind])
        if row.archived_at is None:
            used[row.kind].append(color)
        return color

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
            color=_root_color(row),
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
            color=row.color if parent_id is not None else _root_color(row),
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
) -> tuple[int, int, int, list[str], list[str]]:
    """Recompute fingerprint → dedup → insert → relink transfer pairs and refund links.

    Returns ``(imported, skipped_dupe, transfers_relinked, warnings, link_warnings)``;
    ``link_warnings`` are transfer-pair and refund links that could not be restored on rows
    that DID land.
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
    inserted_by_group: dict[str, list[tuple[Transaction, ParsedBackupTransaction]]] = {}
    # (txn, raw label names) for rows carrying F3a labels — linked after the flush
    # below assigns ids.
    pending_labels: list[tuple[Transaction, tuple[str, ...]]] = []
    # (txn, its source row) for every inserted row, for the refund-link pass.
    inserted: list[tuple[Transaction, ParsedBackupTransaction]] = []
    # Refund-link bookkeeping. `file_key_counts` tallies dedup keys over every row that reaches
    # the allocator, so a key the file repeats is known to be ambiguous. `ref_targets` maps a
    # backup-local row_ref to the Transaction this restore inserted for it, or to the dedup key
    # of the pre-existing row it deduped against; None marks a ref shared by two rows.
    # `deduped_refunds` are the deduped rows that carry a refund_of_ref.
    file_key_counts: Counter[str] = Counter()
    ref_targets: dict[str, Transaction | str | None] = {}
    deduped_refunds: list[tuple[str, ParsedBackupTransaction]] = []

    for row in rows:
        account = accounts_by_name.get(row.account_name)
        if account is None:
            warnings.append(f"{TRANSACTIONS_CSV} row {row.line_no}: account not found — skipped")
            continue

        # ADR-0006 rule 3b: a merchant past the String(512) columns is skipped, never
        # truncated (a truncated key is a different identity). The origin's merchant is a
        # stored column too. `merchant_raw` is nullable here: a blank cell is not too long.
        if (
            merchant_too_long(row.merchant_normalized)
            or (row.merchant_raw is not None and merchant_too_long(row.merchant_raw))
            or (row.origin is not None and merchant_too_long(row.origin.merchant_normalized))
        ):
            warnings.append(
                f"{TRANSACTIONS_CSV} row {row.line_no}: merchant longer than 512 characters"
                " — skipped"
            )
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
        # ADR-0006 rule 17: recompute the origin on THIS install from the file's origin inputs
        # (a hash is never carried: it bakes in the source install's account id).
        origin_fingerprint: str | None = None
        origin_inputs: tuple[date, int, str, int] | None = None
        if row.origin is not None:
            origin_account = accounts_by_name.get(row.origin.account_name)
            if origin_account is None:
                warnings.append(
                    f"{TRANSACTIONS_CSV} row {row.line_no}: origin account not found — skipped"
                )
                continue
            origin_merchant = normalize_merchant(row.origin.merchant_normalized)
            origin_fingerprint = transaction_fingerprint(
                txn_date=row.origin.date,
                amount_paise=row.origin.amount_paise,
                normalized_merchant=origin_merchant,
                account_id=origin_account.id,
            )
            if origin_fingerprint == fingerprint:
                origin_fingerprint = fingerprint  # unedited since import: no stored inputs
            else:
                origin_inputs = (
                    row.origin.date,
                    row.origin.amount_paise,
                    origin_merchant,
                    origin_account.id,
                )
        # The incoming dedup key is the same COALESCE the prefetch counts stored rows under;
        # the ordinal still has to clear MAX(occurrence) on the row's CURRENT fingerprint.
        dedup_key = origin_fingerprint or fingerprint
        file_key_counts[dedup_key] += 1
        # `is None` — occurrence 0 is a valid first sighting (see the allocator).
        occurrence = allocator.allocate(dedup_key, slot=fingerprint)
        if occurrence is None:
            skipped_dupe += 1
            if row.row_ref is not None:
                ref_targets[row.row_ref] = None if row.row_ref in ref_targets else dedup_key
            if row.refund_of_ref is not None:
                deduped_refunds.append((dedup_key, row))
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
            # Provenance recomputed above (ADR-0006 rule 17); NULL for rows with none.
            origin_fingerprint=origin_fingerprint,
            origin_date=origin_inputs[0] if origin_inputs else None,
            origin_amount_paise=origin_inputs[1] if origin_inputs else None,
            origin_merchant_normalized=origin_inputs[2] if origin_inputs else None,
            origin_account_id=origin_inputs[3] if origin_inputs else None,
            occurrence=occurrence,
            source=row.source,
            import_batch_id=batch_id,
            confirmed_at=row.confirmed_at,
            transfer_pair_id=None,  # linked in the second pass below
            # ADR-0002 rule 9 provenance. Non-null implies a pair leg, so a row with no
            # group (hand-edited) never carries one; _relink_transfers releases a leg that
            # ends up unpaired.
            pre_link_type=row.pre_link_type if row.transfer_group is not None else None,
        )
        session.add(txn)
        imported += 1
        inserted.append((txn, row))
        if row.row_ref is not None:
            ref_targets[row.row_ref] = None if row.row_ref in ref_targets else txn
        if row.labels:
            pending_labels.append((txn, row.labels))
        if row.transfer_group is not None:
            inserted_by_group.setdefault(row.transfer_group, []).append((txn, row))

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
    # Transfers first: the refund checks below read the final (post-release) types.
    relinked, transfer_warnings = _relink_transfers(inserted_by_group, categories_by_key)
    refund_warnings = _relink_refunds(
        session,
        user_id=user_id,
        inserted=inserted,
        ref_targets=ref_targets,
        deduped_refunds=deduped_refunds,
        existing_counts=counts,
        file_key_counts=file_key_counts,
    )
    session.flush()
    return imported, skipped_dupe, relinked, warnings, [*transfer_warnings, *refund_warnings]


def _relink_refunds(
    session: Session,
    *,
    user_id: UUID,
    inserted: list[tuple[Transaction, ParsedBackupTransaction]],
    ref_targets: dict[str, Transaction | str | None],
    deduped_refunds: list[tuple[str, ParsedBackupTransaction]],
    existing_counts: dict[str, int],
    file_key_counts: Counter[str],
) -> list[str]:
    """Restore each refund → original link (ADR-0009 rule 13) by backup-local ``row_ref``.

    The link is written only onto a refund row this restore inserted, under the same checks as
    ``PUT /transactions/{id}/refund-link`` (:func:`refund_link_problem`). The original may be a
    row this restore inserted, or the one pre-existing row it deduped against — resolved only
    when that match is unambiguous (:func:`_resolve_existing`). Restore never mutates a
    pre-existing row, so a refund that itself deduped is warned about only when it has no link
    yet; one that already carries its link needs nothing, which keeps an idempotent re-import
    silent. A ref shared by two rows or pointing nowhere is unresolvable. Each failure leaves
    ``refund_of_id`` as it was with a PII-safe warning (line number only), returned here.
    """
    inserted_ids = [txn.id for txn, _ in inserted]

    def resolve(target: Transaction | str | None) -> Transaction | None:
        if isinstance(target, str):
            return _resolve_existing(
                session,
                user_id=user_id,
                key=target,
                inserted_ids=inserted_ids,
                existing_counts=existing_counts,
                file_key_counts=file_key_counts,
            )
        return target

    warnings: list[str] = []
    for txn, row in inserted:
        if row.refund_of_ref is None:
            continue
        original = resolve(ref_targets.get(row.refund_of_ref))
        if original is not None and refund_link_problem(txn, original) is None:
            txn.refund_of_id = original.id
        else:
            warnings.append(f"{TRANSACTIONS_CSV} row {row.line_no}: refund link not restored")
    for key, row in deduped_refunds:
        existing = resolve(key)
        if existing is None or existing.refund_of_id is None:
            warnings.append(f"{TRANSACTIONS_CSV} row {row.line_no}: refund link not restored")
    return warnings


def _resolve_existing(
    session: Session,
    *,
    user_id: UUID,
    key: str,
    inserted_ids: list[int],
    existing_counts: dict[str, int],
    file_key_counts: Counter[str],
) -> Transaction | None:
    """The one pre-existing row a deduped backup row matched, or ``None`` when it is a guess.

    Unambiguous means one existing row under the coalesced dedup key AND one file row with that
    key; with more of either, which existing row a given file row maps to is arbitrary. Rows
    this restore inserted are excluded.
    """
    if existing_counts.get(key, 0) != 1 or file_key_counts[key] != 1:
        return None
    return session.scalars(
        select(Transaction).where(
            Transaction.user_id == user_id,
            func.coalesce(Transaction.origin_fingerprint, Transaction.fingerprint) == key,
            Transaction.id.not_in(inserted_ids),
        )
    ).one_or_none()


def _saved_category_id(
    row: ParsedBackupTransaction, categories_by_key: dict[tuple[str, str], Category]
) -> int | None:
    """The restoring user's category a row's pre-link category name and kind resolve to.

    The same name-and-kind lookup as ``category_name``, so it only ever finds this user's own
    categories; a name that does not resolve is dropped silently, like ``category_name``.
    """
    if row.pre_link_category_name is None or row.pre_link_category_kind is None:
        return None
    category = categories_by_key.get((row.pre_link_category_name, row.pre_link_category_kind))
    return category.id if category is not None else None


def _release_unpaired_leg(
    txn: Transaction,
    row: ParsedBackupTransaction,
    categories_by_key: dict[tuple[str, str], Category],
) -> None:
    """Give a leg left unpaired what its link took, as if its partner were deleted.

    The canonical rule is :func:`app.services.reconciliation_service.release_leg` (ADR-0002
    rule 9); this variant works on rows that are not flushed yet and on the pre-link category
    still held in the file. A link-born leg returns to its pre-link type; an F2-born one (no
    pre-link type) stays a ``transfer``. A saved category goes back onto the leg only when the
    leg has no category of its own (only a hand-edited zip carries both, and the row's own
    category wins).
    """
    if txn.pre_link_type is not None:
        if txn.transaction_type == "transfer":
            txn.transaction_type = txn.pre_link_type
        txn.pre_link_type = None
    if txn.category_id is None:
        txn.category_id = _saved_category_id(row, categories_by_key)


def _relink_transfers(
    groups: dict[str, list[tuple[Transaction, ParsedBackupTransaction]]],
    categories_by_key: dict[tuple[str, str], Category],
) -> tuple[int, list[str]]:
    """Second pass: pair each valid transfer group's two legs by resolved id (ADR-0002 rules 7-8).

    A group is paired only when exactly two legs were inserted by this restore, both are
    confirmed transfers (a pending row is deleted by import cancel, ADR-0002 rule 5), and the
    amounts are equal and opposite on different accounts. Restore never
    mutates a pre-existing row, so a lone leg (its partner deduped, was rejected, or is not in
    the file) and an invalid group are kept unpaired, released to their pre-link type, and
    reported — one warning per leg, line number only. A paired leg regains its saved pre-link
    category in the same pass as its pair (the row was inserted unpaired, because CHECK
    ``pre_link_category_while_linked`` needs a pair), only when it has no category of its own.
    Returns ``(legs paired, warnings)``.
    """
    relinked = 0
    warnings: list[str] = []
    for legs in groups.values():
        if len(legs) == 2:
            (first, first_row), (second, second_row) = legs
            if (
                first.transaction_type == "transfer"
                and second.transaction_type == "transfer"
                and first.confirmed_at is not None
                and second.confirmed_at is not None
                and first.amount_paise == -second.amount_paise
                and first.account_id != second.account_id
            ):
                for leg, other, leg_row in (
                    (first, second, first_row),
                    (second, first, second_row),
                ):
                    leg.transfer_pair_id = other.id
                    if leg.category_id is None:
                        leg.pre_link_category_id = _saved_category_id(leg_row, categories_by_key)
                relinked += 2
                continue
        message = (
            "transfer pair not restored — legs must be two confirmed transfers of equal and "
            "opposite amount on different accounts"
            if len(legs) > 1
            else "transfer partner not restored — left unpaired"
        )
        for txn, row in legs:
            _release_unpaired_leg(txn, row, categories_by_key)
            warnings.append(f"{TRANSACTIONS_CSV} row {row.line_no}: {message}")
    return relinked, warnings
