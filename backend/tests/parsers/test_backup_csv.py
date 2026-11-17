"""Parser tests for the backup zip reader (PRD §F10).

Pure — no DB. Covers zip-membership validation, the strict-integer paise rule (the trap the
importer must not repeat from the rupee-scaling investment CSV), the INR-only account guard,
and per-row skip warnings that keep the rest of a file importable.
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections.abc import Sequence
from datetime import date

import pytest

from app.parsers.backup_csv import (
    ACCOUNT_COLUMNS,
    ACCOUNTS_CSV,
    CATEGORIES_CSV,
    CATEGORY_COLUMNS,
    METADATA_JSON,
    TRANSACTION_COLUMNS,
    TRANSACTIONS_CSV,
    BackupFormatVersionError,
    BackupParseError,
    ParsedOrigin,
    parse_backup_zip,
)
from tests.backup_zip import Row, account, make_backup_zip, txn

_ACCOUNT_OK = account(opening_balance_paise=-50000)
_CATEGORY_OK = {"name": "Food", "kind": "spend", "color": "#4f46e5"}
_TXN_OK = txn(category_name="Food", category_kind="spend", labels="lunch")


def _valid_zip(
    *,
    accounts: Sequence[Row] = (_ACCOUNT_OK,),
    categories: Sequence[Row] = (_CATEGORY_OK,),
    transactions: Sequence[Row] = (_TXN_OK,),
) -> bytes:
    return make_backup_zip(accounts=accounts, categories=categories, transactions=transactions)


def _zip_without(*dropped: str) -> bytes:
    """A valid zip minus the named members."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(_valid_zip())) as zin, zipfile.ZipFile(buffer, "w") as zout:
        for name in zin.namelist():
            if name not in dropped:
                zout.writestr(name, zin.read(name))
    return buffer.getvalue()


def test_parses_a_valid_backup() -> None:
    parsed = parse_backup_zip(_valid_zip())
    assert len(parsed.accounts) == 1
    assert len(parsed.categories) == 1
    assert len(parsed.transactions) == 1
    assert not parsed.warnings

    account = parsed.accounts[0]
    assert account.name == "Axis CC"
    assert account.type == "credit_card"
    assert account.opening_balance_paise == -50000

    txn = parsed.transactions[0]
    assert txn.amount_paise == -50000
    assert txn.merchant_normalized == "swiggy"
    assert txn.category_name == "Food"
    assert txn.category_kind == "spend"


def test_bad_zip_bytes_raise() -> None:
    with pytest.raises(BackupParseError):
        parse_backup_zip(b"this is not a zip archive")


def test_missing_required_member_raises() -> None:
    with pytest.raises(BackupParseError, match="missing required member"):
        parse_backup_zip(_zip_without(TRANSACTIONS_CSV))


def test_missing_metadata_member_raises() -> None:
    with pytest.raises(BackupParseError, match="missing required member"):
        parse_backup_zip(_zip_without(METADATA_JSON))


def test_unexpected_member_raises() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(_valid_zip())) as zin, zipfile.ZipFile(buffer, "w") as zout:
        for name in zin.namelist():
            zout.writestr(name, zin.read(name))
        zout.writestr("surprise.csv", "x\n1\n")
    with pytest.raises(BackupParseError, match="unexpected"):
        parse_backup_zip(buffer.getvalue())


# The duplicate zip entry we build here is deliberate; zipfile warns when writing it.
@pytest.mark.filterwarnings("ignore:Duplicate name")
def test_duplicate_member_raises() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(_valid_zip())) as zin, zipfile.ZipFile(buffer, "w") as zout:
        for name in zin.namelist():
            zout.writestr(name, zin.read(name))
        zout.writestr(ACCOUNTS_CSV, zin.read(ACCOUNTS_CSV))  # duplicate entry
    with pytest.raises(BackupParseError, match="duplicate"):
        parse_backup_zip(buffer.getvalue())


@pytest.mark.parametrize(
    "metadata",
    [
        pytest.param('{"format_version": 1}', id="version-1"),
        pytest.param('{"format_version": 2}', id="version-2"),
        pytest.param('{"format_version": 4}', id="version-4"),
        pytest.param('{"format_version": "2"}', id="string-version"),
        pytest.param('{"format_version": true}', id="bool-version"),
        pytest.param("{}", id="missing-key"),
        pytest.param("[]", id="not-an-object"),
        pytest.param("{not json", id="malformed-json"),
    ],
)
def test_another_format_version_is_refused(metadata: str) -> None:
    with pytest.raises(BackupFormatVersionError, match="unsupported backup format version"):
        parse_backup_zip(
            make_backup_zip(
                accounts=[_ACCOUNT_OK],
                categories=[_CATEGORY_OK],
                transactions=[_TXN_OK],
                metadata=metadata,
            )
        )


def _csv_without_column(zip_bytes: bytes, member: str, column: str) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zin, zipfile.ZipFile(out, "w") as zout:
        for name in zin.namelist():
            data = zin.read(name)
            if name == member:
                reader = csv.DictReader(io.StringIO(data.decode()))
                fields = [f for f in (reader.fieldnames or []) if f != column]
                buf = io.StringIO(newline="")
                writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(reader)
                data = buf.getvalue().encode()
            zout.writestr(name, data)
    return out.getvalue()


