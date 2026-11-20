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
from datetime import date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import clock
from app.core.fiscal_year import fy_start_year
from app.core.security import ACCESS_COOKIE_NAME, REFRESH_COOKIE_NAME, create_access_token
from app.models import (
    Account,
    Category,
    ImportBatch,
    InvestmentAccount,
    InvestmentTransaction,
    MerchantTagMap,
    PeerContact,
    PeerEntry,
    RealisedLot,
    Transaction,
    User,
)
from app.parsers.backup_csv import ACCOUNTS_CSV, TRANSACTIONS_CSV
from app.services import guest_service
from app.services.fingerprint import transaction_fingerprint
from tests.backup_zip import account, make_backup_zip, txn

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


def test_import_batch_commit_cancel_and_reconciliation_are_scoped(
    client: TestClient, seeded_user: User, session: Session
) -> None:
    """B (seeded_user) cannot commit, cancel, or read A's import batch's
    reconciliation — all three 404 via ``_get_batch_or_404``."""
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
    assert client.get(f"/api/v1/imports/{batch.id}/reconciliation").status_code == 404


def test_commit_discard_of_another_users_pending_row_422(
    client: TestClient,
    seeded_user: User,
    seeded_categories: list[Category],
    session: Session,
) -> None:
    """ADR-0003 rules 1 and 3: ``discard_ids`` is a list of body FKs. B naming A's pending row
    gets a 422 listing it, A's row survives and B's own commit row is not committed."""
    other = User(id=_OTHER_USER_ID)
    session.add(other)
    session.commit()

    def pending_row(user: User, name: str) -> tuple[int, int]:
        account = Account(
            user_id=user.id, name=name, type="credit_card", issuer="axis", last4="1234"
        )
        session.add(account)
        session.flush()
        batch = ImportBatch(
            user_id=user.id,
            account_id=account.id,
            source_file_hash=f"hash-{name}",
            parser_name="AxisCC",
            status="completed",
        )
        session.add(batch)
        session.flush()
        txn = Transaction(
            user_id=user.id,
            account_id=account.id,
            import_batch_id=batch.id,
            date=date(2026, 7, 1),
            amount_paise=-50000,
            transaction_type="spend",
            merchant_raw="SWIGGY",
            merchant_normalized="swiggy",
            fingerprint=transaction_fingerprint(
                txn_date=date(2026, 7, 1),
                amount_paise=-50000,
                normalized_merchant="swiggy",
                account_id=account.id,
            ),
            source="import",
        )
        session.add(txn)
        session.commit()
        return batch.id, txn.id

    _, a_row = pending_row(other, "A CC")
    b_batch, b_row = pending_row(seeded_user, "B CC")

    resp = client.post(
        f"/api/v1/imports/{b_batch}/commit",
        json={"transaction_ids": [b_row], "discard_ids": [a_row]},
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == {
        "message": "some transactions are not eligible to commit or discard",
        "invalid_ids": [a_row],
    }
    session.expire_all()
    assert session.get(Transaction, a_row) is not None
    assert session.get(Transaction, b_row).confirmed_at is None


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
    other_category = Category(color="#2a78d6", user_id=other.id, name="Food", kind="spend")
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


def test_category_parent_cannot_point_at_another_users_category(
    seeded_user: User, session: Session
) -> None:
    """ADR-0003 rule 10 / ADR-0012 rule 1: the DB itself refuses a cross-user parent.

    The router's 422 (rule 9) is skipped here on purpose: the composite same-user FK
    ``(parent_id, user_id) → categories(id, user_id)`` must reject the row on its own.
    """
    other = User(id=_OTHER_USER_ID)
    session.add(other)
    session.commit()
    their_parent = Category(color="#2a78d6", user_id=other.id, name="Theirs", kind="spend")
    session.add(their_parent)
    session.commit()

    session.add(
        Category(user_id=seeded_user.id, name="Mine", kind="spend", parent_id=their_parent.id)
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def _sell_all(client: TestClient, instrument_id: int) -> int:
    r = client.post(
        "/api/v1/investment-transactions",
        json={
            "date": "2026-02-10",
            "instrument_id": instrument_id,
            "transaction_type": "sell",
            "units": "10",
            "price_per_unit_native": "160",
            "amount_native_paise": 160000,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_realised_lots_are_isolated_per_user(unauth_client: TestClient, session: Session) -> None:
    """ADR-0003 rule 11 for ``realised_lots`` (ADR-0015 rule 5): B's investment writes and
    rebuilds never touch A's rows, B cannot address A's sell, and the DB itself refuses a
    row whose txns belong to someone else (composite same-user FK)."""
    _register(unauth_client, "lots-owner@example.com")
    a_iid = _make_instrument(unauth_client)
    a_buy = _make_investment_txn(unauth_client, a_iid)
    a_sell = _sell_all(unauth_client, a_iid)
    a_user = session.scalars(select(User).where(User.email == "lots-owner@example.com")).one()
    a_rows = [
        (r.id, r.user_id, r.sell_txn_id, r.buy_txn_id)
        for r in session.scalars(select(RealisedLot).order_by(RealisedLot.id))
    ]
    assert a_rows == [(a_rows[0][0], a_user.id, a_sell, a_buy)]

    unauth_client.cookies.clear()
    _register(unauth_client, "lots-intruder@example.com")
    b_iid = _make_instrument(unauth_client, symbol="INF-ISO-B")
    b_buy = _make_investment_txn(unauth_client, b_iid)
    _sell_all(unauth_client, b_iid)
    # B cannot reach A's sell; B's own delete runs B's rebuild (and is refused).
    assert unauth_client.delete(f"/api/v1/investment-transactions/{a_sell}").status_code == 404
    assert (
        unauth_client.delete(f"/api/v1/investment-transactions/{b_buy}").status_code == 422
    )  # uncovers B's own sell; refused, still scoped to B

    session.expire_all()
    mine = list(session.scalars(select(RealisedLot).where(RealisedLot.user_id == a_user.id)))
    assert [(r.id, r.user_id, r.sell_txn_id, r.buy_txn_id) for r in mine] == a_rows
    for r in session.scalars(select(RealisedLot)):
        owners = {
            t.user_id
            for t in session.scalars(
                select(InvestmentTransaction).where(
                    InvestmentTransaction.id.in_([r.sell_txn_id, r.buy_txn_id])
                )
            )
        }
        assert owners == {r.user_id}

    b_user = session.scalars(select(User).where(User.email == "lots-intruder@example.com")).one()
    b_acc = session.scalars(
        select(InvestmentAccount).where(InvestmentAccount.user_id == b_user.id)
    ).first()
    assert b_acc is not None
    session.add(
        RealisedLot(
            user_id=b_user.id,
            account_id=b_acc.id,
            sell_txn_id=a_sell,
            buy_txn_id=a_buy,
            units=Decimal("1"),
            cost_native_paise=1,
            proceeds_native_paise=1,
            sell_fees_native_paise=0,
            buy_date=date(2026, 1, 10),
            sell_date=date(2026, 2, 10),
            kind="fifo",
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_merchant_aliases_are_isolated_per_user(unauth_client: TestClient) -> None:
    """A new owned table (ADR-0011 merchant-alias layer, Phase A4): B can
    neither list A's aliases nor address one of A's alias ids (scoped 404,
    never 403 -- no existence leak).

    Pattern and canonical stay clear of the seed dictionary so this test checks isolation
    only, not ADR-0011 rule 5's conflict rules.
    """
    _register(unauth_client, "alias-owner@example.com")
    create = unauth_client.post(
        "/api/v1/rules/aliases",
        json={"pattern": "localkirana998877", "canonical": "local kirana store"},
    )
    assert create.status_code == 201, create.text
    alias_id = create.json()["id"]

    # Become a second user.
    unauth_client.cookies.clear()
    _register(unauth_client, "alias-intruder@example.com")

    # B's own registration seeds ~96 is_seeded=True aliases (Phase A5) — the list is not
    # empty by design. Isolation means B has none of A's USER-AUTHORED ones: no alias at
    # B's own pattern, and no non-seeded row at all (B never created one).
    b_aliases = unauth_client.get("/api/v1/rules/aliases").json()
    assert not any(a["pattern"] == "localkirana998877" for a in b_aliases)
    assert not any(a["is_seeded"] is False for a in b_aliases)
    assert (
        unauth_client.patch(
            f"/api/v1/rules/aliases/{alias_id}", json={"canonical": "hijacked"}
        ).status_code
        == 404
    )
    assert unauth_client.delete(f"/api/v1/rules/aliases/{alias_id}").status_code == 404


def test_categories_are_isolated_per_user(unauth_client: TestClient) -> None:
    """ADR-0003 rules 8 and 9 for ``categories``: B cannot list, address or parent
    under A's custom category."""
    _register(unauth_client, "cat-owner@example.com")
    create = unauth_client.post("/api/v1/categories", json={"name": "Iso Hobbies"})
    assert create.status_code == 201, create.text
    a_category = create.json()["id"]

    unauth_client.cookies.clear()
    _register(unauth_client, "cat-intruder@example.com")

    b_categories = unauth_client.get("/api/v1/categories").json()
    assert all(c["id"] != a_category for c in b_categories)
    assert not any(c["name"] == "Iso Hobbies" for c in b_categories)
    patch = unauth_client.patch(f"/api/v1/categories/{a_category}", json={"name": "hijacked"})
    assert patch.status_code == 404
    assert unauth_client.delete(f"/api/v1/categories/{a_category}").status_code == 404
    child = unauth_client.post(
        "/api/v1/categories", json={"name": "Under Theirs", "parent_id": a_category}
    )
    assert child.status_code == 422


def test_labels_are_isolated_per_user(unauth_client: TestClient) -> None:
    """ADR-0003 rule 8 for ``labels``."""
    _register(unauth_client, "label-owner@example.com")
    create = unauth_client.post("/api/v1/labels", json={"name": "iso-trip"})
    assert create.status_code == 201, create.text
    a_label = create.json()["id"]

    unauth_client.cookies.clear()
    _register(unauth_client, "label-intruder@example.com")

    assert all(lbl["id"] != a_label for lbl in unauth_client.get("/api/v1/labels").json())
    patch = unauth_client.patch(f"/api/v1/labels/{a_label}", json={"name": "hijacked"})
    assert patch.status_code == 404
    assert unauth_client.delete(f"/api/v1/labels/{a_label}").status_code == 404


def test_merchant_label_rules_are_isolated_per_user(unauth_client: TestClient) -> None:
    """ADR-0003 rule 8 for ``merchant_label_map`` (the pinned merchant→label rules)."""
    _register(unauth_client, "rule-owner@example.com")
    label = unauth_client.post("/api/v1/labels", json={"name": "iso-rule"})
    assert label.status_code == 201, label.text
    create = unauth_client.post(
        "/api/v1/rules/labels",
        json={"merchant": "isokirana445566", "label_id": label.json()["id"]},
    )
    assert create.status_code == 201, create.text
    rule_id = create.json()["id"]
    merchant = create.json()["merchant_normalized"]

    unauth_client.cookies.clear()
    _register(unauth_client, "rule-intruder@example.com")

    b_rules = unauth_client.get("/api/v1/rules").json()
    assert not any(r["merchant_canonical"] == merchant for r in b_rules)
    assert not any(lr["id"] == rule_id for r in b_rules for lr in r["labels"])
    patch = unauth_client.patch(f"/api/v1/rules/labels/{rule_id}", json={"pinned": False})
    assert patch.status_code == 404
    assert unauth_client.delete(f"/api/v1/rules/labels/{rule_id}").status_code == 404


def test_refresh_sessions_are_isolated_per_user(unauth_client: TestClient) -> None:
    """ADR-0003 rule 7 for ``sessions``: B changing their password and logging out
    revokes only B's refresh families, never A's."""
    _register(unauth_client, "session-owner@example.com")
    a_refresh = unauth_client.cookies.get(REFRESH_COOKIE_NAME)
    assert a_refresh is not None

    unauth_client.cookies.clear()
    _register(unauth_client, "session-intruder@example.com")
    changed = unauth_client.post(
        "/api/v1/auth/change-password",
        json={"current_password": "passphrase-123", "new_password": "another-passphrase"},
    )
    assert changed.status_code == 200, changed.text
    assert unauth_client.post("/api/v1/auth/logout").status_code == 204

    unauth_client.cookies.clear()
    unauth_client.cookies.set(REFRESH_COOKIE_NAME, a_refresh)
    assert unauth_client.post("/api/v1/auth/refresh").status_code == 200


def test_guest_sandbox_is_isolated_from_real_users(
    unauth_client: TestClient, session: Session
) -> None:
    """ADR-0003 rule 15: a guest is just another tenant — it cannot see or address a
    real user's rows, and the real user does not see the guest's seeded data."""
    _register(unauth_client, "guest-neighbour@example.com")
    a_account = _make_account(unauth_client, "Neighbour Bank")
    a_cookie = unauth_client.cookies.get(ACCESS_COOKIE_NAME)
    assert a_cookie is not None

    guest, _ = guest_service.create_guest_sandbox(session)
    guest_accounts = set(session.scalars(select(Account.id).where(Account.user_id == guest.id)))
    assert guest_accounts

    unauth_client.cookies.clear()
    _act_as(unauth_client, guest.id)
    seen_by_guest = {a["id"] for a in unauth_client.get("/api/v1/accounts").json()}
    assert a_account not in seen_by_guest
    assert seen_by_guest == guest_accounts
    patch = unauth_client.patch(f"/api/v1/accounts/{a_account}", json={"name": "hijacked"})
    assert patch.status_code == 404

    unauth_client.cookies.clear()
    unauth_client.cookies.set(ACCESS_COOKIE_NAME, a_cookie)
    seen_by_a = {a["id"] for a in unauth_client.get("/api/v1/accounts").json()}
    assert seen_by_a == {a_account}


def _minimal_backup_zip() -> bytes:
    """A one-account / one-category / one-transaction backup zip referencing 'Axis CC'."""
    return make_backup_zip(
        accounts=[account(opening_balance_paise=-50000)],
        categories=[{"name": "Food", "kind": "spend", "color": "#4f46e5"}],
        transactions=[txn(category_name="Food", category_kind="spend", labels="lunch")],
    )


def _make_spend(client: TestClient, account: int, merchant: str) -> int:
    r = client.post(
        "/api/v1/transactions",
        json={
            "date": "2026-01-15",
            "account_id": account,
            "amount_paise": -50000,
            "transaction_type": "spend",
            "merchant_raw": merchant,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_bulk_categorise_rejects_foreign_rows_and_category(unauth_client: TestClient) -> None:
    """ADR-0003 rules 8 and 9 for the bulk path: another user's ids are ``invalid_ids``
    (never a 404/200 existence oracle), another user's category is a 422, and neither
    writes anything."""
    _register(unauth_client, "bulk-owner@example.com")
    a_txn = _make_spend(unauth_client, _make_account(unauth_client, "Bulk A"), "SWIGGY")
    a_category = unauth_client.post("/api/v1/categories", json={"name": "Bulk Iso"}).json()["id"]

    unauth_client.cookies.clear()
    _register(unauth_client, "bulk-intruder@example.com")
    b_txn = _make_spend(unauth_client, _make_account(unauth_client, "Bulk B"), "SWIGGY")
    b_category = unauth_client.post("/api/v1/categories", json={"name": "Bulk Mine"}).json()["id"]

    foreign_row = unauth_client.post(
        "/api/v1/transactions/bulk-categorise",
        json={"transaction_ids": [b_txn, a_txn], "category_id": b_category},
    )
    assert foreign_row.status_code == 422
    assert foreign_row.json()["detail"]["invalid_ids"] == [a_txn]

    foreign_category = unauth_client.post(
        "/api/v1/transactions/bulk-categorise",
        json={"transaction_ids": [b_txn], "category_id": a_category},
    )
    assert foreign_category.status_code == 422

    mine = unauth_client.get("/api/v1/transactions").json()
    assert [t["category_id"] for t in mine] == [None]
    assert unauth_client.get("/api/v1/dashboards/tagging-stats").json()["rules_count"] == 0


def test_bulk_delete_rejects_foreign_rows(unauth_client: TestClient) -> None:
    """ADR-0003 rules 8 and 9 for the bulk path: another user's ids are ``invalid_ids``
    (never a 404/204 existence oracle), and nothing is deleted on either side."""
    _register(unauth_client, "bulkdel-owner@example.com")
    a_user = uuid.UUID(unauth_client.get("/api/v1/auth/me").json()["id"])
    a_txn = _make_spend(unauth_client, _make_account(unauth_client, "BulkDel A"), "SWIGGY")

    unauth_client.cookies.clear()
    _register(unauth_client, "bulkdel-intruder@example.com")
    b_txn = _make_spend(unauth_client, _make_account(unauth_client, "BulkDel B"), "SWIGGY")

    resp = unauth_client.post(
        "/api/v1/transactions/bulk-delete", json={"transaction_ids": [b_txn, a_txn]}
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["invalid_ids"] == [a_txn]
    assert [t["id"] for t in unauth_client.get("/api/v1/transactions").json()] == [b_txn]

    unauth_client.cookies.clear()
    _act_as(unauth_client, a_user)
    assert [t["id"] for t in unauth_client.get("/api/v1/transactions").json()] == [a_txn]


def test_past_rows_foreign_source_404(unauth_client: TestClient) -> None:
    """ADR-0003 rule 8: another user's transaction id is a 404 on the past-rows preview."""
    _register(unauth_client, "past-owner@example.com")
    a_txn = _make_spend(unauth_client, _make_account(unauth_client, "Past A"), "SWIGGY")

    unauth_client.cookies.clear()
    _register(unauth_client, "past-intruder@example.com")

    assert unauth_client.get(f"/api/v1/transactions/{a_txn}/past-rows").status_code == 404


def test_link_foreign_source_404(unauth_client: TestClient) -> None:
    """ADR-0003 rule 8: another user's transaction id is a 404 on the manual link."""
    _register(unauth_client, "link-owner@example.com")
    a_txn = _make_spend(unauth_client, _make_account(unauth_client, "Link A"), "SWIGGY")

    unauth_client.cookies.clear()
    _register(unauth_client, "link-intruder@example.com")
    b_txn = _make_spend(unauth_client, _make_account(unauth_client, "Link B"), "ZOMATO")

    resp = unauth_client.post(f"/api/v1/transactions/{a_txn}/link", json={"partner_id": b_txn})

    assert resp.status_code == 404


def test_link_foreign_partner_422(unauth_client: TestClient) -> None:
    """ADR-0003 rule 9: the user's own row linked to another user's row is a 422 and
    nothing is written, even when the amounts are equal and opposite."""
    _register(unauth_client, "linkp-owner@example.com")
    a_account = _make_account(unauth_client, "Linkp A")
    a_income = unauth_client.post(
        "/api/v1/transactions",
        json={
            "date": "2026-01-15",
            "account_id": a_account,
            "amount_paise": 50000,
            "transaction_type": "income",
            "merchant_raw": "REFUND",
        },
    ).json()["id"]

    unauth_client.cookies.clear()
    _register(unauth_client, "linkp-intruder@example.com")
    b_txn = _make_spend(unauth_client, _make_account(unauth_client, "Linkp B"), "ZOMATO")

    resp = unauth_client.post(f"/api/v1/transactions/{b_txn}/link", json={"partner_id": a_income})

    assert resp.status_code == 422
    mine = unauth_client.get("/api/v1/transactions").json()
    assert [(t["transaction_type"], t["transfer_pair_id"]) for t in mine] == [("spend", None)]


def test_link_candidates_foreign_source_404(unauth_client: TestClient) -> None:
    """ADR-0003 rule 8: another user's transaction id is a 404 on link-candidates."""
    _register(unauth_client, "lcand-owner@example.com")
    a_txn = _make_spend(unauth_client, _make_account(unauth_client, "Lcand A"), "SWIGGY")

    unauth_client.cookies.clear()
    _register(unauth_client, "lcand-intruder@example.com")

    assert unauth_client.get(f"/api/v1/transactions/{a_txn}/link-candidates").status_code == 404


def test_refund_candidates_foreign_source_404(unauth_client: TestClient) -> None:
    """ADR-0003 rule 8: another user's transaction id is a 404 on refund-candidates."""
    _register(unauth_client, "rcand-owner@example.com")
    a_txn = _make_spend(unauth_client, _make_account(unauth_client, "Rcand A"), "SWIGGY")

    unauth_client.cookies.clear()
    _register(unauth_client, "rcand-intruder@example.com")

    assert unauth_client.get(f"/api/v1/transactions/{a_txn}/refund-candidates").status_code == 404


def test_sample_import_into_a_foreign_account_422(unauth_client: TestClient) -> None:
    """ADR-0003 rule 9: a sample imported into another user's account is a 422 (a body FK
    naming another user's row), and stages nothing there."""
    _register(unauth_client, "sample-owner@example.com")
    r = unauth_client.post(
        "/api/v1/accounts",
        json={"name": "Axis A", "type": "credit_card", "issuer": "axis"},
    )
    assert r.status_code == 201, r.text
    a_account = r.json()["id"]

    unauth_client.cookies.clear()
    _register(unauth_client, "sample-intruder@example.com")

    resp = unauth_client.post(
        "/api/v1/imports/samples", json={"sample_id": "axis-cc-2025-06", "account_id": a_account}
    )

    assert resp.status_code == 422
    assert resp.json()["detail"] == "account not found or archived"
    assert unauth_client.get("/api/v1/imports/pending").json() == []


def test_statement_upload_into_a_foreign_account_422(unauth_client: TestClient) -> None:
    """ADR-0003 rule 9: a statement uploaded into another user's account is a 422."""
    _register(unauth_client, "upload-owner@example.com")
    r = unauth_client.post(
        "/api/v1/accounts",
        json={"name": "Axis A", "type": "credit_card", "issuer": "axis"},
    )
    assert r.status_code == 201, r.text
    a_account = r.json()["id"]

    unauth_client.cookies.clear()
    _register(unauth_client, "upload-intruder@example.com")

    resp = unauth_client.post(
        "/api/v1/imports",
        data={"account_id": str(a_account)},
        files={"file": ("s.pdf", b"x", "application/pdf")},
    )

    assert resp.status_code == 422
    assert resp.json()["detail"] == "account not found or archived"


def test_archive_reassign_to_foreign_category_422(unauth_client: TestClient) -> None:
    """ADR-0003 rule 9: a ``reassign_to`` pointing at another user's category is a 422,
    and the category is not archived."""
    _register(unauth_client, "reassign-owner@example.com")
    a_category = unauth_client.post("/api/v1/categories", json={"name": "Reassign Iso"}).json()[
        "id"
    ]

    unauth_client.cookies.clear()
    _register(unauth_client, "reassign-intruder@example.com")
    mine = unauth_client.post("/api/v1/categories", json={"name": "Reassign Mine"}).json()["id"]

    resp = unauth_client.delete(f"/api/v1/categories/{mine}?reassign_to={a_category}")

    assert resp.status_code == 422
    names = {c["name"] for c in unauth_client.get("/api/v1/categories").json()}
    assert "Reassign Mine" in names


def test_tax_statement_is_isolated_per_user(unauth_client: TestClient) -> None:
    """A's intraday trades and realised-sale years never surface in B's F11 statement."""
    _register(unauth_client, "tax-owner@example.com")
    r = unauth_client.post(
        "/api/v1/instruments",
        json={
            "symbol": "ISO-EQ",
            "name": "Iso Equity",
            "asset_class": "indian_equity",
            "currency": "INR",
            "exchange": "NSE",
            "current_nav": "110",
        },
    )
    assert r.status_code == 201, r.text
    iid = r.json()["id"]
    for kind, amount in (("buy", 100000), ("sell", 110000)):
        t = unauth_client.post(
            "/api/v1/investment-transactions",
            json={
                "date": "2026-03-03",
                "instrument_id": iid,
                "transaction_type": kind,
                "units": "10",
                "price_per_unit_native": "100" if kind == "buy" else "110",
                "amount_native_paise": amount,
            },
        )
        assert t.status_code == 201, t.text
    owner_view = unauth_client.get("/api/v1/tax/intraday-trades?year=2025").json()
    assert len(owner_view["trades"]) == 1

    unauth_client.cookies.clear()
    _register(unauth_client, "tax-intruder@example.com")
    other_view = unauth_client.get("/api/v1/tax/intraday-trades?year=2025").json()
    assert other_view["trades"] == []
    assert other_view["total_profit_paise"] == 0
    years = unauth_client.get("/api/v1/tax/available-years").json()["years"]
    assert years == [fy_start_year(clock.today())]


def test_peer_ledger_is_isolated_per_user(unauth_client: TestClient, session: Session) -> None:
    """ADR-0017 Phase 1: PeerContact and PeerEntry cross-tenant isolation.

    A user can only link entries to their own contacts and transactions.
    Cross-user foreign links are rejected by the composite same-user FKs.
    """
    _register(unauth_client, "peer-owner@example.com")
    a_user = session.scalars(select(User).where(User.email == "peer-owner@example.com")).one()
    a_acct = _make_account(unauth_client, "Owner Bank")
    r_tx = unauth_client.post(
        "/api/v1/transactions",
        json={
            "account_id": a_acct,
            "date": "2026-03-01",
            "amount_paise": -500000,
            "transaction_type": "spend",
            "merchant_raw": "Dinner with friend",
        },
    )
    assert r_tx.status_code == 201, r_tx.text
    a_txn_id = r_tx.json()["id"]

    a_contact = PeerContact(user_id=a_user.id, name="Bob")
    session.add(a_contact)
    session.flush()

    a_entry = PeerEntry(
        user_id=a_user.id,
        contact_id=a_contact.id,
        transaction_id=a_txn_id,
        date=date(2026, 3, 1),
        amount_paise=250000,
        direction="you_gave",
    )
    session.add(a_entry)
    session.commit()

    # Switch to User B
    unauth_client.cookies.clear()
    _register(unauth_client, "peer-intruder@example.com")
    b_user = session.scalars(select(User).where(User.email == "peer-intruder@example.com")).one()
    b_acct = _make_account(unauth_client, "Intruder Bank")
    r_btx = unauth_client.post(
        "/api/v1/transactions",
        json={
            "account_id": b_acct,
            "date": "2026-03-01",
            "amount_paise": -100000,
            "transaction_type": "spend",
            "merchant_raw": "Coffee",
        },
    )
    assert r_btx.status_code == 201, r_btx.text
    b_txn_id = r_btx.json()["id"]

    b_contact = PeerContact(user_id=b_user.id, name="Bob")
    session.add(b_contact)
    session.commit()

    # B cannot link an entry to A's contact (rejected by composite FK)
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            PeerEntry(
                user_id=b_user.id,
                contact_id=a_contact.id,
                date=date(2026, 3, 1),
                amount_paise=10000,
                direction="you_gave",
            )
        )
        session.flush()

    # B cannot link an entry to A's transaction (rejected by composite FK)
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            PeerEntry(
                user_id=b_user.id,
                contact_id=b_contact.id,
                transaction_id=a_txn_id,
                date=date(2026, 3, 1),
                amount_paise=10000,
                direction="you_gave",
            )
        )
        session.flush()

    # B can link their own transaction to their own contact
    b_entry = PeerEntry(
        user_id=b_user.id,
        contact_id=b_contact.id,
        transaction_id=b_txn_id,
        date=date(2026, 3, 1),
        amount_paise=10000,
        direction="you_gave",
    )
    session.add(b_entry)
    session.commit()

    # B scoped query sees only B's contacts/entries
    b_contacts = list(session.scalars(select(PeerContact).where(PeerContact.user_id == b_user.id)))
    assert [c.id for c in b_contacts] == [b_contact.id]
    b_entries = list(session.scalars(select(PeerEntry).where(PeerEntry.user_id == b_user.id)))
    assert [e.id for e in b_entries] == [b_entry.id]


def test_investment_accounts_are_isolated_per_user(unauth_client: TestClient) -> None:
    """ADR-0003 & ADR-0019: Investment accounts are strictly isolated.

    User B cannot list or get User A's account, and passing User A's account_id
    to an investment transaction returns 422.
    """
    _register(unauth_client, "inv-acc-owner@example.com")
    a_acc_res = unauth_client.post(
        "/api/v1/investment-accounts",
        json={
            "name": "User A Demat",
            "account_type": "demat",
            "provider": "zerodha",
            "external_account_ref": "acc_a_demat",
            "currency": "INR",
        },
    )
    assert a_acc_res.status_code == 201
    a_acc_id = a_acc_res.json()["id"]

    unauth_client.cookies.clear()
    _register(unauth_client, "inv-acc-intruder@example.com")

    # B cannot list A's account
    assert unauth_client.get("/api/v1/investment-accounts").json() == []

    # B cannot fetch A's account (404, never 403)
    assert unauth_client.get(f"/api/v1/investment-accounts/{a_acc_id}").status_code == 404

    # B cannot link a transaction to A's account_id (ADR-0003 rule 3 -> 422)
    b_inst_res = unauth_client.post(
        "/api/v1/instruments",
        json={
            "symbol": "INFY-B",
            "name": "Infosys",
            "asset_class": "indian_equity",
            "currency": "INR",
            "exchange": "NSE",
        },
    )
    assert b_inst_res.status_code == 201
    b_inst_id = b_inst_res.json()["id"]

    txn_res = unauth_client.post(
        "/api/v1/investment-transactions",
        json={
            "instrument_id": b_inst_id,
            "account_id": a_acc_id,
            "date": "2026-06-01",
            "transaction_type": "buy",
            "units": 10,
            "price_per_unit_native": 1500,
            "amount_native_paise": 1500000,
        },
    )
    assert txn_res.status_code == 422
    assert "investment account not found or inactive" in txn_res.json()["detail"]
