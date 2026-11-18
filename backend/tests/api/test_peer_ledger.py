"""API tests for Friends & Khata / Peer Ledger (PRD §F3b, ADR-0017)."""

from __future__ import annotations

import uuid
from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import ACCESS_COOKIE_NAME, create_access_token
from app.models import Account, Category, Transaction, User
from app.services.category_service import CASHBACK_CATEGORY_NAME
from tests.api.conftest import _demo_password_hash


def _act_as(client: TestClient, user_id: uuid.UUID) -> None:
    client.cookies.set(ACCESS_COOKIE_NAME, create_access_token(user_id))


def _make_card_txn(session: Session, user: User, amount_paise: int = -500000) -> Transaction:
    account = Account(
        user_id=user.id,
        name="Credit Card",
        type="credit_card",
        issuer="hdfc",
        last4="4321",
    )
    session.add(account)
    session.flush()

    txn = Transaction(
        user_id=user.id,
        account_id=account.id,
        date=date(2026, 3, 1),
        amount_paise=amount_paise,
        transaction_type="spend",
        merchant_raw="Amazon Retail",
        merchant_normalized="amazon retail",
        fingerprint=uuid.uuid4().hex * 2,
        source="manual",
    )
    session.add(txn)
    session.commit()
    session.refresh(txn)
    return txn


def test_contact_full_crud_cycle(client: TestClient) -> None:
    """Full lifecycle: create, list, get, update, soft-archive."""
    # 1. Create contact
    r_create = client.post(
        "/api/v1/friends/contacts",
        json={
            "name": "Alice Smith",
            "phone": "+919876543210",
            "upi_id": "alice@upi",
            "notes": "Colleague",
        },
    )
    assert r_create.status_code == 201, r_create.text
    contact = r_create.json()
    contact_id = contact["id"]
    assert contact["name"] == "Alice Smith"
    assert contact["phone"] == "+919876543210"
    assert contact["upi_id"] == "alice@upi"
    assert contact["archived_at"] is None

    # 2. List contacts
    r_list = client.get("/api/v1/friends/contacts")
    assert r_list.status_code == 200
    items = r_list.json()
    assert any(c["id"] == contact_id for c in items)

    # 3. Get single contact with balance
    r_get = client.get(f"/api/v1/friends/contacts/{contact_id}")
    assert r_get.status_code == 200
    single = r_get.json()
    assert single["id"] == contact_id
    assert single["balance_paise"] == 0
    assert single["total_gave_paise"] == 0
    assert single["total_got_paise"] == 0
    assert single["active_entries_count"] == 0

    # 4. Patch contact
    r_patch = client.patch(
        f"/api/v1/friends/contacts/{contact_id}",
        json={"name": "Alice S.", "notes": "Close friend"},
    )
    assert r_patch.status_code == 200
    patched = r_patch.json()
    assert patched["name"] == "Alice S."
    assert patched["notes"] == "Close friend"

    # 5. Soft-archive contact with 0 balance
    r_del = client.delete(f"/api/v1/friends/contacts/{contact_id}")
    assert r_del.status_code == 200
    archived = r_del.json()
    assert archived["archived_at"] is not None

    # Verify excluded by default from list
    r_active = client.get("/api/v1/friends/contacts")
    assert not any(c["id"] == contact_id for c in r_active.json())

    # Included when include_archived=True
    r_all = client.get("/api/v1/friends/contacts?include_archived=true")
    assert any(c["id"] == contact_id for c in r_all.json())


def test_contact_name_collision(client: TestClient) -> None:
    """Duplicate active contact name returns 409 conflict."""
    r1 = client.post("/api/v1/friends/contacts", json={"name": "Bob Builder"})
    assert r1.status_code == 201

    r2 = client.post("/api/v1/friends/contacts", json={"name": "  bob builder  "})
    assert r2.status_code == 409
    assert "already exists" in r2.json()["detail"]


