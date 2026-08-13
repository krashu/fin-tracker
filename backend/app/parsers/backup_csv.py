"""Canonical backup-zip parser — the read side of the spend backup (PRD §F10).

Pure: ``bytes`` → :class:`ParsedBackup` (accounts + categories + transactions) plus per-row
skip warnings. No DB, no logging — a backup zip is untrusted boundary input (a hand-edited or
corrupted file), so this does structural + vocabulary + money validation and the caller
(:mod:`app.services.backup_import_service`) does cross-entity resolution / dedup / persistence.

This module owns the backup schema (member filenames + column names); the exporter
(:mod:`app.services.export_service`) imports these constants so the two halves cannot drift.

Money discipline (the trap this parser exists to avoid): ``*_paise`` cells are the app's raw
integer paise and are parsed with strict :func:`int` — **no** ``Decimal``, **no** ×100 rupee
scaling (that scaling is right for broker CSVs in :mod:`app.parsers.investment_csv`, and wrong
here). A non-integer paise cell is a counted row rejection, never a silent round.

Warnings are PII-safe: ``"<file> row N: <reason>"`` — line number + cause only, never a raw
cell value.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from datetime import date as date_t
from typing import cast, get_args

from app.models.account import AccountTypeStr, CurrencyStr
from app.models.category import CategoryKindStr
from app.models.transaction import TransactionSourceStr, TransactionTypeStr
from app.parsers.base import ParserError

# --- Backup schema (single source of truth; the exporter imports these) --------------------
METADATA_JSON = "metadata.json"
ACCOUNTS_CSV = "accounts.csv"
CATEGORIES_CSV = "categories.csv"
TRANSACTIONS_CSV = "transactions.csv"

ACCOUNT_COLUMNS = (
    "name",
    "type",
    "issuer",
    "last4",
    "opening_balance_paise",
    "currency",
    "archived_at",
)
CATEGORY_COLUMNS = ("name", "kind", "color", "archived_at", "parent_name")
TRANSACTION_COLUMNS = (
    "date",
    "account_name",
    "amount_paise",
    "transaction_type",
    "merchant_raw",
    "merchant_normalized",
    "category_name",
    "category_kind",
    "labels",
    "source",
    "confirmed_at",
    "transfer_group",
)

# Columns each CSV must carry to be resolvable; others default to empty/None.
_ACCOUNT_REQUIRED = frozenset({"name", "type"})
_CATEGORY_REQUIRED = frozenset({"name", "kind"})
_TRANSACTION_REQUIRED = frozenset(
    {"date", "account_name", "amount_paise", "transaction_type", "merchant_normalized", "source"}
)

_ACCOUNT_TYPES = frozenset(get_args(AccountTypeStr))
_CATEGORY_KINDS = frozenset(get_args(CategoryKindStr))
_TXN_TYPES = frozenset(get_args(TransactionTypeStr))
_SOURCES = frozenset(get_args(TransactionSourceStr))
_LAST4_RE = re.compile(r"^\d{4}$")
_HEX_COLOR_RE = re.compile(r"^#[0-9a-f]{6}$")


@dataclass(frozen=True, slots=True)
class ParsedBackupAccount:
    line_no: int
    name: str
    type: AccountTypeStr
    issuer: str | None
    last4: str | None
    opening_balance_paise: int
    currency: CurrencyStr
    archived_at: datetime | None


@dataclass(frozen=True, slots=True)
class ParsedBackupCategory:
    line_no: int
    name: str
    kind: CategoryKindStr
    color: str | None
    archived_at: datetime | None
    parent_name: str | None = None


@dataclass(frozen=True, slots=True)
class ParsedBackupTransaction:
    line_no: int
    date: date_t
    account_name: str
    amount_paise: int
    transaction_type: TransactionTypeStr
    merchant_raw: str | None
    merchant_normalized: str
    category_name: str | None
    category_kind: CategoryKindStr | None
    # F3a labels (user tags) as raw names — the ``;``-joined cell, tokenized here;
    # normalization + get-or-create happens in the import service.
    labels: tuple[str, ...]
    source: TransactionSourceStr
    confirmed_at: datetime | None
    transfer_group: str | None


@dataclass(frozen=True, slots=True)
class ParsedBackup:
    accounts: list[ParsedBackupAccount]
    categories: list[ParsedBackupCategory]
    transactions: list[ParsedBackupTransaction]
    warnings: list[str]


class BackupParseError(ParserError):
    """Zip-level failure: not a zip, missing/duplicate/unexpected member, bad encoding, or a
    CSV missing a required column. Maps to HTTP 422 at the route."""


def parse_backup_zip(file_bytes: bytes) -> ParsedBackup:
    """Parse a backup zip into typed rows + per-row skip warnings.

    Raises:
        BackupParseError: not a zip, a duplicate/unexpected/missing member, a non-UTF-8 CSV,
            or a CSV missing a required column.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(file_bytes))
    except zipfile.BadZipFile as e:
        raise BackupParseError("file is not a valid zip archive") from e

    with archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise BackupParseError("backup archive has duplicate entries")
        present = set(names)
        required = {ACCOUNTS_CSV, CATEGORIES_CSV, TRANSACTIONS_CSV}
        missing = required - present
        if missing:
            raise BackupParseError(
                f"backup archive missing required member(s): {', '.join(sorted(missing))}"
            )
        unexpected = present - (required | {METADATA_JSON})
        if unexpected:
            raise BackupParseError("backup archive has unexpected entries")

        accounts, account_warnings = _parse_accounts(archive.read(ACCOUNTS_CSV))
        categories, category_warnings = _parse_categories(archive.read(CATEGORIES_CSV))
        transactions, transaction_warnings = _parse_transactions(archive.read(TRANSACTIONS_CSV))

    return ParsedBackup(
        accounts=accounts,
        categories=categories,
        transactions=transactions,
        warnings=[*account_warnings, *category_warnings, *transaction_warnings],
    )


