"""Bundled sample statements (ADR-0003 rule 15): they parse with their registered parser,
reconcile on a fresh account, and re-importing one stages nothing new."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Account, ImportBatch, Transaction, User
from app.services.import_service import PARSERS
from app.services.sample_statements import (
    SAMPLE_ROOT,
    SAMPLES,
    SAMPLES_BY_ID,
    import_sample_statement,
    load_sample,
)

# (date, amount_paise, merchant_raw) of the Axis June 2025 sample.
_AXIS_2025_06 = [
    (date(2025, 6, 1), -45000, "SWIGGY BANGALORE"),
    (date(2025, 6, 3), -31250, "UBER INDIA"),
    (date(2025, 6, 5), -184075, "BIGBASKET BANGALORE"),
    (date(2025, 6, 8), -36500, "STARBUCKS BANGALORE"),
    (date(2025, 6, 10), -68900, "ZOMATO ONLINE"),
    (date(2025, 6, 12), -52400, "DOMINOS PIZZA"),
    (date(2025, 6, 14), -64900, "NETFLIX SUBSCRIPTION"),
    (date(2025, 6, 16), 15000, "REFUND UBER INDIA"),
    (date(2025, 6, 18), 12000, "CASHBACK CREDIT"),
    (date(2025, 6, 20), -38000, "SWIGGY BANGALORE"),
    (date(2025, 6, 22), -24500, "UBER INDIA"),
    (date(2025, 6, 25), 569900, "PAYMENT RECEIVED THANK YOU"),
]


# (date, amount_paise, merchant_raw) of the HDFC savings June 2025 sample.
_HDFC_2025_06 = [
    (
        date(2025, 6, 1),
        8500000,
        "NEFT CR-EXAM0000001-SENTINEL EMPLOYER PVT LTD-TEST USER-EXAMN00000000001",
    ),
    (date(2025, 6, 2), -45000, "UPI-SWIGGY-swiggy@example-EXAM0000002-000000000002-UPI"),
    (date(2025, 6, 4), -31250, "UPI-UBER INDIA-uber@example-EXAM0000003-000000000003-UPI"),
    (date(2025, 6, 6), -184075, "UPI-BIGBASKET-bigbasket@example-EXAM0000004-000000000004-UPI"),
    (date(2025, 6, 8), -64900, "UPI-NETFLIX-netflix@example-EXAM0000005-000000000005-UPI"),
    (date(2025, 6, 10), -68900, "UPI-ZOMATO-zomato@example-EXAM0000006-000000000006-UPI"),
    (date(2025, 6, 10), 68900, "REV-UPI-000000000006-ZOMATO-zomato@example"),
    (date(2025, 6, 12), -500000, "ATW-000000XXXXXX0000-S1AEXAM0-MUMBAI"),
    (date(2025, 6, 15), -1234567, "CC 000000XXXXXX0000 AUTOPAY SI-TAD"),
    (date(2025, 6, 18), -36500, "UPI-STARBUCKS-starbucks@example-EXAM0000007-000000000007-UPI"),
    (date(2025, 6, 20), 2500, "DEBIT CARD CASHBACK"),
    (date(2025, 6, 24), -52400, "UPI-DOMINOS PIZZA-dominos@example-EXAM0000008-000000000008-UPI"),
]
_ROW_COUNTS = {"axis-cc-2025-06": len(_AXIS_2025_06), "hdfc-savings-2025-06": len(_HDFC_2025_06)}
_ALL_IDS = list(_ROW_COUNTS)


@pytest.fixture
def account_for(session: Session):
    """Build a fresh account of the kind a sample was written for."""
    user = User(id=get_settings().v1_user_id)
    session.add(user)
    session.flush()

    def make(sample_id: str) -> Account:
        sample = SAMPLES_BY_ID[sample_id]
        account = Account(
            user_id=user.id,
            name=sample.label,
            type=sample.account_type,
            issuer=sample.issuer,
            last4="1234",
        )
        session.add(account)
        session.commit()
        return account

    return make


def test_every_sample_parses_with_its_registered_parser() -> None:
    assert len({s.id for s in SAMPLES}) == len(SAMPLES)
    for sample in SAMPLES:
        assert (sample.issuer, sample.account_type) in PARSERS
        statement, _ = load_sample(sample)
        assert statement.summary.opening_balance_paise is not None
        assert statement.summary.closing_balance_paise is not None
        assert statement.summary.period_start is not None
        assert statement.summary.period_end is not None

    statement, _ = load_sample(SAMPLES_BY_ID["axis-cc-2025-06"])
    assert [(r.date, r.amount_paise, r.merchant_raw) for r in statement.rows] == _AXIS_2025_06

    statement, _ = load_sample(SAMPLES_BY_ID["hdfc-savings-2025-06"])
    assert [(r.date, r.amount_paise, r.merchant_raw) for r in statement.rows] == _HDFC_2025_06


@pytest.mark.parametrize("sample_id", _ALL_IDS)
def test_sample_reconciles_on_a_fresh_account(
    session: Session, account_for: Callable[[str], Account], sample_id: str
) -> None:
    account = account_for(sample_id)

    result = import_sample_statement(
        session, user_id=account.user_id, account_id=account.id, sample_id=sample_id
    )

    assert result.imported == _ROW_COUNTS[sample_id]
    assert result.reconciliation_delta_paise == 0


@pytest.mark.parametrize("sample_id", _ALL_IDS)
def test_reimporting_a_sample_stages_nothing_new(
    session: Session, account_for: Callable[[str], Account], sample_id: str
) -> None:
    account = account_for(sample_id)
    first = import_sample_statement(
        session, user_id=account.user_id, account_id=account.id, sample_id=sample_id
    )
    again = import_sample_statement(
        session, user_id=account.user_id, account_id=account.id, sample_id=sample_id
    )

    assert again.batch_id == first.batch_id
    assert again.already_imported is True
    assert session.scalar(select(func.count()).select_from(ImportBatch)) == 1
    assert session.scalar(select(func.count()).select_from(Transaction)) == _ROW_COUNTS[sample_id]


@pytest.mark.parametrize("sample_id", _ALL_IDS)
def test_sample_hash_covers_both_asset_files(sample_id: str) -> None:
    """The hash is sha256(tables + NUL + summary): a re-import reconciles like a re-upload,
    and editing either file makes a different file."""
    sample = SAMPLES_BY_ID[sample_id]
    tables = (SAMPLE_ROOT / sample.directory / "tables.json").read_bytes()
    summary = (SAMPLE_ROOT / sample.directory / "summary.txt").read_bytes()

    _, file_hash = load_sample(sample)

    assert file_hash == hashlib.sha256(tables + b"\0" + summary).hexdigest()
    assert file_hash != hashlib.sha256(tables).hexdigest()
    assert file_hash != hashlib.sha256(summary + b"\0" + tables).hexdigest()
