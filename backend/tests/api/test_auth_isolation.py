"""Cross-user isolation (PRD §Users & access v2).

Every owned-table query already filters ``user_id`` (resolved from the access
cookie). These tests lock that in end-to-end across the owned tables: a second
user can neither list, read, nor address the first user's rows, and a body FK
that points at another user's row is rejected (422), never silently honoured.

Two idioms:
* **Register + cookie-swap** on ``unauth_client`` — register replaces the auth
  cookies, so clearing them and re-registering acts as a fresh second user.
* **Session-insert + cookie-swap** — for tables whose "other user" state is
  cheapest to seed directly in the DB (import batches, learned tags); the acting
  user is chosen by minting an access cookie for a given id via
  :func:`create_access_token`.
"""

from __future__ import annotations

import io
import uuid
import zipfile
from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import ACCESS_COOKIE_NAME, create_access_token
from app.models import (
    Account,
    Category,
    ImportBatch,
    MerchantTagMap,
    Transaction,
    User,
)
from app.parsers.backup_csv import ACCOUNTS_CSV, CATEGORIES_CSV, TRANSACTIONS_CSV
from app.services.fingerprint import transaction_fingerprint

# A fixed second user, distinct from the seeded v1 user, for the session-insert idiom.
_OTHER_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000099")


def _register(client: TestClient, email: str) -> None:
    r = client.post("/api/v1/auth/register", json={"email": email, "password": "passphrase-123"})
    assert r.status_code == 201, r.text


def _act_as(client: TestClient, user_id: uuid.UUID) -> None:
    """Point the client's access cookie at ``user_id`` (session-insert idiom)."""
    client.cookies.set(ACCESS_COOKIE_NAME, create_access_token(user_id))