def test_entry_crud_and_chronological_listing(client: TestClient) -> None:
    """Create entries, list chronologically, and delete an entry."""
    r_c = client.post("/api/v1/friends/contacts", json={"name": "Charlie"})
    contact_id = r_c.json()["id"]

    # Entry 1: 2026-03-05 (User gave 3000 paise)
    r_e1 = client.post(
        "/api/v1/friends/entries",
        json={
            "contact_id": contact_id,
            "date": "2026-03-05",
            "amount_paise": 3000,
            "direction": "you_gave",
            "notes": "Lunch",
        },
    )
    assert r_e1.status_code == 201
    e1_id = r_e1.json()["id"]

    # Entry 2: 2026-03-02 (User gave 2000 paise - earlier date)
    r_e2 = client.post(
        "/api/v1/friends/entries",
        json={
            "contact_id": contact_id,
            "date": "2026-03-02",
            "amount_paise": 2000,
            "direction": "you_gave",
            "notes": "Coffee",
        },
    )
    assert r_e2.status_code == 201
    e2_id = r_e2.json()["id"]

    # Get entries - must be chronologically ordered (e2 on 03-02 before e1 on 03-05)
    r_entries = client.get(f"/api/v1/friends/contacts/{contact_id}/entries")
    assert r_entries.status_code == 200
    entries = r_entries.json()
    assert len(entries) == 2
    assert entries[0]["id"] == e2_id
    assert entries[1]["id"] == e1_id

    # Check contact balance: 3000 + 2000 = 5000
    r_bal = client.get(f"/api/v1/friends/contacts/{contact_id}")
    assert r_bal.json()["balance_paise"] == 5000

    # Delete entry 1
    r_del = client.delete(f"/api/v1/friends/entries/{e1_id}")
    assert r_del.status_code == 204

    # Check updated balance: 2000
    r_bal_after = client.get(f"/api/v1/friends/contacts/{contact_id}")
    assert r_bal_after.json()["balance_paise"] == 2000


def test_settle_contact_flow(client: TestClient) -> None:
    """Full settlement endpoint zeroes the outstanding balance."""
    r_c = client.post("/api/v1/friends/contacts", json={"name": "Diana"})
    contact_id = r_c.json()["id"]

    # Create positive balance (Diana owes user 10000 paise)
    client.post(
        "/api/v1/friends/entries",
        json={
            "contact_id": contact_id,
            "date": "2026-03-01",
            "amount_paise": 10000,
            "direction": "you_gave",
        },
    )

    # Settle
    r_settle = client.post(
        f"/api/v1/friends/contacts/{contact_id}/settle",
        json={"notes": "Paid back in full"},
    )
    assert r_settle.status_code == 200
    settlement = r_settle.json()
    assert settlement["direction"] == "you_got"
    assert settlement["amount_paise"] == 10000
    assert settlement["is_settlement"] is True
    assert settlement["notes"] == "Paid back in full"

    # Contact balance is now 0
    r_bal = client.get(f"/api/v1/friends/contacts/{contact_id}")
    assert r_bal.json()["balance_paise"] == 0

    # Settle again when balance is 0 -> 400
    r_settle2 = client.post(f"/api/v1/friends/contacts/{contact_id}/settle", json={})
    assert r_settle2.status_code == 400
    assert "already settled" in r_settle2.json()["detail"]


def test_archive_contact_with_balance_confirmation(client: TestClient) -> None:
    """Archiving contact with balance requires confirm_with_balance=True."""
    r_c = client.post("/api/v1/friends/contacts", json={"name": "Evan"})
    contact_id = r_c.json()["id"]

    client.post(
        "/api/v1/friends/entries",
        json={
            "contact_id": contact_id,
            "date": "2026-03-01",
            "amount_paise": 7500,
            "direction": "you_gave",
        },
    )

    # DELETE without confirm flag fails 400
    r_del_fail = client.delete(f"/api/v1/friends/contacts/{contact_id}")
    assert r_del_fail.status_code == 400
    assert "Confirmation required" in r_del_fail.json()["detail"]

    # DELETE with confirm flag succeeds
    r_del_ok = client.delete(f"/api/v1/friends/contacts/{contact_id}?confirm_with_balance=true")
    assert r_del_ok.status_code == 200
    assert r_del_ok.json()["archived_at"] is not None


