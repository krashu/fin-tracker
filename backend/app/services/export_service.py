"""Backup export — spend transactions + their accounts/categories as a CSV zip (PRD §F10).

Produces a **self-sufficient** zip: the user's confirmed spend transactions plus the
accounts and categories those rows reference (name-keyed, archived state preserved), so the
backup rebuilds on a fresh DB through the additive importer
(:mod:`app.services.backup_import_service`).

This is deliberately **not** the PRD's Excel-only 7-table export — it's the restore-faithful
spend subset the user actually backs up (investments round-trip separately via
``/imports/investments``). Two money/identity rules, shared with the importer:

* **Integer paise only** — ``amount_paise`` / ``opening_balance_paise`` are written as their
  raw ``int`` (never a float or a scaled decimal). The importer parses them back with
  ``int()``; there is no rupee↔paise scaling on this path.
* **No hash is exported.** The fingerprint formula bakes in ``account_id``
  (:func:`app.services.fingerprint.transaction_fingerprint`), which is install-specific, so a
  carried value would never match a natively-created row. ``merchant_normalized`` (the true
  identity input) is exported verbatim; the importer re-normalizes it (a hand-edited cell is
  its threat model) and recomputes the fingerprint from the resolved ``account_id``. Provenance
  (ADR-0006 rule 17) travels the same way: the ``origin_*`` columns carry the *inputs* of the
  row's origin identity — its frozen inputs once edited, its own current inputs when unedited,
  blank when it has none — and restore recomputes ``origin_fingerprint`` on the target.

``metadata.json`` carries ``format_version`` (:data:`BACKUP_FORMAT_VERSION`, owned by the
parser). The importer accepts only the current version and requires every column, so any
change to the column set bumps it.

Only ``confirmed_at IS NOT NULL`` rows are exported — pending/in-review rows would otherwise
be silently auto-confirmed on restore. ``transfer_pair_id`` is exported indirectly as a
backup-local ``transfer_group`` token (shared by both legs of a pair) so the importer can
re-link the pair without leaking install-specific ids; ``pre_link_type`` rides along
verbatim, and ``pre_link_category_*`` as a category name and kind (like ``category_*``), so
an unlink after restore still gives each leg back its own type and category (ADR-0002
rule 9). A refund's link to its original (ADR-0009 rule 13) travels the same way as the
pair: ``row_ref`` / ``refund_of_ref`` are backup-local tokens, meaningful only inside one
zip.
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import date, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core import clock
from app.models import Account, Category, Transaction

# The parser owns the backup schema (member names + column order); import them so the
# written CSVs cannot drift from what the importer reads.
from app.parsers.backup_csv import (
    ACCOUNT_COLUMNS,
    ACCOUNTS_CSV,
    BACKUP_FORMAT_VERSION,
    CATEGORIES_CSV,
    CATEGORY_COLUMNS,
    METADATA_JSON,
    TRANSACTION_COLUMNS,
    TRANSACTIONS_CSV,
)


def _cell(value: object) -> str:
    """Serialize one CSV cell. ``None`` → empty; datetimes/dates → ISO; else ``str``.

    Never emits a float — every money column reaching here is already an ``int``.
    """
    if value is None:
        return ""
    if isinstance(value, datetime | date):
        return value.isoformat()
    return str(value)


def _write_csv(header: tuple[str, ...], rows: list[tuple[object, ...]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(header)
    for row in rows:
        writer.writerow([_cell(v) for v in row])
    return buffer.getvalue().encode("utf-8")


def _origin_cells(
    t: Transaction, account_name_by_id: dict[int, str]
) -> tuple[object, object, object, object]:
    """``origin_date, origin_amount_paise, origin_merchant_normalized, origin_account_name``.

    Blank when the row has no provenance. An edited row exports its frozen inputs; an unedited
    row with provenance (origin == identity) exports its own current inputs.
    """
    if t.origin_fingerprint is None:
        return None, None, None, None
    if t.origin_date is not None:
        return (
            t.origin_date,
            t.origin_amount_paise,
            t.origin_merchant_normalized,
            account_name_by_id.get(t.origin_account_id) if t.origin_account_id else None,
        )
    return t.date, t.amount_paise, t.merchant_normalized, account_name_by_id.get(t.account_id)


def build_backup_zip(session: Session, *, user_id: UUID) -> bytes:
    """Build the backup zip for ``user_id`` and return its bytes.

    Selects confirmed transactions, then the accounts/categories they reference (with their
    ``archived_at`` state), and packs three CSVs + ``metadata.json``. Deterministic row order
    (by id) keeps the CSV payloads stable for snapshot assertions; the zip container's entry
    timestamps are not relied on (the importer has no byte-hash short-circuit).
    """
    transactions = list(
        session.scalars(
            select(Transaction)
            .options(selectinload(Transaction.labels))
            .where(
                Transaction.user_id == user_id,
                Transaction.confirmed_at.is_not(None),
            )
            .order_by(Transaction.id)
        )
    )

    # An account referenced only as a row's origin (the row was moved off it) is still needed
    # by name in accounts.csv, or restore could not resolve the origin.
    account_ids = {t.account_id for t in transactions} | {
        t.origin_account_id for t in transactions if t.origin_account_id is not None
    }
    # A saved pre-link category is exported like a live one, so it (and its parent) lands in
    # categories.csv even when no row currently sits on it.
    category_ids = {t.category_id for t in transactions if t.category_id is not None} | {
        t.pre_link_category_id for t in transactions if t.pre_link_category_id is not None
    }

    # Restate ``user_id`` on both re-fetches even though these ids come from the user's own
    # transactions above — every other query in this module scopes it, and if a bad FK ever
    # landed on a transaction row an unscoped lookup would silently pack another user's account
    # name / last4 into the zip. A dangling id then simply drops (blank cell), which is the safe
    # degradation, not a leak.
    accounts = (
        list(
            session.scalars(
                select(Account)
                .where(Account.id.in_(account_ids), Account.user_id == user_id)
                .order_by(Account.id)
            )
        )
        if account_ids
        else []
    )
    categories = (
        list(
            session.scalars(
                select(Category)
                .where(Category.id.in_(category_ids), Category.user_id == user_id)
                .order_by(Category.id)
            )
        )
        if category_ids
        else []
    )
    # If any referenced category is a subcategory, also fetch its parent category so
    # parent_name can be resolved and the parent category exists on backup restore.
    parent_ids = {c.parent_id for c in categories if c.parent_id is not None} - category_ids
    if parent_ids:
        parent_cats = list(
            session.scalars(
                select(Category)
                .where(Category.id.in_(parent_ids), Category.user_id == user_id)
                .order_by(Category.id)
            )
        )
        categories.extend(parent_cats)
        categories.sort(key=lambda c: c.id)

    account_name_by_id = {a.id: a.name for a in accounts}
    category_by_id = {c.id: c for c in categories}
    category_name_by_id = {c.id: c.name for c in categories}

    account_rows: list[tuple[object, ...]] = [
        (
            a.name,
            a.type,
            a.issuer,
            a.last4,
            a.opening_balance_paise,
            a.currency,
            a.archived_at,
        )
        for a in accounts
    ]
    category_rows: list[tuple[object, ...]] = [
        (
            c.name,
            c.kind,
            c.color,
            c.archived_at,
            category_name_by_id.get(c.parent_id) if c.parent_id is not None else None,
        )
        for c in categories
    ]
    transaction_rows: list[tuple[object, ...]] = []
    for t in transactions:
        category = category_by_id.get(t.category_id) if t.category_id is not None else None
        pre_link_category = (
            category_by_id.get(t.pre_link_category_id)
            if t.pre_link_category_id is not None
            else None
        )
        # Shared token for a transfer pair: both legs resolve to the same min(id) so the
        # importer can regroup and re-link them without exporting install-specific ids.
        transfer_group = (
            str(min(t.id, t.transfer_pair_id)) if t.transfer_pair_id is not None else None
        )
        transaction_rows.append(
            (
                t.date,
                account_name_by_id.get(t.account_id),
                t.amount_paise,
                t.transaction_type,
                t.merchant_raw,
                t.merchant_normalized,
                category.name if category is not None else None,
                category.kind if category is not None else None,
                # F3a labels — the row's tags as a ``;``-joined name list (labels
                # are ordered by name via the relationship). Empty → None (blank cell).
                ";".join(label.name for label in t.labels) or None,
                t.source,
                t.confirmed_at,
                transfer_group,
                t.pre_link_type,
                pre_link_category.name if pre_link_category is not None else None,
                pre_link_category.kind if pre_link_category is not None else None,
                str(t.id),
                str(t.refund_of_id) if t.refund_of_id is not None else None,
                *_origin_cells(t, account_name_by_id),
            )
        )

    metadata = {
        "format_version": BACKUP_FORMAT_VERSION,
        "created_at": clock.utcnow().isoformat(),
        "row_counts": {
            "accounts": len(account_rows),
            "categories": len(category_rows),
            "transactions": len(transaction_rows),
        },
    }

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(METADATA_JSON, json.dumps(metadata, indent=2))
        zf.writestr(ACCOUNTS_CSV, _write_csv(ACCOUNT_COLUMNS, account_rows))
        zf.writestr(CATEGORIES_CSV, _write_csv(CATEGORY_COLUMNS, category_rows))
        zf.writestr(TRANSACTIONS_CSV, _write_csv(TRANSACTION_COLUMNS, transaction_rows))
    return buffer.getvalue()