def _make_account(client: TestClient, name: str) -> int:
    r = client.post(
        "/api/v1/accounts",
        json={"name": name, "type": "bank", "issuer": "hdfc", "opening_balance_paise": 0},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _make_instrument(client: TestClient, symbol: str = "INF-ISO-A") -> int:
    r = client.post(
        "/api/v1/instruments",
        json={
            "symbol": symbol,
            "name": "Iso Fund",
            "asset_class": "indian_mf",
            "currency": "INR",
            "exchange": "MFCentral",
            "current_nav": "150",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _make_investment_txn(client: TestClient, instrument_id: int) -> int:
    r = client.post(
        "/api/v1/investment-transactions",
        json={
            "date": "2026-01-10",
            "instrument_id": instrument_id,
            "transaction_type": "buy",
            "units": "10",
            "price_per_unit_native": "150",
            "amount_native_paise": 150000,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_accounts_are_isolated_per_user(unauth_client: TestClient) -> None:
    _register(unauth_client, "owner@example.com")
    a_account = _make_account(unauth_client, "Owner Bank")

    # Become a second user.
    unauth_client.cookies.clear()
    _register(unauth_client, "intruder@example.com")

    # B's account list excludes A's account.
    accounts = unauth_client.get("/api/v1/accounts").json()
    assert all(a["id"] != a_account for a in accounts)
    assert accounts == []  # B created none

    # B cannot address A's account by id (scoped 404, not 403 — no existence leak).
    patch = unauth_client.patch(f"/api/v1/accounts/{a_account}", json={"name": "hijacked"})
    assert patch.status_code == 404
    delete = unauth_client.delete(f"/api/v1/accounts/{a_account}")
    assert delete.status_code == 404


def test_transactions_are_isolated_per_user(unauth_client: TestClient) -> None:
    _register(unauth_client, "owner2@example.com")
    account = _make_account(unauth_client, "Owner Bank 2")
    r = unauth_client.post(
        "/api/v1/transactions",
        json={
            "date": "2026-01-15",
            "account_id": account,
            "amount_paise": -50000,
            "transaction_type": "spend",
            "merchant_raw": "Test Merchant",
            "labels": ["private"],  # F3a label — must not leak to B
        },
    )
    assert r.status_code == 201, r.text
    txn_id = r.json()["id"]

    unauth_client.cookies.clear()
    _register(unauth_client, "intruder2@example.com")

    assert unauth_client.get("/api/v1/transactions").json() == []
    # F3a label isolation: B sees none of A's labels, and the composite same-user
    # FK on transaction_labels makes a cross-user link structurally impossible.
    assert unauth_client.get("/api/v1/labels").json() == []
    assert (
        unauth_client.patch(f"/api/v1/transactions/{txn_id}", json={"labels": ["seen"]}).status_code
        == 404
    )
    assert unauth_client.delete(f"/api/v1/transactions/{txn_id}").status_code == 404


def test_instruments_are_isolated_per_user(unauth_client: TestClient) -> None:
    _register(unauth_client, "inst-owner@example.com")
    iid = _make_instrument(unauth_client)

    unauth_client.cookies.clear()
    _register(unauth_client, "inst-intruder@example.com")

    assert unauth_client.get("/api/v1/instruments").json() == []
    assert (
        unauth_client.patch(f"/api/v1/instruments/{iid}", json={"name": "hijacked"}).status_code
        == 404
    )
    assert unauth_client.delete(f"/api/v1/instruments/{iid}").status_code == 404

    # B has no instruments, so refresh-navs processes none of A's rows (and makes no
    # network call — the fetch is gated on the user's own instrument set).
    r = unauth_client.post("/api/v1/instruments/refresh-navs")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mf_updated"] == 0
    assert body["equity_updated"] == 0


def test_investment_transactions_are_isolated_per_user(unauth_client: TestClient) -> None:
    _register(unauth_client, "inv-owner@example.com")
    iid = _make_instrument(unauth_client)
    txn_id = _make_investment_txn(unauth_client, iid)

    unauth_client.cookies.clear()
    _register(unauth_client, "inv-intruder@example.com")

    assert unauth_client.get("/api/v1/investment-transactions").json() == []
    # B PATCH/DELETE A's investment-txn → scoped 404 (no existence leak).
    assert (
        unauth_client.patch(
            f"/api/v1/investment-transactions/{txn_id}", json={"note": "seen"}
        ).status_code
        == 404
    )
    assert unauth_client.delete(f"/api/v1/investment-transactions/{txn_id}").status_code == 404

    # Cross-owned-table FK injection: B cannot bind a new investment-txn to A's instrument.
    inject = unauth_client.post(
        "/api/v1/investment-transactions",
        json={
            "date": "2026-01-11",
            "instrument_id": iid,  # A's instrument
            "transaction_type": "buy",
            "units": "5",
            "price_per_unit_native": "150",
            "amount_native_paise": 75000,
        },
    )
    assert inject.status_code == 422, inject.text


def test_investment_read_models_are_isolated_per_user(unauth_client: TestClient) -> None:
    """A's positions never surface in B's holdings / portfolio rollup."""
    _register(unauth_client, "rollup-owner@example.com")
    iid = _make_instrument(unauth_client)
    _make_investment_txn(unauth_client, iid)

    unauth_client.cookies.clear()
    _register(unauth_client, "rollup-intruder@example.com")

    assert unauth_client.get("/api/v1/holdings").json() == {"holdings": []}
    summary = unauth_client.get("/api/v1/portfolio/summary").json()
    assert summary["holdings_count"] == 0
    assert summary["current_value_paise"] == 0
    assert summary["holding_xirr"] == []


def test_import_batch_commit_and_cancel_are_scoped(
    client: TestClient, seeded_user: User, session: Session
) -> None:
    """B (seeded_user) cannot commit or cancel A's import batch — both 404."""
    other = User(id=_OTHER_USER_ID)
    session.add(other)
    session.commit()
    other_account = Account(
        user_id=other.id, name="Other CC", type="credit_card", issuer="axis", last4="9999"
    )
    session.add(other_account)
    session.commit()
    batch = ImportBatch(
        user_id=other.id,
        account_id=other_account.id,
        source_file_hash="other-hash",
        parser_name="AxisCC",
        status="completed",
    )
    session.add(batch)
    session.commit()
    session.refresh(batch)

    # seeded_user acts here; the batch belongs to `other`.
    commit = client.post(f"/api/v1/imports/{batch.id}/commit", json={"transaction_ids": [1]})
    assert commit.status_code == 404, commit.text
    assert client.delete(f"/api/v1/imports/{batch.id}").status_code == 404


def test_backup_export_excludes_other_users_spend(
    client: TestClient, seeded_user: User, session: Session
) -> None:
    """A's confirmed spend never appears in B's backup export."""
    # A = seeded_user has a confirmed transaction.
    a_account = Account(
        user_id=seeded_user.id, name="Axis CC", type="credit_card", issuer="axis", last4="1234"
    )
    session.add(a_account)
    session.flush()
    session.add(
        Transaction(
            user_id=seeded_user.id,
            account_id=a_account.id,
            date=datetime(2026, 7, 1).date(),
            amount_paise=-50000,
            transaction_type="spend",
            merchant_raw="SWIGGY",
            merchant_normalized="swiggy",
            fingerprint=transaction_fingerprint(
                txn_date=datetime(2026, 7, 1).date(),
                amount_paise=-50000,
                normalized_merchant="swiggy",
                account_id=a_account.id,
            ),
            source="import",
            confirmed_at=datetime(2026, 7, 1, 10, 0, 0),
        )
    )
    other = User(id=_OTHER_USER_ID)
    session.add(other)
    session.commit()

    # Download as B — the zip must carry none of A's rows.
    _act_as(client, other.id)
    resp = client.get("/api/v1/backup")
    assert resp.status_code == 200, resp.text
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        txns = zf.read(TRANSACTIONS_CSV).decode()
        accounts = zf.read(ACCOUNTS_CSV).decode()
    assert "swiggy" not in txns.lower()
    assert "Axis CC" not in accounts


def test_backup_import_binds_to_current_user_on_name_collision(
    client: TestClient, seeded_user: User, session: Session
) -> None:
    """A name-collision backup import resolves within the acting user, never cross-user.

    Both A (seeded_user) and B own an "Axis CC" account. When B imports a backup whose
    transaction references "Axis CC", it must bind to B's account id — never A's — and
    leave A's data untouched.
    """
    a_account = Account(
        user_id=seeded_user.id, name="Axis CC", type="credit_card", issuer="axis", last4="1234"
    )
    other = User(id=_OTHER_USER_ID)
    session.add_all([a_account, other])
    session.commit()
    b_account = Account(
        user_id=other.id, name="Axis CC", type="credit_card", issuer="axis", last4="1234"
    )
    session.add(b_account)
    session.commit()
    session.refresh(a_account)
    session.refresh(b_account)

    _act_as(client, other.id)
    resp = client.post(
        "/api/v1/backup/import",
        files={"file": ("backup.zip", _minimal_backup_zip(), "application/zip")},
    )
    assert resp.status_code == 200, resp.text

    # B's imported transaction binds to B's account; A's account has none.
    b_txns = list(session.scalars(select(Transaction).where(Transaction.user_id == other.id)))
    assert b_txns, "expected the import to create a transaction for B"
    assert all(t.account_id == b_account.id for t in b_txns)
    a_txn = session.scalar(select(Transaction).where(Transaction.account_id == a_account.id))
    assert a_txn is None


def test_merchant_tags_do_not_leak_across_users(
    client: TestClient, seeded_user: User, session: Session
) -> None:
    """A's learned merchant→category rules never surface in B's tagging stats."""
    other = User(id=_OTHER_USER_ID)
    session.add(other)
    session.commit()
    other_category = Category(user_id=other.id, name="Food", kind="spend")
    session.add(other_category)
    session.commit()
    session.add(
        MerchantTagMap(
            user_id=other.id, merchant_normalized="swiggy", category_id=other_category.id
        )
    )
    session.commit()

    # seeded_user acts; the learned rule belongs to `other`.
    stats = client.get("/api/v1/dashboards/tagging-stats")
    assert stats.status_code == 200, stats.text
    assert stats.json()["rules_count"] == 0


def _minimal_backup_zip() -> bytes:
    """A one-account / one-category / one-transaction backup zip referencing 'Axis CC'."""
    accounts_header = "name,type,issuer,last4,opening_balance_paise,currency,archived_at"
    categories_header = "name,kind,color,archived_at"
    transactions_header = (
        "date,account_name,amount_paise,transaction_type,merchant_raw,"
        "merchant_normalized,category_name,category_kind,labels,source,confirmed_at,transfer_group"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(ACCOUNTS_CSV, f"{accounts_header}\nAxis CC,credit_card,axis,1234,-50000,INR,\n")
        zf.writestr(CATEGORIES_CSV, f"{categories_header}\nFood,spend,#4f46e5,\n")
        zf.writestr(
            TRANSACTIONS_CSV,
            f"{transactions_header}\n"
            "2026-07-01,Axis CC,-50000,spend,SWIGGY,swiggy,Food,spend,lunch,import,"
            "2026-07-01T10:00:00,\n",
        )
    return buffer.getvalue()