def _get(row: dict[str, str | None], key: str) -> str | None:
    """Stripped cell value, or ``None`` for absent/blank."""
    value = row.get(key)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _split_labels(raw: str | None) -> tuple[str, ...]:
    """Tokenize a ``;``-joined labels cell into raw names (blanks dropped).

    Only splits/strips here — ``resolve_label_names`` in the import service does
    the real normalization (lowercase, ``#``/``;`` handling) + get-or-create.
    """
    if not raw:
        return ()
    return tuple(s for part in raw.split(";") if (s := part.strip()))


def _parse_dt(value: str | None) -> datetime | None:
    """Parse a backup datetime cell to **naive UTC** (ADR-0001 rule 5). ``None`` if unusable.

    The only datetime boundary in the app that accepts hand-authored input, and the only one
    where an offset can appear at all. An offset-bearing cell — ``2026-07-30T10:00:00+05:30``,
    the natural thing to type in India, and the export gives no hint that UTC is required —
    used to reach the ORM aware, where SQLite DROPS the offset (storing the wall clock, so
    5h30m wrong) while Postgres converts. Permanently, with no error.

    Convert THEN strip, and only when there is something to convert: a naive cell is already
    UTC (that is the shape ``export_service`` writes — ``isoformat()`` of a value SQLite hands
    back naive), so calling ``astimezone`` on it unconditionally would interpret it as the
    HOST's local time and shift every ordinary round-trip by the host offset — reintroducing
    the same bug in the opposite direction, on the common path rather than the rare one.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def _iter_rows(
    data: bytes, *, required: frozenset[str], what: str
) -> list[tuple[int, dict[str, str | None]]]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise BackupParseError(f"{what} is not valid UTF-8 text") from e
    reader = csv.DictReader(io.StringIO(text))
    headers = {(h or "").strip() for h in (reader.fieldnames or [])}
    missing = required - headers
    if missing:
        raise BackupParseError(f"{what} missing required column(s): {', '.join(sorted(missing))}")
    # ``line_no`` starts at 2 — row 1 is the header — so warnings point at the real CSV line.
    return list(enumerate(reader, start=2))


def _parse_accounts(data: bytes) -> tuple[list[ParsedBackupAccount], list[str]]:
    accounts: list[ParsedBackupAccount] = []
    warnings: list[str] = []
    for line_no, row in _iter_rows(data, required=_ACCOUNT_REQUIRED, what=ACCOUNTS_CSV):
        result = _parse_account_row(row, line_no)
        if isinstance(result, str):
            warnings.append(result)
        else:
            accounts.append(result)
    return accounts, warnings


def _parse_account_row(row: dict[str, str | None], line_no: int) -> ParsedBackupAccount | str:
    name = _get(row, "name")
    if not name:
        return f"{ACCOUNTS_CSV} row {line_no}: missing name"
    type_raw = (_get(row, "type") or "").lower()
    if type_raw not in _ACCOUNT_TYPES:
        return f"{ACCOUNTS_CSV} row {line_no}: unknown account type"
    currency = (_get(row, "currency") or "INR").upper()
    if currency != "INR":
        # v1 spending is INR-only (AccountCreate enforces the same at the JSON boundary).
        return f"{ACCOUNTS_CSV} row {line_no}: non-INR account not supported in v1"
    balance_raw = _get(row, "opening_balance_paise")
    try:
        opening_balance_paise = int(balance_raw) if balance_raw is not None else 0
    except ValueError:
        return f"{ACCOUNTS_CSV} row {line_no}: opening_balance_paise must be an integer"
    issuer = _get(row, "issuer")
    last4 = _get(row, "last4")
    if last4 is not None and not _LAST4_RE.match(last4):
        last4 = None  # cosmetic — drop a malformed value rather than reject the account
    return ParsedBackupAccount(
        line_no=line_no,
        name=name,
        type=cast("AccountTypeStr", type_raw),
        issuer=issuer.lower() if issuer else None,
        last4=last4,
        opening_balance_paise=opening_balance_paise,
        currency=cast("CurrencyStr", currency),
        archived_at=_parse_dt(_get(row, "archived_at")),
    )


def _parse_categories(data: bytes) -> tuple[list[ParsedBackupCategory], list[str]]:
    categories: list[ParsedBackupCategory] = []
    warnings: list[str] = []
    for line_no, row in _iter_rows(data, required=_CATEGORY_REQUIRED, what=CATEGORIES_CSV):
        result = _parse_category_row(row, line_no)
        if isinstance(result, str):
            warnings.append(result)
        else:
            categories.append(result)
    return categories, warnings


def _parse_category_row(row: dict[str, str | None], line_no: int) -> ParsedBackupCategory | str:
    name = _get(row, "name")
    if not name:
        return f"{CATEGORIES_CSV} row {line_no}: missing name"
    kind = (_get(row, "kind") or "").lower()
    if kind not in _CATEGORY_KINDS:
        return f"{CATEGORIES_CSV} row {line_no}: unknown category kind"
    color = _get(row, "color")
    if color is not None:
        color = color.lower()
        if not _HEX_COLOR_RE.match(color):
            color = None  # cosmetic — revert to derive-from-id rather than reject
    parent_name = _get(row, "parent_name")
    return ParsedBackupCategory(
        line_no=line_no,
        name=name,
        kind=cast("CategoryKindStr", kind),
        color=color,
        archived_at=_parse_dt(_get(row, "archived_at")),
        parent_name=parent_name,
    )


def _parse_transactions(data: bytes) -> tuple[list[ParsedBackupTransaction], list[str]]:
    transactions: list[ParsedBackupTransaction] = []
    warnings: list[str] = []
    for line_no, row in _iter_rows(data, required=_TRANSACTION_REQUIRED, what=TRANSACTIONS_CSV):
        result = _parse_transaction_row(row, line_no)
        if isinstance(result, str):
            warnings.append(result)
        else:
            transactions.append(result)
    return transactions, warnings


def _parse_transaction_row(
    row: dict[str, str | None], line_no: int
) -> ParsedBackupTransaction | str:
    date_raw = _get(row, "date")
    try:
        txn_date = date_t.fromisoformat(date_raw) if date_raw else None
    except ValueError:
        txn_date = None
    if txn_date is None:
        return f"{TRANSACTIONS_CSV} row {line_no}: invalid or missing date"

    account_name = _get(row, "account_name")
    if not account_name:
        return f"{TRANSACTIONS_CSV} row {line_no}: missing account_name"

    amount_raw = _get(row, "amount_paise")
    if amount_raw is None:
        return f"{TRANSACTIONS_CSV} row {line_no}: missing amount_paise"
    try:
        # Strict integer paise — a decimal string is a rejection, never a silent round/scale.
        amount_paise = int(amount_raw)
    except ValueError:
        return f"{TRANSACTIONS_CSV} row {line_no}: amount_paise must be an integer"

    txn_type = (_get(row, "transaction_type") or "").lower()
    # Legacy alias, read-only. `refund` was a fourth transaction_type until
    # ADR-0009 collapsed it into a positively-signed `spend`, and _TXN_TYPES
    # derives from that Literal — so every backup zip exported before the
    # collapse would fail the vocabulary check below on restore. The amount is
    # already positive in those files (the old `refund > 0` rule), so remapping
    # the type is the whole migration: the row lands as a refund by sign. Never
    # emitted on export (export_service dumps the column verbatim).
    if txn_type == "refund":
        txn_type = "spend"
    if txn_type not in _TXN_TYPES:
        return f"{TRANSACTIONS_CSV} row {line_no}: unknown transaction type"

    source = (_get(row, "source") or "").lower()
    if source not in _SOURCES:
        return f"{TRANSACTIONS_CSV} row {line_no}: unknown source"

    category_kind_raw = _get(row, "category_kind")
    category_kind = category_kind_raw.lower() if category_kind_raw else None
    if category_kind is not None and category_kind not in _CATEGORY_KINDS:
        category_kind = None  # unresolvable kind → import leaves the row uncategorized

    return ParsedBackupTransaction(
        line_no=line_no,
        date=txn_date,
        account_name=account_name,
        amount_paise=amount_paise,
        transaction_type=cast("TransactionTypeStr", txn_type),
        merchant_raw=_get(row, "merchant_raw"),
        # NOT NULL in the DB; "" is a legitimate (if unusual) normalized value, so keep it.
        merchant_normalized=_get(row, "merchant_normalized") or "",
        category_name=_get(row, "category_name"),
        category_kind=cast("CategoryKindStr | None", category_kind),
        labels=_split_labels(_get(row, "labels")),
        source=cast("TransactionSourceStr", source),
        confirmed_at=_parse_dt(_get(row, "confirmed_at")),
        transfer_group=_get(row, "transfer_group"),
    )
