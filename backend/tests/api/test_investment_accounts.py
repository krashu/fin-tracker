"""API tests for investment accounts (ADR-0019).

Covers CRUD, validation, 409 duplicate checks, tenant isolation (ADR-0003),
and transaction linkage restrictions.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def _register(client: TestClient, email: str = "investor@example.com") -> None:
    res = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "passphrase-123"},
    )
    assert res.status_code == 201, res.text


def test_investment_accounts_crud(unauth_client: TestClient) -> None:
    _register(unauth_client, "crud@example.com")

    # 1. Initially empty
    res = unauth_client.get("/api/v1/investment-accounts")
    assert res.status_code == 200
    assert res.json() == []

    # 2. Create Zerodha Demat
    create_res = unauth_client.post(
        "/api/v1/investment-accounts",
        json={
            "name": "Zerodha Demat",
            "account_type": "demat",
            "provider": "zerodha",
            "external_account_ref": "zerodha_123",
            "currency": "INR",
        },
    )
    assert create_res.status_code == 201
    acc = create_res.json()
    acc_id = acc["id"]
    assert acc["name"] == "Zerodha Demat"
    assert acc["account_type"] == "demat"
    assert acc["provider"] == "zerodha"
    assert acc["external_account_ref"] == "zerodha_123"
    assert acc["currency"] == "INR"
    assert acc["is_active"] is True

    # 3. List
    list_res = unauth_client.get("/api/v1/investment-accounts")
    assert list_res.status_code == 200
    assert len(list_res.json()) == 1

    # 4. Get by ID
    get_res = unauth_client.get(f"/api/v1/investment-accounts/{acc_id}")
    assert get_res.status_code == 200
    assert get_res.json()["name"] == "Zerodha Demat"

    # 5. Patch name & is_active
    patch_res = unauth_client.patch(
        f"/api/v1/investment-accounts/{acc_id}",
        json={"name": "Zerodha Primary Demat", "is_active": False},
    )
    assert patch_res.status_code == 200
    assert patch_res.json()["name"] == "Zerodha Primary Demat"
    assert patch_res.json()["is_active"] is False

    # 6. Filter by is_active
    active_res = unauth_client.get("/api/v1/investment-accounts?is_active=true")
    assert active_res.status_code == 200
    assert active_res.json() == []

    inactive_res = unauth_client.get("/api/v1/investment-accounts?is_active=false")
    assert inactive_res.status_code == 200
    assert len(inactive_res.json()) == 1

    # 7. Delete
    del_res = unauth_client.delete(f"/api/v1/investment-accounts/{acc_id}")
    assert del_res.status_code == 204

    # 8. Gone
    assert unauth_client.get(f"/api/v1/investment-accounts/{acc_id}").status_code == 404


def test_investment_account_duplicate_conflict(unauth_client: TestClient) -> None:
    _register(unauth_client, "conflict@example.com")

    payload = {
        "name": "INDmoney US",
        "account_type": "us_broker",
        "provider": "indmoney",
        "external_account_ref": "ind_acc_1",
        "currency": "USD",
    }
    res1 = unauth_client.post("/api/v1/investment-accounts", json=payload)
    assert res1.status_code == 201

    # Duplicate (provider, ref) for same user -> 409
    res2 = unauth_client.post("/api/v1/investment-accounts", json=payload)
    assert res2.status_code == 409
    assert "already exists" in res2.json()["detail"]


def test_investment_account_tenant_isolation(unauth_client: TestClient) -> None:
    """ADR-0003: User B cannot view, modify, delete or target User A's investment account."""
    _register(unauth_client, "owner_a@example.com")
    res_a = unauth_client.post(
        "/api/v1/investment-accounts",
        json={
            "name": "User A Demat",
            "account_type": "demat",
            "provider": "zerodha",
            "external_account_ref": "a_1",
            "currency": "INR",
        },
    )
    assert res_a.status_code == 201
    a_acc_id = res_a.json()["id"]

    # Switch to User B
    unauth_client.cookies.clear()
    _register(unauth_client, "intruder_b@example.com")

    # User B listing does not show User A's account
    assert unauth_client.get("/api/v1/investment-accounts").json() == []

    # User B get returns 404 (ADR-0003 rule 2, never 403)
    assert unauth_client.get(f"/api/v1/investment-accounts/{a_acc_id}").status_code == 404

    # User B patch returns 404
    assert (
        unauth_client.patch(
            f"/api/v1/investment-accounts/{a_acc_id}",
            json={"name": "Hacked"},
        ).status_code
        == 404
    )

    # User B delete returns 404
    assert unauth_client.delete(f"/api/v1/investment-accounts/{a_acc_id}").status_code == 404

    # User B creating account with same (provider, ref) succeeds (per-tenant uniqueness)
    res_b = unauth_client.post(
        "/api/v1/investment-accounts",
        json={
            "name": "User B Demat",
            "account_type": "demat",
            "provider": "zerodha",
            "external_account_ref": "a_1",
            "currency": "INR",
        },
    )
    assert res_b.status_code == 201


