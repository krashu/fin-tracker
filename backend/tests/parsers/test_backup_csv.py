"""Parser tests for the backup zip reader (PRD §F10).

Pure — no DB. Covers zip-membership validation, the strict-integer paise rule (the trap the
importer must not repeat from the rupee-scaling investment CSV), the INR-only account guard,
and per-row skip warnings that keep the rest of a file importable.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from app.parsers.backup_csv import (
    ACCOUNTS_CSV,
    CATEGORIES_CSV,
    METADATA_JSON,
    TRANSACTIONS_CSV,
    BackupParseError,
    parse_backup_zip,
)

_ACCOUNTS_HEADER = "name,type,issuer,last4,opening_balance_paise,currency,archived_at"
_CATEGORIES_HEADER = "name,kind,color,archived_at"
_TRANSACTIONS_HEADER = (
    "date,account_name,amount_paise,transaction_type,merchant_raw,"
    "merchant_normalized,category_name,category_kind,labels,source,confirmed_at,transfer_group"
)

_ACCOUNTS_OK = f"{_ACCOUNTS_HEADER}\nAxis CC,credit_card,axis,1234,-50000,INR,\n"
_CATEGORIES_OK = f"{_CATEGORIES_HEADER}\nFood,spend,#4f46e5,\n"
_TXN_OK = (
    f"{_TRANSACTIONS_HEADER}\n"
    "2026-07-01,Axis CC,-50000,spend,SWIGGY,swiggy,Food,spend,lunch,import,2026-07-01T10:00:00,\n"
)


def _zip(**members: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, content in members.items():
            zf.writestr(_MEMBER_NAMES[name], content)
    return buffer.getvalue()


_MEMBER_NAMES = {
    "accounts": ACCOUNTS_CSV,
    "categories": CATEGORIES_CSV,
    "transactions": TRANSACTIONS_CSV,
    "metadata": METADATA_JSON,
}


def _valid_zip(
    *, accounts: str = _ACCOUNTS_OK, categories: str = _CATEGORIES_OK, transactions: str = _TXN_OK
) -> bytes:
    return _zip(accounts=accounts, categories=categories, transactions=transactions, metadata="{}")


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
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(ACCOUNTS_CSV, _ACCOUNTS_OK)
        zf.writestr(CATEGORIES_CSV, _CATEGORIES_OK)
        # transactions.csv omitted
    with pytest.raises(BackupParseError, match="missing required member"):
        parse_backup_zip(buffer.getvalue())


def test_unexpected_member_raises() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(ACCOUNTS_CSV, _ACCOUNTS_OK)
        zf.writestr(CATEGORIES_CSV, _CATEGORIES_OK)
        zf.writestr(TRANSACTIONS_CSV, _TXN_OK)
        zf.writestr("surprise.csv", "x\n1\n")
    with pytest.raises(BackupParseError, match="unexpected"):
        parse_backup_zip(buffer.getvalue())


# The duplicate zip entry we build here is deliberate; zipfile warns when writing it.
@pytest.mark.filterwarnings("ignore:Duplicate name")
def test_duplicate_member_raises() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(ACCOUNTS_CSV, _ACCOUNTS_OK)
        zf.writestr(ACCOUNTS_CSV, _ACCOUNTS_OK)  # duplicate entry
        zf.writestr(CATEGORIES_CSV, _CATEGORIES_OK)
        zf.writestr(TRANSACTIONS_CSV, _TXN_OK)
    with pytest.raises(BackupParseError, match="duplicate"):
        parse_backup_zip(buffer.getvalue())


def test_missing_required_column_raises() -> None:
    accounts = "name,issuer\nAxis CC,axis\n"  # no 'type' column
    with pytest.raises(BackupParseError, match="required column"):
        parse_backup_zip(_valid_zip(accounts=accounts))


def test_integer_paise_round_trips() -> None:
    transactions = (
        f"{_TRANSACTIONS_HEADER}\n"
        "2026-07-01,Axis CC,123456,spend,SHOP,shop,,,,import,2026-07-01T10:00:00,\n"
    )
    parsed = parse_backup_zip(_valid_zip(transactions=transactions))
    assert parsed.transactions[0].amount_paise == 123456  # not 12345600 — no rupee scaling


def test_legacy_refund_type_is_aliased_to_spend() -> None:
    """T4: a backup zip exported before ADR-0009 carries ``transaction_type=refund``
    — a value ``_TXN_TYPES`` no longer contains, since it derives from the narrowed
    ``TransactionTypeStr``. The read-only legacy alias maps it to ``spend`` on
    read, so an old export still restores cleanly instead of every row 422ing on
    "unknown transaction type". The amount is untouched (already positive under
    the old ``refund >= 0`` rule), so the row lands as a refund BY SIGN, matching
    what a fresh export of the same data would produce today.
    """
    transactions = (
        f"{_TRANSACTIONS_HEADER}\n"
        "2026-07-01,Axis CC,50000,refund,SWIGGY,swiggy,Food,spend,,import,2026-07-01T10:00:00,\n"
    )
    parsed = parse_backup_zip(_valid_zip(transactions=transactions))
    assert not parsed.warnings
    assert len(parsed.transactions) == 1
    txn = parsed.transactions[0]
    assert txn.transaction_type == "spend"
    assert txn.amount_paise == 50000  # unchanged — the alias never re-signs.


def test_decimal_paise_is_rejected_not_scaled() -> None:
    transactions = (
        f"{_TRANSACTIONS_HEADER}\n"
        "2026-07-01,Axis CC,123.45,spend,SHOP,shop,,,,import,2026-07-01T10:00:00,\n"
    )
    parsed = parse_backup_zip(_valid_zip(transactions=transactions))
    assert parsed.transactions == []
    assert any("amount_paise must be an integer" in w for w in parsed.warnings)


def test_non_inr_account_is_rejected() -> None:
    accounts = f"{_ACCOUNTS_HEADER}\nUS Card,credit_card,,,,USD,\n"
    parsed = parse_backup_zip(_valid_zip(accounts=accounts))
    assert parsed.accounts == []
    assert any("non-INR" in w for w in parsed.warnings)


def test_malformed_row_is_skipped_others_survive() -> None:
    transactions = (
        f"{_TRANSACTIONS_HEADER}\n"
        "not-a-date,Axis CC,-100,spend,A,a,,,,import,,\n"  # bad date → skipped
        "2026-07-02,Axis CC,-200,spend,B,b,,,,import,2026-07-02T10:00:00,\n"  # good
    )
    parsed = parse_backup_zip(_valid_zip(transactions=transactions))
    assert len(parsed.transactions) == 1
    assert parsed.transactions[0].amount_paise == -200
    assert any("invalid or missing date" in w for w in parsed.warnings)


def test_empty_backup_is_valid() -> None:
    parsed = parse_backup_zip(
        _valid_zip(
            accounts=f"{_ACCOUNTS_HEADER}\n",
            categories=f"{_CATEGORIES_HEADER}\n",
            transactions=f"{_TRANSACTIONS_HEADER}\n",
        )
    )
    assert parsed.accounts == []
    assert parsed.categories == []
    assert parsed.transactions == []
    assert not parsed.warnings


@pytest.mark.parametrize(
    ("account_row", "reason"),
    [
        (",credit_card,,,,INR,", "missing name"),
        ("X,not_a_type,,,,INR,", "unknown account type"),
        ("X,credit_card,,,abc,INR,", "opening_balance_paise must be an integer"),
    ],
)
def test_account_row_rejections(account_row: str, reason: str) -> None:
    parsed = parse_backup_zip(_valid_zip(accounts=f"{_ACCOUNTS_HEADER}\n{account_row}\n"))
    assert parsed.accounts == []
    assert any(reason in w for w in parsed.warnings)


def test_account_bad_last4_is_coerced_not_rejected() -> None:
    # A malformed last4 is cosmetic — drop it, keep the account (else its txns cascade-fail).
    parsed = parse_backup_zip(
        _valid_zip(accounts=f"{_ACCOUNTS_HEADER}\nX,credit_card,,99,-1,INR,\n")
    )
    assert len(parsed.accounts) == 1
    assert parsed.accounts[0].last4 is None


@pytest.mark.parametrize(
    ("category_row", "reason"),
    [
        (",spend,,", "missing name"),
        ("Food,not_a_kind,,", "unknown category kind"),
    ],
)
def test_category_row_rejections(category_row: str, reason: str) -> None:
    parsed = parse_backup_zip(_valid_zip(categories=f"{_CATEGORIES_HEADER}\n{category_row}\n"))
    assert parsed.categories == []
    assert any(reason in w for w in parsed.warnings)


def test_category_bad_color_is_coerced_not_rejected() -> None:
    parsed = parse_backup_zip(
        _valid_zip(categories=f"{_CATEGORIES_HEADER}\nFood,spend,notacolor,\n")
    )
    assert len(parsed.categories) == 1
    assert parsed.categories[0].color is None


@pytest.mark.parametrize(
    ("txn_row", "reason"),
    [
        ("2026-07-01,,-100,spend,A,a,,,,import,,", "missing account_name"),
        ("2026-07-01,Axis CC,,spend,A,a,,,,import,,", "missing amount_paise"),
        ("2026-07-01,Axis CC,-100,not_a_type,A,a,,,,import,,", "unknown transaction type"),
        ("2026-07-01,Axis CC,-100,spend,A,a,,,,not_a_source,,", "unknown source"),
    ],
)
def test_transaction_row_rejections(txn_row: str, reason: str) -> None:
    parsed = parse_backup_zip(_valid_zip(transactions=f"{_TRANSACTIONS_HEADER}\n{txn_row}\n"))
    assert parsed.transactions == []
    assert any(reason in w for w in parsed.warnings)


def test_transaction_invalid_category_kind_leaves_it_uncategorized() -> None:
    # A present category_name with a garbage kind → the row still imports (uncategorized), not
    # rejected — the transaction is the data worth keeping.
    txn = "2026-07-01,Axis CC,-100,spend,A,a,Food,not_a_kind,,import,2026-07-01T10:00:00,"
    parsed = parse_backup_zip(_valid_zip(transactions=f"{_TRANSACTIONS_HEADER}\n{txn}\n"))
    assert len(parsed.transactions) == 1
    assert parsed.transactions[0].category_kind is None