@pytest.mark.parametrize(
    ("member", "column"),
    [
        *((ACCOUNTS_CSV, c) for c in ACCOUNT_COLUMNS),
        *((CATEGORIES_CSV, c) for c in CATEGORY_COLUMNS),
        *((TRANSACTIONS_CSV, c) for c in TRANSACTION_COLUMNS),
    ],
)
def test_every_column_is_required(member: str, column: str) -> None:
    with pytest.raises(BackupParseError, match="required column"):
        parse_backup_zip(_csv_without_column(_valid_zip(), member, column))


def test_integer_paise_round_trips() -> None:
    parsed = parse_backup_zip(_valid_zip(transactions=[txn(amount_paise=123456)]))
    assert parsed.transactions[0].amount_paise == 123456  # not 12345600 — no rupee scaling


def test_decimal_paise_is_rejected_not_scaled() -> None:
    parsed = parse_backup_zip(_valid_zip(transactions=[txn(amount_paise="123.45")]))
    assert parsed.transactions == []
    assert any("amount_paise must be an integer" in w for w in parsed.warnings)


def test_non_inr_account_is_rejected() -> None:
    parsed = parse_backup_zip(_valid_zip(accounts=[account("US Card", currency="USD")]))
    assert parsed.accounts == []
    assert any("non-INR" in w for w in parsed.warnings)


def test_malformed_row_is_skipped_others_survive() -> None:
    transactions = [
        txn(date="not-a-date", amount_paise=-100),  # bad date → skipped
        txn(date="2026-07-02", amount_paise=-200),  # good
    ]
    parsed = parse_backup_zip(_valid_zip(transactions=transactions))
    assert len(parsed.transactions) == 1
    assert parsed.transactions[0].amount_paise == -200
    assert any("invalid or missing date" in w for w in parsed.warnings)


def test_empty_backup_is_valid() -> None:
    parsed = parse_backup_zip(_valid_zip(accounts=[], categories=[], transactions=[]))
    assert parsed.accounts == []
    assert parsed.categories == []
    assert parsed.transactions == []
    assert not parsed.warnings


@pytest.mark.parametrize(
    ("account_row", "reason"),
    [
        (account(name=""), "missing name"),
        (account("X", type="not_a_type"), "unknown account type"),
        (account("X", opening_balance_paise="abc"), "opening_balance_paise must be an integer"),
    ],
)
def test_account_row_rejections(account_row: dict[str, object], reason: str) -> None:
    parsed = parse_backup_zip(_valid_zip(accounts=[account_row]))
    assert parsed.accounts == []
    assert any(reason in w for w in parsed.warnings)


def test_account_bad_last4_is_coerced_not_rejected() -> None:
    # A malformed last4 is cosmetic — drop it, keep the account (else its txns cascade-fail).
    parsed = parse_backup_zip(_valid_zip(accounts=[account("X", last4="99")]))
    assert len(parsed.accounts) == 1
    assert parsed.accounts[0].last4 is None


@pytest.mark.parametrize(
    ("category_row", "reason"),
    [
        ({"name": "", "kind": "spend"}, "missing name"),
        ({"name": "Food", "kind": "not_a_kind"}, "unknown category kind"),
    ],
)
def test_category_row_rejections(category_row: dict[str, object], reason: str) -> None:
    parsed = parse_backup_zip(_valid_zip(categories=[category_row]))
    assert parsed.categories == []
    assert any(reason in w for w in parsed.warnings)


def test_category_bad_color_is_coerced_not_rejected() -> None:
    parsed = parse_backup_zip(
        _valid_zip(categories=[{"name": "Food", "kind": "spend", "color": "notacolor"}])
    )
    assert len(parsed.categories) == 1
    assert parsed.categories[0].color is None


@pytest.mark.parametrize(
    ("cells", "reason"),
    [
        ({"account_name": ""}, "missing account_name"),
        ({"amount_paise": ""}, "missing amount_paise"),
        ({"transaction_type": "not_a_type"}, "unknown transaction type"),
        # `refund` was a fourth type before ADR-0009; the format is versioned, so it is
        # unknown like any other (ADR-0009 rule 10, retired).
        ({"transaction_type": "refund", "amount_paise": 100}, "unknown transaction type"),
        ({"source": "not_a_source"}, "unknown source"),
        ({"amount_paise": 0}, "amount_paise must be non-zero"),
    ],
)
def test_transaction_row_rejections(cells: dict[str, object], reason: str) -> None:
    parsed = parse_backup_zip(_valid_zip(transactions=[txn(**cells)]))
    assert parsed.transactions == []
    assert any(reason in w for w in parsed.warnings)


def test_transaction_invalid_category_kind_leaves_it_uncategorized() -> None:
    # A present category_name with a garbage kind → the row still imports (uncategorized), not
    # rejected — the transaction is the data worth keeping.
    parsed = parse_backup_zip(
        _valid_zip(transactions=[txn(category_name="Food", category_kind="not_a_kind")])
    )
    assert len(parsed.transactions) == 1
    assert parsed.transactions[0].category_kind is None