def test_cannot_delete_account_with_transactions(unauth_client: TestClient) -> None:
    _register(unauth_client, "txns_guard@example.com")

    # Create account
    acc_res = unauth_client.post(
        "/api/v1/investment-accounts",
        json={
            "name": "Active Portfolio",
            "account_type": "demat",
            "provider": "zerodha",
            "external_account_ref": "active_1",
            "currency": "INR",
        },
    )
    assert acc_res.status_code == 201
    acc_id = acc_res.json()["id"]

    # Create instrument
    inst_res = unauth_client.post(
        "/api/v1/instruments",
        json={
            "symbol": "TCS",
            "name": "Tata Consultancy Services",
            "asset_class": "indian_equity",
            "currency": "INR",
            "exchange": "NSE",
        },
    )
    assert inst_res.status_code == 201
    inst_id = inst_res.json()["id"]

    # Record buy transaction linked to account
    txn_res = unauth_client.post(
        "/api/v1/investment-transactions",
        json={
            "instrument_id": inst_id,
            "account_id": acc_id,
            "date": "2026-06-01",
            "transaction_type": "buy",
            "units": 10,
            "price_per_unit_native": 3500,
            "amount_native_paise": 3500000,
        },
    )
    assert txn_res.status_code == 201

    # Attempting to delete account with transactions must fail with 422
    del_res = unauth_client.delete(f"/api/v1/investment-accounts/{acc_id}")
    assert del_res.status_code == 422
    assert "contains investment transactions" in del_res.json()["detail"]


def test_transaction_creation_auto_provisions_default_account(
    unauth_client: TestClient,
) -> None:
    _register(unauth_client, "auto_default@example.com")

    # Create instrument
    inst_res = unauth_client.post(
        "/api/v1/instruments",
        json={
            "symbol": "INFY",
            "name": "Infosys",
            "asset_class": "indian_equity",
            "currency": "INR",
            "exchange": "NSE",
        },
    )
    inst_id = inst_res.json()["id"]

    # Post transaction without account_id
    txn_res = unauth_client.post(
        "/api/v1/investment-transactions",
        json={
            "instrument_id": inst_id,
            "date": "2026-06-01",
            "transaction_type": "buy",
            "units": 5,
            "price_per_unit_native": 1500,
            "amount_native_paise": 750000,
        },
    )
    assert txn_res.status_code == 201
    txn = txn_res.json()
    assert txn["account_id"] is not None

    # Check that Default Portfolio was provisioned
    accs_res = unauth_client.get("/api/v1/investment-accounts")
    assert accs_res.status_code == 200
    accs = accs_res.json()
    assert len(accs) == 1
    assert accs[0]["id"] == txn["account_id"]
    assert accs[0]["name"] == "Default Portfolio"
    assert accs[0]["provider"] == "manual"


def test_deactivated_default_account_reactivation(unauth_client: TestClient) -> None:
    _register(unauth_client, "reactivate_default@example.com")

    # 1. Trigger default account provisioning
    inst_res = unauth_client.post(
        "/api/v1/instruments",
        json={
            "symbol": "TCS",
            "name": "Tata Consultancy Services",
            "asset_class": "indian_equity",
            "currency": "INR",
            "exchange": "NSE",
        },
    )
    inst_id = inst_res.json()["id"]

    txn_res = unauth_client.post(
        "/api/v1/investment-transactions",
        json={
            "instrument_id": inst_id,
            "date": "2026-06-01",
            "transaction_type": "buy",
            "units": 10,
            "price_per_unit_native": 3000,
            "amount_native_paise": 3000000,
        },
    )
    assert txn_res.status_code == 201
    default_acc_id = txn_res.json()["account_id"]

    # 2. Deactivate the default portfolio
    patch_res = unauth_client.patch(
        f"/api/v1/investment-accounts/{default_acc_id}",
        json={"is_active": False},
    )
    assert patch_res.status_code == 200
    assert patch_res.json()["is_active"] is False

    # 3. Post another transaction without account_id - should reactivate rather than crash
    txn2_res = unauth_client.post(
        "/api/v1/investment-transactions",
        json={
            "instrument_id": inst_id,
            "date": "2026-06-02",
            "transaction_type": "buy",
            "units": 5,
            "price_per_unit_native": 3100,
            "amount_native_paise": 1550000,
        },
    )
    assert txn2_res.status_code == 201
    assert txn2_res.json()["account_id"] == default_acc_id

    # 4. Verify account is active again
    get_res = unauth_client.get(f"/api/v1/investment-accounts/{default_acc_id}")
    assert get_res.status_code == 200
    assert get_res.json()["is_active"] is True
