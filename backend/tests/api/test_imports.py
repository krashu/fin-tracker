"""End-to-end tests for ``POST /api/v1/imports`` (PRD §F1).

Parser dispatch is bypassed via a ``StubParser`` registered in
:data:`app.services.import_service.PARSERS` so the test exercises the
full HTTP → service → DB flow without a real PDF. The committed JSON
table fixtures cover the parser layer itself; the ``_local/*.pdf`` path
will be exercised in the next-increment real-PDF parametrized test.
"""

from __future__ import annotations

from datetime import date
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Account, Category, ImportBatch, Transaction, User
from app.parsers import ParsedStatement, RawTransaction, StatementSummary
from app.parsers.base import InvalidPasswordError, ParserError
from app.services import import_service

# Canned rows modelled on tests/fixtures/axis_cc/tables_sample.expected.json
# (4 purchases, 1 payment, 1 refund, 1 other). Deliberately NOT a row-for-row
# mirror: these drive the import/commit API paths, so classification coverage
# belongs in tests/parsers/test_axis_cc.py against the fixture itself. Don't
# restate the fixture's row count here — it drifts the moment a row is added.
_AXIS_ROWS_A: list[RawTransaction] = [
    RawTransaction(
        date=date(2026, 3, 5),
        amount_paise=-8500,
        merchant_raw="SENTINEL TRANSPORT NCR",
        txn_type="purchase",
    ),
    RawTransaction(
        date=date(2026, 3, 10),
        amount_paise=-12345678,
        merchant_raw="SENTINEL ELECTRONICS DEL",
        txn_type="purchase",
    ),
    RawTransaction(
        date=date(2026, 3, 12),
        amount_paise=-25000,
        merchant_raw="FINANCE CHARGE",
        txn_type="other",
    ),
    RawTransaction(
        date=date(2026, 3, 15),
        amount_paise=-45000,
        merchant_raw="SENTINEL CAFE BLR",
        txn_type="purchase",
    ),
    RawTransaction(
        date=date(2026, 3, 20),
        amount_paise=1000000,
        merchant_raw="PAYMENT RECEIVED THANK YOU",
        txn_type="payment",
    ),
    RawTransaction(
        date=date(2026, 3, 25),
        amount_paise=99950,
        merchant_raw="REFUND SENTINEL MERCHANT",
        txn_type="refund",
    ),
    RawTransaction(
        date=date(2026, 3, 28),
        amount_paise=5000,
        merchant_raw="CASHBACK SENTINEL",
        txn_type="other",
    ),
]

# File B: 5 fingerprints overlap with file A (rows 0..4 unchanged) + 2 new
# rows replace A's last two. Tests row-level dedup on a *different*
# source_file_hash (so the batch-level short-circuit does not fire).
_AXIS_ROWS_B: list[RawTransaction] = [
    *_AXIS_ROWS_A[:5],
    RawTransaction(
        date=date(2026, 4, 2),
        amount_paise=-37500,
        merchant_raw="SENTINEL BOOKSTORE BLR",
        txn_type="purchase",
    ),
    RawTransaction(
        date=date(2026, 4, 5),
        amount_paise=-21000,
        merchant_raw="SENTINEL FUEL NCR",
        txn_type="purchase",
    ),
]


class _StubAxisParser:
    """Returns whatever rows are placed on its ``rows`` class attribute."""

    rows: ClassVar[list[RawTransaction]] = []

    @classmethod
    def parse(cls, pdf_bytes: bytes, password: str | None) -> ParsedStatement:
        return ParsedStatement(rows=list(cls.rows), summary=StatementSummary())


@pytest.fixture
def stub_axis_parser(monkeypatch: pytest.MonkeyPatch) -> type[_StubAxisParser]:
    """Register :class:`_StubAxisParser` for ``("axis", "credit_card")``."""
    monkeypatch.setitem(import_service.PARSERS, ("axis", "credit_card"), _StubAxisParser)
    _StubAxisParser.rows = list(_AXIS_ROWS_A)
    return _StubAxisParser