def test_upfront_discount_cashback_math(client: TestClient) -> None:
    """Upfront discount helper validates amount and auto-formats notes."""
    r_c = client.post("/api/v1/friends/contacts", json={"name": "Fiona"})
    contact_id = r_c.json()["id"]

    # Mismatched amount: original 10000 - discount 500 != amount 9000 -> 422
    r_bad = client.post(
        "/api/v1/friends/entries",
        json={
            "contact_id": contact_id,
            "date": "2026-03-01",
            "original_paise": 10000,
            "discount_paise": 500,
            "amount_paise": 9000,
            "direction": "you_gave",
        },
    )
    assert r_bad.status_code == 422

    # Matched amount without notes: auto-generates notes
    r_good = client.post(
        "/api/v1/friends/entries",
        json={
            "contact_id": contact_id,
            "date": "2026-03-01",
            "original_paise": 10000,
            "discount_paise": 500,
            "amount_paise": 9500,
            "direction": "you_gave",
        },
    )
    assert r_good.status_code == 201
    entry = r_good.json()
    assert entry["amount_paise"] == 9500
    assert "Original: ₹100 less ₹5 discount" in entry["notes"]


def test_cross_tenant_isolation(
    client: TestClient,
    session: Session,
    seeded_user: User,
) -> None:
    """Strict multi-user tenant isolation: foreign IDs in bodies 422, single lookups 404."""
    # Create User B
    b_user_id = uuid.uuid4()
    b_user = User(id=b_user_id, email="user_b@example.com", password_hash=_demo_password_hash())
    session.add(b_user)
    session.commit()

    # User A creates a transaction and a contact
    a_txn = _make_card_txn(session, seeded_user, amount_paise=-100000)
    r_ac = client.post("/api/v1/friends/contacts", json={"name": "User A Contact"})
    a_contact_id = r_ac.json()["id"]

    r_ae = client.post(
        "/api/v1/friends/entries",
        json={
            "contact_id": a_contact_id,
            "transaction_id": a_txn.id,
            "amount_paise": 50000,
            "direction": "you_gave",
        },
    )
    a_entry_id = r_ae.json()["id"]

    # Act as User B
    _act_as(client, b_user_id)

    # 1. User B cannot see User A's contact in list
    r_b_list = client.get("/api/v1/friends/contacts")
    assert not any(c["id"] == a_contact_id for c in r_b_list.json())

    # 2. User B gets 404 when looking up User A's contact directly
    r_b_get = client.get(f"/api/v1/friends/contacts/{a_contact_id}")
    assert r_b_get.status_code == 404

    # 3. User B gets 404 when patching User A's contact
    r_b_patch = client.patch(f"/api/v1/friends/contacts/{a_contact_id}", json={"name": "Hacked"})
    assert r_b_patch.status_code == 404

    # 4. User B gets 404 when archiving User A's contact
    r_b_del = client.delete(f"/api/v1/friends/contacts/{a_contact_id}")
    assert r_b_del.status_code == 404

    # 5. User B gets 404 when listing User A contact's entries
    r_b_entries = client.get(f"/api/v1/friends/contacts/{a_contact_id}/entries")
    assert r_b_entries.status_code == 404

    # 6. User B gets 404 when deleting User A's entry
    r_b_del_entry = client.delete(f"/api/v1/friends/entries/{a_entry_id}")
    assert r_b_del_entry.status_code == 404

    # 7. User B gets 422 when posting entry with User A's contact_id
    r_b_post_bad_contact = client.post(
        "/api/v1/friends/entries",
        json={
            "contact_id": a_contact_id,
            "date": "2026-03-01",
            "amount_paise": 1000,
            "direction": "you_gave",
        },
    )
    assert r_b_post_bad_contact.status_code == 422

    # 8. User B creates their own contact, but tries to link User A's transaction -> 422
    r_b_own_contact = client.post("/api/v1/friends/contacts", json={"name": "B's Friend"})
    b_contact_id = r_b_own_contact.json()["id"]

    r_b_post_foreign_txn = client.post(
        "/api/v1/friends/entries",
        json={
            "contact_id": b_contact_id,
            "transaction_id": a_txn.id,
            "amount_paise": 1000,
            "direction": "you_gave",
        },
    )
    assert r_b_post_foreign_txn.status_code == 422


