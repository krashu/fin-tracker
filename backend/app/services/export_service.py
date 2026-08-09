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
* **The dedup fingerprint is NOT exported.** The formula bakes in ``account_id``
  (:func:`app.services.fingerprint.transaction_fingerprint`), which is install-specific, so a
  carried value would never match a natively-created row. ``merchant_normalized`` (the true
  identity input) is exported verbatim; the importer re-normalizes it (a hand-edited cell is
  its threat model) and recomputes the fingerprint from the resolved ``account_id``.

Only ``confirmed_at IS NOT NULL`` rows are exported — pending/in-review rows would otherwise
be silently auto-confirmed on restore. ``transfer_pair_id`` is exported indirectly as a
backup-local ``transfer_group`` token (shared by both legs of a pair) so the importer can
re-link the pair without leaking install-specific ids.
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
    CATEGORIES_CSV,
    CATEGORY_COLUMNS,
    METADATA_JSON,
    TRANSACTION_COLUMNS,
    TRANSACTIONS_CSV,
)

# Backup-format version stamped into metadata.json. Bump only on a
# breaking change to the CSV column set so a future importer can branch.
BACKUP_FORMAT_VERSION = 1


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

    account_ids = {t.account_id for t in transactions}
    category_ids = {t.category_id for t in transactions if t.category_id is not None}

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

    account_name_by_id = {a.id: a.name for a in accounts}
    category_by_id = {c.id: c for c in categories}

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
        (c.name, c.kind, c.color, c.archived_at) for c in categories
    ]
    transaction_rows: list[tuple[object, ...]] = []
    for t in transactions:
        category = category_by_id.get(t.category_id) if t.category_id is not None else None
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