def _post_import(
    client: TestClient,
    *,
    account_id: int,
    file_content: bytes,
    filename: str = "statement.pdf",
) -> dict[str, object]:
    resp = client.post(
        "/api/v1/imports",
        data={"account_id": str(account_id)},
        files={"file": (filename, file_content, "application/pdf")},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_import_duplicate_rows_end_to_end(
    client: TestClient,
    axis_account: Account,
    stub_axis_parser: type[_StubAxisParser],
    session_factory: sessionmaker[Session],
) -> None:
    """D1 through the real route, past the commit (ADR-0006).

    This is the 500 guard: ``api/v1/imports`` has no ``IntegrityError`` branch, so
    if the widened unique constraint or the occurrence assignment ever regresses,
    the flush raises and the caller sees a 500 rather than a wrong count. The
    service-level test cannot catch that — it commits in the test body.
    """
    dupe = RawTransaction(
        date=date(2026, 5, 4),
        amount_paise=-25000,
        merchant_raw="UBER INDIA",
        txn_type="purchase",
    )
    stub_axis_parser.rows = [dupe, dupe]

    body = _post_import(client, account_id=axis_account.id, file_content=b"file-dupes")

    assert body["imported"] == 2
    assert body["skipped"] == 0
    assert body["pending_count"] == 2

    with session_factory() as s:
        rows = s.scalars(select(Transaction)).all()
        assert {t.occurrence for t in rows} == {0, 1}
        assert len({t.fingerprint for t in rows}) == 1


def test_import_persists_transactions(
    client: TestClient,
    axis_account: Account,
    stub_axis_parser: type[_StubAxisParser],
    session_factory: sessionmaker[Session],
) -> None:
    body = _post_import(client, account_id=axis_account.id, file_content=b"file-A")

    assert body == {
        "batch_id": 1,
        "imported": 7,
        "skipped": 0,
        "already_imported": False,
        "pending_count": 7,
        "duplicate_of_account_id": None,
        "duplicate_of_account_archived": False,
        "reconciliation_delta_paise": None,
    }

    with session_factory() as s:
        txn_count = s.scalar(select(func.count()).select_from(Transaction))
        assert txn_count == 7
        batch = s.get(ImportBatch, 1)
        assert batch is not None
        assert batch.status == "completed"
        assert batch.imported_count == 7
        assert batch.skipped_count == 0


def test_reimport_same_file_reconciles_no_new_rows(
    client: TestClient,
    axis_account: Account,
    stub_axis_parser: type[_StubAxisParser],
    session_factory: sessionmaker[Session],
) -> None:
    """Re-upload with nothing missing: reuse the batch, re-parse, skip every row.

    All 7 rows are still present (pending from the first import), so the second
    upload adds none but reports them as skipped-dupes and surfaces the still-
    pending count. already_imported flags "this file hash was seen before".
    """
    first = _post_import(client, account_id=axis_account.id, file_content=b"file-A")
    second = _post_import(client, account_id=axis_account.id, file_content=b"file-A")

    assert first == {
        "batch_id": 1,
        "imported": 7,
        "skipped": 0,
        "already_imported": False,
        "pending_count": 7,
        "duplicate_of_account_id": None,
        "duplicate_of_account_archived": False,
        "reconciliation_delta_paise": None,
    }
    assert second == {
        "batch_id": 1,
        "imported": 0,
        "skipped": 7,
        "already_imported": True,
        "pending_count": 7,
        "duplicate_of_account_id": None,
        "duplicate_of_account_archived": False,
        "reconciliation_delta_paise": None,
    }

    with session_factory() as s:
        # Reused the batch (no duplicate) and added no duplicate transactions.
        assert s.scalar(select(func.count()).select_from(ImportBatch)) == 1
        assert s.scalar(select(func.count()).select_from(Transaction)) == 7
        # Batch counters record the FIRST import — not inflated by the re-upload.
        batch = s.get(ImportBatch, 1)
        assert batch is not None
        assert batch.imported_count == 7
        assert batch.skipped_count == 0


def test_reimport_different_bytes_row_level_dedupes(
    client: TestClient,
    axis_account: Account,
    stub_axis_parser: type[_StubAxisParser],
    session_factory: sessionmaker[Session],
) -> None:
    first = _post_import(client, account_id=axis_account.id, file_content=b"file-A")
    assert first == {
        "batch_id": 1,
        "imported": 7,
        "skipped": 0,
        "already_imported": False,
        "pending_count": 7,
        "duplicate_of_account_id": None,
        "duplicate_of_account_archived": False,
        "reconciliation_delta_paise": None,
    }

    _StubAxisParser.rows = list(_AXIS_ROWS_B)
    second = _post_import(client, account_id=axis_account.id, file_content=b"file-B")
    assert second == {
        "batch_id": 2,
        "imported": 2,
        "skipped": 5,
        "already_imported": False,
        "pending_count": 2,
        "duplicate_of_account_id": None,
        "duplicate_of_account_archived": False,
        "reconciliation_delta_paise": None,
    }

    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(ImportBatch)) == 2
        assert s.scalar(select(func.count()).select_from(Transaction)) == 9