def test_cashback_offset_on_peer_entry(
    client: TestClient,
    session: Session,
    seeded_user: User,
) -> None:
    """ADR-0017 Rule 8: Delayed statement cashback rebate (spend-kind Cashback)
    can be linked to a peer contact as a settlement credit (direction = 'you_got').
    Allocating with direction = 'you_gave' is rejected (422), as is allocating
    more than the rebate amount (422)."""
    # 1. Create a peer contact (e.g. Grace)
    r_c = client.post("/api/v1/friends/contacts", json={"name": "Grace Hopper"})
    assert r_c.status_code == 201
    contact_id = r_c.json()["id"]

    # 2. User fronted ₹10,000 on a credit card for Grace
    card_txn = _make_card_txn(session, seeded_user, amount_paise=-1000000)
    r_front = client.post(
        "/api/v1/friends/entries",
        json={
            "contact_id": contact_id,
            "transaction_id": card_txn.id,
            "amount_paise": 1000000,
            "direction": "you_gave",
        },
    )
    assert r_front.status_code == 201
    r_bal1 = client.get(f"/api/v1/friends/contacts/{contact_id}")
    assert r_bal1.json()["balance_paise"] == 1000000

    # 3. Create cashback category and delayed cashback rebate transaction (+50000 paise, spend)
    cashback_cat = Category(
        user_id=seeded_user.id,
        name=CASHBACK_CATEGORY_NAME,
        kind="spend",
        color="#22c55e",
    )
    session.add(cashback_cat)
    session.flush()

    cashback_txn = Transaction(
        user_id=seeded_user.id,
        account_id=card_txn.account_id,
        date=date(2026, 3, 15),
        amount_paise=50000,
        transaction_type="spend",
        category_id=cashback_cat.id,
        merchant_raw="Amazon ICICI Cashback",
        merchant_normalized="amazon icici cashback",
        fingerprint=uuid.uuid4().hex * 2,
        source="import",
    )
    session.add(cashback_txn)
    session.commit()

    # 4. Attempting to link cashback with direction='you_gave' fails 422
    r_bad_dir = client.post(
        "/api/v1/friends/entries",
        json={
            "contact_id": contact_id,
            "transaction_id": cashback_txn.id,
            "amount_paise": 50000,
            "direction": "you_gave",
        },
    )
    assert r_bad_dir.status_code == 422
    assert "direction 'you_got'" in r_bad_dir.json()["detail"]

    # 5. Attempting to allocate more than rebate amount (e.g. 60000) fails 422
    r_overflow = client.post(
        "/api/v1/friends/entries",
        json={
            "contact_id": contact_id,
            "transaction_id": cashback_txn.id,
            "amount_paise": 60000,
            "direction": "you_got",
        },
    )
    assert r_overflow.status_code == 422
    assert "exceeds transaction amount" in r_overflow.json()["detail"]

    # 6. Linking delayed cashback rebate with direction='you_got' marks is_settlement=True
    r_ok = client.post(
        "/api/v1/friends/entries",
        json={
            "contact_id": contact_id,
            "transaction_id": cashback_txn.id,
            "amount_paise": 50000,
            "direction": "you_got",
            "notes": "Amazon 5% cashback rebate offset",
        },
    )
    assert r_ok.status_code == 201
    entry = r_ok.json()
    assert entry["direction"] == "you_got"
    assert entry["is_settlement"] is True
    assert entry["amount_paise"] == 50000
    assert entry["transaction_id"] == cashback_txn.id

    # 7. Contact balance reduced by rebate: 1000000 - 50000 = 950000 paise
    r_bal2 = client.get(f"/api/v1/friends/contacts/{contact_id}")
    assert r_bal2.json()["balance_paise"] == 950000
    assert r_bal2.json()["total_got_paise"] == 50000