def test_category_with_parent_name_is_parsed() -> None:
    categories = [
        {"name": "Food & Dining", "kind": "spend", "color": "#4f46e5"},
        {"name": "Groceries", "kind": "spend", "color": "#4f46e5", "parent_name": "Food & Dining"},
    ]
    parsed = parse_backup_zip(_valid_zip(categories=categories))
    assert len(parsed.categories) == 2
    assert not parsed.warnings
    assert parsed.categories[0].name == "Food & Dining"
    assert parsed.categories[0].parent_name is None
    assert parsed.categories[1].name == "Groceries"
    assert parsed.categories[1].parent_name == "Food & Dining"


def test_pre_link_type_is_parsed() -> None:
    """ADR-0002 rule 9: backup carries a linked row's pre-link type."""
    parsed = parse_backup_zip(
        _valid_zip(
            transactions=[
                txn(transaction_type="transfer", transfer_group="7", pre_link_type="income"),
                txn(date="2026-07-02"),
            ]
        )
    )
    assert parsed.warnings == []
    assert [t.pre_link_type for t in parsed.transactions] == ["income", None]


def test_pre_link_category_is_parsed() -> None:
    """ADR-0002 rule 9: backup carries a linked row's saved category by name and kind; an
    unknown kind is dropped, like ``category_kind``."""
    parsed = parse_backup_zip(
        _valid_zip(
            transactions=[
                txn(
                    transaction_type="transfer",
                    transfer_group="7",
                    pre_link_category_name="Food",
                    pre_link_category_kind="SPEND",
                ),
                txn(date="2026-07-02", pre_link_category_name="Food", pre_link_category_kind="x"),
                txn(date="2026-07-03"),
            ]
        )
    )
    assert parsed.warnings == []
    assert [(t.pre_link_category_name, t.pre_link_category_kind) for t in parsed.transactions] == [
        ("Food", "spend"),
        ("Food", None),
        (None, None),
    ]


def test_refund_link_refs_are_parsed() -> None:
    """ADR-0009 rule 13: ``row_ref`` / ``refund_of_ref`` carry the refund → original link
    as backup-local tokens."""
    transactions = [
        txn(row_ref="11"),
        txn(date="2026-07-02", amount_paise=20000, row_ref="12", refund_of_ref="11"),
        txn(date="2026-07-03"),
    ]
    parsed = parse_backup_zip(_valid_zip(transactions=transactions))
    assert parsed.warnings == []
    assert [(t.row_ref, t.refund_of_ref) for t in parsed.transactions] == [
        ("11", None),
        ("12", "11"),
        (None, None),
    ]


def test_unknown_pre_link_type_rejects_the_row() -> None:
    parsed = parse_backup_zip(_valid_zip(transactions=[txn(pre_link_type="refund")]))
    assert parsed.transactions == []
    assert parsed.warnings == [f"{TRANSACTIONS_CSV} row 2: unknown pre-link type"]


def test_origin_columns_are_parsed() -> None:
    """ADR-0006 rule 17: the origin travels as inputs (account by name), keyed on origin_date;
    a blank merchant is ``""``, and all-blank cells mean no origin."""
    origin = {
        "origin_date": "2026-07-01",
        "origin_amount_paise": -20000,
        "origin_merchant_normalized": "",
        "origin_account_name": "Axis CC",
    }
    parsed = parse_backup_zip(_valid_zip(transactions=[txn(**origin), txn(date="2026-07-02")]))

    assert parsed.warnings == []
    with_origin, without = parsed.transactions
    assert with_origin.origin == ParsedOrigin(
        date=date(2026, 7, 1), amount_paise=-20000, merchant_normalized="", account_name="Axis CC"
    )
    assert without.origin is None


@pytest.mark.parametrize(
    "cells",
    [
        pytest.param({"origin_amount_paise": -1}, id="cells-without-a-date"),
        pytest.param({"origin_date": "2026-07-01"}, id="date-only"),
        pytest.param(
            {"origin_date": "nope", "origin_amount_paise": -1, "origin_account_name": "Axis CC"},
            id="bad-date",
        ),
        pytest.param(
            {
                "origin_date": "2026-07-01",
                "origin_amount_paise": "1.5",
                "origin_account_name": "Axis CC",
            },
            id="decimal-amount",
        ),
        pytest.param(
            {
                "origin_date": "2026-07-01",
                "origin_amount_paise": 0,
                "origin_account_name": "Axis CC",
            },
            id="zero-amount",
        ),
        pytest.param({"origin_date": "2026-07-01", "origin_amount_paise": -1}, id="no-account"),
    ],
)
def test_incomplete_origin_rejects_the_row(cells: dict[str, object]) -> None:
    parsed = parse_backup_zip(_valid_zip(transactions=[txn(**cells)]))

    assert parsed.transactions == []
    assert parsed.warnings == [f"{TRANSACTIONS_CSV} row 2: incomplete origin columns"]