def test_reimport_resurfaces_rows_missing_from_the_db(
    client: TestClient,
    axis_account: Account,
    stub_axis_parser: type[_StubAxisParser],
    session_factory: sessionmaker[Session],
) -> None:
    """Rows deleted before commit re-surface on re-upload; present rows don't dup.

    Import 7 → delete 2 (as the review queue's discard does via
    ``DELETE /transactions/{id}``) → re-upload the same file. The 2 missing rows
    return as pending on the SAME batch; the 5 still-present rows are skipped.
    """
    first = _post_import(client, account_id=axis_account.id, file_content=b"file-A")
    batch_id = first["batch_id"]

    candidates = client.get(f"/api/v1/imports/{batch_id}/candidates").json()
    to_delete = [candidates[0]["id"], candidates[1]["id"]]
    for txn_id in to_delete:
        resp = client.delete(f"/api/v1/transactions/{txn_id}")
        assert resp.status_code == 204, resp.text

    second = _post_import(client, account_id=axis_account.id, file_content=b"file-A")
    assert second == {
        "batch_id": batch_id,
        "imported": 2,  # the 2 discarded rows re-surface
        "skipped": 5,  # the 5 still-present rows are skipped
        "already_imported": True,
        "pending_count": 7,  # 5 remaining + 2 resurfaced
        "duplicate_of_account_id": None,
        "duplicate_of_account_archived": False,
        "reconciliation_delta_paise": None,
    }

    with session_factory() as s:
        # Still one batch, back to 7 transactions, all pending again.
        assert s.scalar(select(func.count()).select_from(ImportBatch)) == 1
        assert s.scalar(select(func.count()).select_from(Transaction)) == 7
        # Counters untouched by the re-upload (record the first import).
        batch = s.get(ImportBatch, batch_id)
        assert batch is not None
        assert batch.imported_count == 7
        assert batch.skipped_count == 0


class _PasswordGatedParser:
    """Returns rows only when a password is supplied; else raises like a real
    parser hitting an encrypted PDF with no/blank password."""

    rows: ClassVar[list[RawTransaction]] = list(_AXIS_ROWS_A)

    @classmethod
    def parse(cls, pdf_bytes: bytes, password: str | None) -> ParsedStatement:
        if not password:
            raise InvalidPasswordError("encrypted PDF requires a password")
        return ParsedStatement(rows=list(cls.rows), summary=StatementSummary())