def test_incoming_bank_repayment_linking(
    client: TestClient,
    session: Session,
    seeded_user: User,
) -> None:
    """ADR-0017 Rule 6: An incoming credit transaction (e.g. transfer/income credit)
    can be linked as a friend repayment (direction='you_got', is_settlement=True).
    Allocation guard prevents allocating more than transaction amount."""
    # 1. Create peer contact Dave
    r_c = client.post("/api/v1/friends/contacts", json={"name": "Dave Miller"})
    assert r_c.status_code == 201
    contact_id = r_c.json()["id"]

    # 2. Front ₹4,000 for Dave
    card_txn = _make_card_txn(session, seeded_user, amount_paise=-400000)
    r_front = client.post(
        "/api/v1/friends/entries",
        json={
            "contact_id": contact_id,
            "transaction_id": card_txn.id,
            "amount_paise": 400000,
            "direction": "you_gave",
        },
    )
    assert r_front.status_code == 201

    # 3. Create incoming bank credit of ₹4,000 (+400000 paise, transfer)
    account = Account(
        user_id=seeded_user.id,
        name="Salary Account",
        type="bank",
        issuer="sbi",
        last4="5555",
    )
    session.add(account)
    session.flush()

    credit_txn = Transaction(
        user_id=seeded_user.id,
        account_id=account.id,
        date=date(2026, 3, 20),
        amount_paise=400000,
        transaction_type="transfer",
        merchant_raw="UPI / Repayment Dave",
        merchant_normalized="upi repayment dave",
        fingerprint=uuid.uuid4().hex * 2,
        source="import",
    )
    session.add(credit_txn)
    session.commit()

    # 4. Allocation guard test: 500000 > 400000 fails 422
    r_bad = client.post(
        "/api/v1/friends/entries",
        json={
            "contact_id": contact_id,
            "transaction_id": credit_txn.id,
            "amount_paise": 500000,
            "direction": "you_got",
        },
    )
    assert r_bad.status_code == 422
    assert "exceeds transaction amount" in r_bad.json()["detail"]

    # 5. Link valid repayment of ₹4,000
    r_ok = client.post(
        "/api/v1/friends/entries",
        json={
            "contact_id": contact_id,
            "transaction_id": credit_txn.id,
            "amount_paise": 400000,
            "direction": "you_got",
            "notes": "Full repayment via UPI",
        },
    )
    assert r_ok.status_code == 201
    entry = r_ok.json()
    assert entry["is_settlement"] is True
    assert entry["direction"] == "you_got"
    assert entry["amount_paise"] == 400000

    # 6. Contact balance is now 0 (fully settled)
    r_bal = client.get(f"/api/v1/friends/contacts/{contact_id}")
    assert r_bal.json()["balance_paise"] == 0


def test_contact_ledger_export_endpoint(
    client: TestClient,
    session: Session,
    seeded_user: User,
) -> None:
    """GET /api/v1/friends/contacts/{id}/export returns CSV with Content-Disposition
    and correct ledger rows; multi-tenant isolation returns 404 for other user."""
    # 1. Create contact
    r_c = client.post(
        "/api/v1/friends/contacts",
        json={"name": "Emma Watson", "upi_id": "emma@upi"},
    )
    assert r_c.status_code == 201
    contact_id = r_c.json()["id"]

    # 2. Add an entry
    r_e = client.post(
        "/api/v1/friends/entries",
        json={
            "contact_id": contact_id,
            "date": "2026-03-01",
            "amount_paise": 150000,
            "direction": "you_gave",
            "notes": "Concert ticket",
        },
    )
    assert r_e.status_code == 201

    # 3. Request export
    r_exp = client.get(f"/api/v1/friends/contacts/{contact_id}/export")
    assert r_exp.status_code == 200
    assert r_exp.headers["content-type"].startswith("text/csv")
    expected_fn = f'filename="peer_contact_{contact_id}_statement.csv"'
    assert expected_fn in r_exp.headers["content-disposition"]

    csv_text = r_exp.text
    lines = csv_text.strip().splitlines()
    assert lines[0] == (
        "Date,Description,Direction,Amount (INR),Settlement Status,Running Balance (INR)"
    )
    assert len(lines) == 2
    assert "2026-03-01,Concert ticket,you_gave,1500.00,Regular,1500.00" in lines[1]

    # 4. Multi-tenant isolation: User B gets 404
    b_user_id = uuid.uuid4()
    b_user = User(
        id=b_user_id,
        email="user_b_export@example.com",
        password_hash=_demo_password_hash(),
    )
    session.add(b_user)
    session.commit()

    _act_as(client, b_user_id)
    r_b_exp = client.get(f"/api/v1/friends/contacts/{contact_id}/export")
    assert r_b_exp.status_code == 404
