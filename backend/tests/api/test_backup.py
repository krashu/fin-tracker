"""API tests for ``/api/v1/backup`` (PRD §F10).

Covers the download response shape (zip + attachment header), a full additive import through
the route (accounts/categories/transactions created), and the generic 422 for an unreadable
upload.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Account, Label, Transaction, TransactionLabel, User
from app.parsers.backup_csv import BACKUP_FORMAT_VERSION, parse_backup_zip
from app.services.fingerprint import transaction_fingerprint
from tests.backup_zip import account, make_backup_zip, txn


def _minimal_backup_zip() -> bytes:
    return make_backup_zip(
        accounts=[account(opening_balance_paise=-50000)],
        categories=[{"name": "Food", "kind": "spend", "color": "#4f46e5"}],
        transactions=[txn(category_name="Food", category_kind="spend", labels="online;lunch")],
    )


@pytest.fixture
def seeded_spend(session: Session, seeded_user: User) -> None:
    """One account + one confirmed transaction so the download has something to export."""
    account = Account(
        user_id=seeded_user.id,
        name="Axis CC",
        type="credit_card",
        issuer="axis",
        last4="1234",
        opening_balance_paise=-500000,
    )
    session.add(account)
    session.flush()
    session.add(
        Transaction(
            user_id=seeded_user.id,
            account_id=account.id,
            date=datetime(2026, 7, 1).date(),
            amount_paise=-50000,
            transaction_type="spend",
            merchant_raw="SWIGGY",
            merchant_normalized="swiggy",
            fingerprint=transaction_fingerprint(
                txn_date=datetime(2026, 7, 1).date(),
                amount_paise=-50000,
                normalized_merchant="swiggy",
                account_id=account.id,
            ),
            source="import",
            confirmed_at=datetime(2026, 7, 1, 10, 0, 0),
        )
    )
    session.commit()


def test_download_backup_streams_a_zip(client: TestClient, seeded_spend: None) -> None:
    response = client.get("/api/v1/backup")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "attachment" in response.headers["content-disposition"]
    assert "fin-tracker-backup-" in response.headers["content-disposition"]

    parsed = parse_backup_zip(response.content)
    assert len(parsed.accounts) == 1
    assert len(parsed.transactions) == 1
    assert parsed.transactions[0].amount_paise == -50000


def test_backup_download_includes_labels(
    client: TestClient, session: Session, seeded_user: User
) -> None:
    """A labeled transaction exports its labels as a ``;``-joined cell that
    re-parses to the same names (round-trip fidelity for F3a)."""
    account = Account(
        user_id=seeded_user.id, name="Axis CC", type="credit_card", issuer="axis", last4="1234"
    )
    session.add(account)
    session.flush()
    txn = Transaction(
        user_id=seeded_user.id,
        account_id=account.id,
        date=datetime(2026, 7, 2).date(),
        amount_paise=-32000,
        transaction_type="spend",
        merchant_raw="MakeMyTrip",
        merchant_normalized="makemytrip",
        fingerprint="cc" * 32,
        source="manual",
        confirmed_at=datetime(2026, 7, 2, 9, 0, 0),
    )
    session.add(txn)
    session.flush()
    for name in ("travel", "goa"):
        lbl = Label(user_id=seeded_user.id, name=name)
        session.add(lbl)
        session.flush()
        session.add(
            TransactionLabel(transaction_id=txn.id, label_id=lbl.id, user_id=seeded_user.id)
        )
    session.commit()

    parsed = parse_backup_zip(client.get("/api/v1/backup").content)
    assert len(parsed.transactions) == 1
    assert set(parsed.transactions[0].labels) == {"travel", "goa"}


def test_import_backup_is_additive(client: TestClient, session: Session, seeded_user: User) -> None:
    response = client.post(
        "/api/v1/backup/import",
        files={"file": ("backup.zip", _minimal_backup_zip(), "application/zip")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["accounts_new"] == 1
    assert body["categories_new"] == 1
    assert body["txns_imported"] == 1
    assert body["txns_skipped_dupe"] == 0

    total = session.scalar(
        select(func.count()).select_from(Transaction).where(Transaction.user_id == seeded_user.id)
    )
    assert total == 1

    # The `;`-joined labels cell imported as two get-or-created, linked labels.
    assert {lab.name for lab in session.scalars(select(Label))} == {"online", "lunch"}
    assert session.scalar(select(func.count()).select_from(TransactionLabel)) == 2


def test_import_backup_normalizes_a_hand_edited_merchant_cell(
    client: TestClient, session: Session, seeded_user: User, seeded_spend: None
) -> None:
    """B#19: the restore path recomputes the F4 fingerprint over the zip's
    ``merchant_normalized`` cell verbatim — the only write path in the app storing a value no
    ``normalize_merchant`` call produced, while ``backup_csv`` declares hand-edited files its
    threat model and vocabulary-validates every other column.

    One capitalised cell used to fingerprint differently from the natively-imported twin, so
    the row staged as a NEW transaction (both then counting in spend-by-category) and the
    merchant could never auto-tag, because both prefetch maps key on the lowercase form.
    """
    zip_bytes = make_backup_zip(
        accounts=[account(opening_balance_paise=-50000)],
        categories=[{"name": "Food", "kind": "spend", "color": "#4f46e5"}],
        # merchant_normalized hand-edited to mixed case; everything else matches the
        # native row seeded by `seeded_spend`.
        transactions=[
            txn(merchant_normalized="SwiGGy", category_name="Food", category_kind="spend")
        ],
    )

    response = client.post(
        "/api/v1/backup/import",
        files={"file": ("backup.zip", zip_bytes, "application/zip")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["txns_imported"] == 0
    assert body["txns_skipped_dupe"] == 1

    rows = list(session.scalars(select(Transaction).where(Transaction.user_id == seeded_user.id)))
    assert len(rows) == 1
    assert rows[0].merchant_normalized == "swiggy"


def test_import_backup_normalizes_a_decomposed_merchant_cell(
    client: TestClient, session: Session, seeded_user: User, seeded_spend: None
) -> None:
    """ADR-0006 rule 3: a zip cell holding the decomposed spelling of a merchant already
    stored precomposed restores as that same row, not a new one."""
    row = session.scalars(select(Transaction).where(Transaction.user_id == seeded_user.id)).one()
    row.merchant_raw = "CAF\u00c9"
    row.merchant_normalized = "caf\u00e9"
    row.fingerprint = transaction_fingerprint(
        txn_date=row.date,
        amount_paise=row.amount_paise,
        normalized_merchant="caf\u00e9",
        account_id=row.account_id,
    )
    session.commit()

    zip_bytes = make_backup_zip(
        accounts=[account(opening_balance_paise=-50000)],
        categories=[{"name": "Food", "kind": "spend", "color": "#4f46e5"}],
        transactions=[
            txn(merchant_normalized="CAFE\u0301", category_name="Food", category_kind="spend")
        ],
    )

    response = client.post(
        "/api/v1/backup/import",
        files={"file": ("backup.zip", zip_bytes, "application/zip")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["txns_imported"] == 0
    assert body["txns_skipped_dupe"] == 1

    session.expire_all()
    rows = list(session.scalars(select(Transaction).where(Transaction.user_id == seeded_user.id)))
    assert len(rows) == 1
    assert rows[0].merchant_normalized == "caf\u00e9"


def test_import_backup_rejects_unreadable_upload(client: TestClient, seeded_user: User) -> None:
    response = client.post(
        "/api/v1/backup/import",
        files={"file": ("backup.zip", b"this is not a zip", "application/zip")},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "could not parse backup file"


def test_import_backup_refuses_another_format_version(
    client: TestClient, session: Session, seeded_user: User
) -> None:
    """A zip of any other ``format_version`` is a 422 naming the expected version; nothing
    is written."""
    response = client.post(
        "/api/v1/backup/import",
        files={
            "file": (
                "backup.zip",
                make_backup_zip(accounts=[account()], transactions=[txn()], format_version=1),
                "application/zip",
            )
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == (
        f"unsupported backup format version (expected {BACKUP_FORMAT_VERSION}); export a new backup"
    )
    assert session.scalar(select(func.count()).select_from(Transaction)) == 0
    assert session.scalar(select(func.count()).select_from(Account)) == 0


def test_import_backup_over_cap_is_413(client: TestClient, seeded_user: User) -> None:
    response = client.post(
        "/api/v1/backup/import",
        files={"file": ("backup.zip", b"\0" * (26 * 1024 * 1024), "application/zip")},
    )

    assert response.status_code == 413
    assert response.json()["detail"] == "file too large"