def test_reimport_encrypted_pdf_without_password_422s(
    client: TestClient,
    axis_account: Account,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-upload re-parses, so a protected file needs its password again.

    First upload supplies the password → parses + completes. The re-upload with a
    blank password now reaches the parser (no short-circuit) and 422s, where the
    old batch-level short-circuit would have returned 200 already_imported.
    """
    monkeypatch.setitem(import_service.PARSERS, ("axis", "credit_card"), _PasswordGatedParser)

    first = client.post(
        "/api/v1/imports",
        data={"account_id": str(axis_account.id), "password": "secret"},
        files={"file": ("statement.pdf", b"file-A", "application/pdf")},
    )
    assert first.status_code == 200, first.text
    assert first.json()["imported"] == len(_AXIS_ROWS_A)

    second = client.post(
        "/api/v1/imports",
        data={"account_id": str(axis_account.id)},  # no password
        files={"file": ("statement.pdf", b"file-A", "application/pdf")},
    )
    assert second.status_code == 422
    assert second.json()["detail"] == "incorrect or missing PDF password"


def test_reconcile_dupcheck_scoped_to_statement_date_range(
    client: TestClient,
    axis_account: Account,
    seeded_user: User,
    stub_axis_parser: type[_StubAxisParser],
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """Dedup loads only the statement's date window, yet stays exact.

    An in-window pre-existing row (same fingerprint as a statement row) must
    still dedup; an out-of-window row must be ignored. Proves correctness is
    independent of account-history size (the whole point of the range scoping).
    """
    # Local imports: keeps this test self-contained (the module header is
    # ruff-managed and would strip these as they aren't used elsewhere).
    from datetime import UTC, datetime

    from app.services.fingerprint import transaction_fingerprint
    from app.services.merchant import normalize_merchant

    # In-window manual row matching _AXIS_ROWS_A[0] (2026-03-05) → must be skipped.
    match = _AXIS_ROWS_A[0]
    match_norm = normalize_merchant(match.merchant_raw)
    match_fp = transaction_fingerprint(
        txn_date=match.date,
        amount_paise=match.amount_paise,
        normalized_merchant=match_norm,
        account_id=axis_account.id,
    )
    session.add(
        Transaction(
            user_id=seeded_user.id,
            account_id=axis_account.id,
            date=match.date,
            amount_paise=match.amount_paise,
            transaction_type="spend",
            merchant_raw=match.merchant_raw,
            merchant_normalized=match_norm,
            fingerprint=match_fp,
            source="manual",
            confirmed_at=datetime(2026, 3, 5, tzinfo=UTC),
        )
    )
    # Out-of-window row (2026-01-01, outside [03-05, 03-28]) → must be ignored by
    # the range-scoped dedup load and must never be treated as a duplicate.
    session.add(
        Transaction(
            user_id=seeded_user.id,
            account_id=axis_account.id,
            date=date(2026, 1, 1),
            amount_paise=-9999,
            transaction_type="spend",
            merchant_raw="OLD OUT OF WINDOW",
            merchant_normalized="old out of window",
            fingerprint="out-of-window-sentinel-fp",
            source="manual",
            confirmed_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )
    session.commit()

    body = _post_import(client, account_id=axis_account.id, file_content=b"file-A")
    # 7 parsed rows; row[0] matches the in-window manual row → 6 imported, 1 skipped.
    assert body["imported"] == 6
    assert body["skipped"] == 1
    assert body["pending_count"] == 6

    with session_factory() as s:
        # 2 seeded + 6 imported, no duplicate of the matched row.
        assert s.scalar(select(func.count()).select_from(Transaction)) == 8


def test_import_empty_statement_skips_dupcheck(
    client: TestClient,
    axis_account: Account,
    stub_axis_parser: type[_StubAxisParser],
) -> None:
    """A parser yielding no rows must not crash the date-range dup-check.

    ``min()``/``max()`` over an empty list would raise, so the query is guarded
    (skipped) when there are no rows.
    """
    _StubAxisParser.rows = []
    body = _post_import(client, account_id=axis_account.id, file_content=b"empty")
    assert body == {
        "batch_id": 1,
        "imported": 0,
        "skipped": 0,
        "already_imported": False,
        "pending_count": 0,
        "duplicate_of_account_id": None,
        "duplicate_of_account_archived": False,
        "reconciliation_delta_paise": None,
    }


def test_two_axis_cards_dedup_independent(
    client: TestClient,
    seeded_user: User,
    session: Session,
    stub_axis_parser: type[_StubAxisParser],
    session_factory: sessionmaker[Session],
) -> None:
    """Two Axis CC accounts with distinct last4 dedup independently."""
    card_a = Account(
        user_id=seeded_user.id,
        name="Axis CC A",
        type="credit_card",
        issuer="axis",
        last4="1234",
    )
    card_b = Account(
        user_id=seeded_user.id,
        name="Axis CC B",
        type="credit_card",
        issuer="axis",
        last4="5678",
    )
    session.add_all([card_a, card_b])
    session.commit()
    session.refresh(card_a)
    session.refresh(card_b)

    body_a = _post_import(client, account_id=card_a.id, file_content=b"file-A")
    body_b = _post_import(client, account_id=card_b.id, file_content=b"file-A")

    assert body_a["imported"] == 7
    assert body_b["imported"] == 7
    assert body_a["batch_id"] != body_b["batch_id"]

    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(Transaction)) == 14
        per_account = dict(
            s.execute(
                select(Transaction.account_id, func.count()).group_by(Transaction.account_id)
            ).all()
        )
        assert per_account == {card_a.id: 7, card_b.id: 7}


def _two_axis_cards(session: Session, user: User) -> tuple[Account, Account]:
    """Two Axis CC accounts for the same user, distinct last4."""
    card_a = Account(
        user_id=user.id,
        name="Axis CC A",
        type="credit_card",
        issuer="axis",
        last4="1234",
    )
    card_b = Account(
        user_id=user.id,
        name="Axis CC B",
        type="credit_card",
        issuer="axis",
        last4="5678",
    )
    session.add_all([card_a, card_b])
    session.commit()
    session.refresh(card_a)
    session.refresh(card_b)
    return card_a, card_b


def test_same_file_into_another_account_names_that_account(
    client: TestClient,
    seeded_user: User,
    seeded_categories: list[Category],
    session: Session,
    stub_axis_parser: type[_StubAxisParser],
) -> None:
    """UX-09b: an identical file uploaded against a DIFFERENT account reports it.

    The F4 dedup key is per-account by design — that scoping is what makes
    cross-account isolation hold — so nothing else catches a wrong-account import.
    The same-account re-upload stays the ``already_imported`` path's business and
    must keep reporting ``None`` for this field.
    """
    card_a, card_b = _two_axis_cards(session, seeded_user)

    first = _post_import(client, account_id=card_a.id, file_content=b"file-A")
    assert first["duplicate_of_account_id"] is None
    ids = [c["id"] for c in client.get(f"/api/v1/imports/{first['batch_id']}/candidates").json()]
    commit = client.post(
        f"/api/v1/imports/{first['batch_id']}/commit",
        json={"transaction_ids": ids},
    )
    assert commit.status_code == 204, commit.text

    # Same account again: already_imported owns this, the probe stays silent.
    again = _post_import(client, account_id=card_a.id, file_content=b"file-A")
    assert again["already_imported"] is True
    assert again["duplicate_of_account_id"] is None

    other = _post_import(client, account_id=card_b.id, file_content=b"file-A")
    assert other["duplicate_of_account_id"] == card_a.id
    assert other["duplicate_of_account_archived"] is False
    # Not an error — the rows still stage, the frontend warns before commit.
    assert other["already_imported"] is False
    assert other["pending_count"] == 7


def test_duplicate_account_flagged_archived_when_source_is_archived(
    client: TestClient,
    seeded_user: User,
    session: Session,
    stub_axis_parser: type[_StubAxisParser],
) -> None:
    """``GET /accounts`` hides archived rows, so the id alone won't resolve to a
    label — the flag is what lets the form say "an archived account"."""
    card_a, card_b = _two_axis_cards(session, seeded_user)

    _post_import(client, account_id=card_a.id, file_content=b"file-A")
    archived = client.delete(f"/api/v1/accounts/{card_a.id}")
    assert archived.status_code == 204, archived.text

    other = _post_import(client, account_id=card_b.id, file_content=b"file-A")
    assert other["duplicate_of_account_id"] == card_a.id
    assert other["duplicate_of_account_archived"] is True


class _RaisingParser:
    """Parser stub that raises whatever exception is placed on ``exc``."""

    exc: ClassVar[Exception] = ParserError("boom")

    @classmethod
    def parse(cls, pdf_bytes: bytes, password: str | None) -> list[RawTransaction]:
        raise cls.exc


def test_import_unknown_account_returns_404(
    client: TestClient,
    seeded_user: User,
) -> None:
    resp = client.post(
        "/api/v1/imports",
        data={"account_id": "9999"},
        files={"file": ("statement.pdf", b"x", "application/pdf")},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "account not found"


def test_import_onto_archived_account_is_refused(
    client: TestClient,
    axis_account: Account,
    stub_axis_parser: type[_StubAxisParser],
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """B#8/#20: statement import was the one transaction-write path that accepted an
    archived account, while its four siblings (transactions.py:204/346, accounts.py:137/222)
    all refuse one.

    Reachable through a stale cross-tab ``["accounts"]`` cache: the rows committed onto an
    account ``GET /accounts`` will never return again — counted in net worth by design, but
    unselectable in the /expenses filter and rendered with an em-dash.
    """
    resp = client.delete(f"/api/v1/accounts/{axis_account.id}")
    assert resp.status_code == 204, resp.text

    refused = client.post(
        "/api/v1/imports",
        data={"account_id": str(axis_account.id)},
        files={"file": ("statement.pdf", b"file-A", "application/pdf")},
    )

    assert refused.status_code == 404
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(Transaction)) == 0
        assert s.scalar(select(func.count()).select_from(ImportBatch)) == 0

    # The same upload onto an ACTIVE card still succeeds — the guard refuses archived
    # accounts, not every account.
    active = Account(
        user_id=axis_account.user_id,
        name="Axis CC 2",
        type="credit_card",
        issuer="axis",
        last4="4321",
    )
    session.add(active)
    session.commit()

    body = _post_import(client, account_id=active.id, file_content=b"file-A")
    assert body["imported"] == len(_AXIS_ROWS_A)


def test_import_unknown_parser_returns_422(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    """An account whose (issuer, type) has no PARSERS entry → 422."""
    bank = Account(
        user_id=seeded_user.id,
        name="No-Parser Bank",
        type="bank",
        issuer="sbi",
    )
    session.add(bank)
    session.commit()
    session.refresh(bank)

    resp = client.post(
        "/api/v1/imports",
        data={"account_id": str(bank.id)},
        files={"file": ("statement.pdf", b"x", "application/pdf")},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "no parser registered for this issuer/type"


def test_import_usd_account_returns_422(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    """A USD account (reachable only via model/seed — AccountCreate blocks it)
    is rejected before parser dispatch: v1 import is INR-only, and this must be
    a 422, not the 404 an AccountNotFoundError would give."""
    usd_cc = Account(
        user_id=seeded_user.id,
        name="US Card",
        type="credit_card",
        issuer="axis",
        currency="USD",
    )
    session.add(usd_cc)
    session.commit()
    session.refresh(usd_cc)

    resp = client.post(
        "/api/v1/imports",
        data={"account_id": str(usd_cc.id)},
        files={"file": ("statement.pdf", b"x", "application/pdf")},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "statement import requires an INR account"


def test_import_invalid_password_returns_422(
    client: TestClient,
    axis_account: Account,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `monkeypatch.setattr` (not bare `_RaisingParser.exc = ...`) so the
    # class attribute reverts on teardown. Bare assignment would leak the
    # exception type into whatever test runs next in the suite.
    pwd = "MY-SECRET-PWD-9F2E"
    monkeypatch.setattr(_RaisingParser, "exc", InvalidPasswordError(f"wrong password: {pwd}"))
    monkeypatch.setitem(import_service.PARSERS, ("axis", "credit_card"), _RaisingParser)

    resp = client.post(
        "/api/v1/imports",
        data={"account_id": str(axis_account.id), "password": pwd},
        files={"file": ("statement.pdf", b"x", "application/pdf")},
    )
    assert resp.status_code == 422
    # Layered leak guards:
    # 1. Exact-string detail — any change to detail=str(e) fails CI.
    # 2. resp.text full-body check — catches a future regression that
    #    adds a sibling field (e.g. `received_password`, `hint`) to the
    #    HTTPException response or wraps it in an error envelope.
    assert resp.json()["detail"] == "incorrect or missing PDF password"
    assert pwd not in resp.text


def test_import_parser_error_returns_422(
    client: TestClient,
    axis_account: Account,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    err_msg = "layout exploded with internal-detail-XYZ"
    monkeypatch.setattr(_RaisingParser, "exc", ParserError(err_msg))
    monkeypatch.setitem(import_service.PARSERS, ("axis", "credit_card"), _RaisingParser)

    resp = client.post(
        "/api/v1/imports",
        data={"account_id": str(axis_account.id)},
        files={"file": ("statement.pdf", b"x", "application/pdf")},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "could not parse statement file"
    # Internal parser-error messages must not bleed into the response.
    assert err_msg not in resp.text
