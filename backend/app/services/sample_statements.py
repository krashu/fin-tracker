"""Bundled synthetic sample statements a guest can import (ADR-0003 rule 15).

A guest can never upload a file, so the demo needs a statement to import. Each sample is a
directory under ``app/sample_statements/`` holding ``tables.json`` (pdfplumber ``pages ->
rows -> cells``) and ``summary.txt`` (the statement's text lines). Loading composes the
registered parser's ``interpret_tables`` and ``interpret_summary`` directly: it never calls
``parse``, so it skips PDF extraction and the upload path's parse-boundary handling. Note that
``interpret_*`` also skips any first-row running-balance check that ``parse`` does for a bank
statement.

Adding a sample is one :data:`SAMPLES` entry plus its directory; nothing else is keyed by
issuer. The bank sample (HDFC savings) opens at a balance its summary states, and its running
balance chains, so the import reconciles to zero though ``interpret_*`` skips the row-1 check.
Assets must pass the fixture-redaction guard (``tests/test_fixture_redaction.py``).

Caller owns the commit, as everywhere in ``services/``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Account
from app.parsers import AccountType, ParsedStatement
from app.services.import_service import (
    PARSERS,
    ImportResult,
    load_statement_account,
    persist_statement,
)

SAMPLE_ROOT = Path(__file__).parent.parent / "sample_statements"


class UnknownSampleError(LookupError):
    """No bundled sample has this id."""


class SampleAccountMismatchError(ValueError):
    """The account's issuer or type is not the one the sample was written for."""


@dataclass(frozen=True, slots=True)
class SampleStatement:
    id: str
    label: str
    issuer: str
    account_type: AccountType
    directory: str


SAMPLES: tuple[SampleStatement, ...] = (
    SampleStatement(
        id="axis-cc-2025-06",
        label="Axis Bank credit card, June 2025",
        issuer="axis",
        account_type="credit_card",
        directory="axis_cc_2025_06",
    ),
    SampleStatement(
        id="hdfc-savings-2025-06",
        label="HDFC Bank savings account, June 2025",
        issuer="hdfc",
        account_type="bank",
        directory="hdfc_savings_2025_06",
    ),
)
SAMPLES_BY_ID: dict[str, SampleStatement] = {s.id: s for s in SAMPLES}


def eligible_account_ids(session: Session, *, user_id: UUID, sample: SampleStatement) -> list[int]:
    """The user's live INR accounts this sample can be imported into, by id."""
    return list(
        session.scalars(
            select(Account.id)
            .where(
                Account.user_id == user_id,
                Account.archived_at.is_(None),
                Account.currency == "INR",
                Account.issuer == sample.issuer,
                Account.type == sample.account_type,
            )
            .order_by(Account.id)
        )
    )


def load_sample(sample: SampleStatement) -> tuple[ParsedStatement, str]:
    """The parsed sample and a stable file hash, so a re-import reconciles like a re-upload."""
    parser = PARSERS[(sample.issuer, sample.account_type)]
    tables_bytes = (SAMPLE_ROOT / sample.directory / "tables.json").read_bytes()
    summary_bytes = (SAMPLE_ROOT / sample.directory / "summary.txt").read_bytes()
    statement = ParsedStatement(
        rows=parser.interpret_tables(json.loads(tables_bytes)),
        summary=parser.interpret_summary(summary_bytes.decode().splitlines()),
    )
    return statement, hashlib.sha256(tables_bytes + b"\0" + summary_bytes).hexdigest()


def import_sample_statement(
    session: Session, *, user_id: UUID, account_id: int, sample_id: str
) -> ImportResult:
    """Stage a bundled sample into one of the caller's accounts.

    Raises:
        UnknownSampleError: no sample has ``sample_id``.
        AccountNotFoundError / NonInrAccountError: as :func:`load_statement_account`.
        SampleAccountMismatchError: the account is for another issuer or type.
        StatementMerchantTooLongError: as :func:`persist_statement`.
    """
    sample = SAMPLES_BY_ID.get(sample_id)
    if sample is None:
        raise UnknownSampleError(sample_id)
    account = load_statement_account(session, user_id=user_id, account_id=account_id)
    if (account.issuer, account.type) != (sample.issuer, sample.account_type):
        raise SampleAccountMismatchError(sample_id)
    statement, file_hash = load_sample(sample)
    return persist_statement(
        session,
        user_id=user_id,
        account=account,
        parser_name=PARSERS[(sample.issuer, sample.account_type)].__name__,
        source_file_hash=file_hash,
        statement=statement,
    )
