"""Hand-built backup zips for tests.

``make_backup_zip`` takes rows as dicts and fills every column the caller leaves out with
``""``, so a test names only the cells it cares about and a future column addition touches
no call site. ``edit_transactions`` rewrites ``transactions.csv`` of a real export.
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from collections.abc import Callable, Mapping, Sequence

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

Row = Mapping[str, object]


def _csv_bytes(columns: tuple[str, ...], rows: Sequence[Row]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=columns, restval="")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def make_backup_zip(
    *,
    accounts: Sequence[Row] = (),
    categories: Sequence[Row] = (),
    transactions: Sequence[Row] = (),
    format_version: object = BACKUP_FORMAT_VERSION,
    metadata: str | None = None,
) -> bytes:
    """A backup zip. ``metadata`` overrides ``metadata.json``'s raw text (e.g. malformed JSON)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(
            METADATA_JSON,
            metadata if metadata is not None else json.dumps({"format_version": format_version}),
        )
        zf.writestr(ACCOUNTS_CSV, _csv_bytes(ACCOUNT_COLUMNS, accounts))
        zf.writestr(CATEGORIES_CSV, _csv_bytes(CATEGORY_COLUMNS, categories))
        zf.writestr(TRANSACTIONS_CSV, _csv_bytes(TRANSACTION_COLUMNS, transactions))
    return buffer.getvalue()


def edit_transactions(
    zip_bytes: bytes, fn: Callable[[list[dict[str, str]]], list[dict[str, str]]]
) -> bytes:
    """Rewrite ``transactions.csv`` of ``zip_bytes`` through ``fn`` (rows in, rows out)."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zin, zipfile.ZipFile(out, "w") as zout:
        for name in zin.namelist():
            data = zin.read(name)
            if name == TRANSACTIONS_CSV:
                reader = csv.DictReader(io.StringIO(data.decode("utf-8")))
                rows = fn([dict(r) for r in reader])
                data = _csv_bytes(TRANSACTION_COLUMNS, rows)
            zout.writestr(name, data)
    return out.getvalue()


def account(name: str = "Axis CC", **cells: object) -> dict[str, object]:
    """An account row with sane defaults (an Axis credit card)."""
    return {
        "name": name,
        "type": "credit_card",
        "issuer": "axis",
        "last4": "1234",
        "opening_balance_paise": 0,
        "currency": "INR",
        **cells,
    }


def txn(**cells: object) -> dict[str, object]:
    """A confirmed imported spend of -50000 on Axis CC; override any cell."""
    return {
        "date": "2026-07-01",
        "account_name": "Axis CC",
        "amount_paise": -50000,
        "transaction_type": "spend",
        "merchant_raw": "SWIGGY",
        "merchant_normalized": "swiggy",
        "source": "import",
        "confirmed_at": "2026-07-01T10:00:00",
        **cells,
    }
