"""End-to-end vertical slice: create → import → review → commit → list.

Locks the contract that the full lifecycle composes into the user-visible
flow. Reuses the ``_StubAxisParser`` pattern from ``test_imports.py`` so
the slice runs without a real PDF.

The slice exercises both branches of the commit's category-id rule: the
two spend rows get PATCHed to a category first (commit would reject them
otherwise); the income row (``PAYMENT RECEIVED``) commits with
``category_id IS NULL`` per the locked decision in
``imports-review-flow``.
"""

from __future__ import annotations

from datetime import date
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient

from app.models import Category, User
from app.parsers import ParsedStatement, RawTransaction, StatementSummary
from app.services import import_service


class _StubAxisParser:
    rows: ClassVar[list[RawTransaction]] = []

    @classmethod
    def parse(cls, pdf_bytes: bytes, password: str | None) -> ParsedStatement:
        return ParsedStatement(rows=list(cls.rows), summary=StatementSummary())


_SLICE_ROWS: list[RawTransaction] = [
    RawTransaction(
        date=date(2026, 3, 5),
        amount_paise=-8500,
        merchant_raw="SLICE TRANSPORT",
        txn_type="purchase",
    ),
    RawTransaction(
        date=date(2026, 3, 10),
        amount_paise=-12000,
        merchant_raw="SLICE CAFE",
        txn_type="purchase",
    ),
    RawTransaction(
        date=date(2026, 3, 20),
        amount_paise=500000,
        merchant_raw="PAYMENT RECEIVED",
        txn_type="payment",
    ),
]


@pytest.fixture
def stub_axis_parser(monkeypatch: pytest.MonkeyPatch) -> type[_StubAxisParser]:
    monkeypatch.setitem(import_service.PARSERS, ("axis", "credit_card"), _StubAxisParser)
    _StubAxisParser.rows = list(_SLICE_ROWS)
    return _StubAxisParser


def test_create_then_import_then_review_then_commit_then_list(
    client: TestClient,
    seeded_user: User,
    seeded_categories: list[Category],
    stub_axis_parser: type[_StubAxisParser],
) -> None:
    # 1. Create account.
    create_resp = client.post(
        "/api/v1/accounts",
        json={
            "name": "Axis CC",
            "type": "credit_card",
            "issuer": "axis",
            "last4": "1234",
        },
    )
    assert create_resp.status_code == 201, create_resp.text
    account_id = create_resp.json()["id"]

    # 2. Import → all rows land pending.
    import_resp = client.post(
        "/api/v1/imports",
        data={"account_id": str(account_id)},
        files={"file": ("statement.pdf", b"slice-bytes", "application/pdf")},
    )
    assert import_resp.status_code == 200, import_resp.text
    import_body = import_resp.json()
    assert import_body["imported"] == len(_SLICE_ROWS)
    assert import_body["already_imported"] is False
    batch_id = import_body["batch_id"]

    # 3. Board is empty pre-commit — pending rows don't surface here.
    pre_commit = client.get(f"/api/v1/transactions?account_id={account_id}")
    assert pre_commit.status_code == 200
    assert pre_commit.json() == []

    # 4. Review queue lists all 3 with confidence=none (no prior history).
    candidates_resp = client.get(f"/api/v1/imports/{batch_id}/candidates")
    assert candidates_resp.status_code == 200
    candidates = candidates_resp.json()
    assert len(candidates) == 3
    assert all(c["confidence"] == "none" for c in candidates)
    assert all(c["prior_matches"] == 0 for c in candidates)

    # 5. PATCH the spend rows to a category (commit rejects spend-with-null).
    food = next(c for c in seeded_categories if c.name == "Food")
    transport = next(c for c in seeded_categories if c.name == "Transport")
    for c in candidates:
        if c["amount_paise"] >= 0:
            continue  # income row: commit accepts null category
        cat_id = transport.id if "TRANSPORT" in c["merchant_raw"] else food.id
        patch_resp = client.patch(
            f"/api/v1/transactions/{c['id']}",
            json={"category_id": cat_id},
        )
        assert patch_resp.status_code == 200, patch_resp.text

    # 6. Commit all 3 ids atomically.
    commit_resp = client.post(
        f"/api/v1/imports/{batch_id}/commit",
        json={"transaction_ids": [c["id"] for c in candidates]},
    )
    assert commit_resp.status_code == 204, commit_resp.text

    # 7. Board now returns all 3 (newest first).
    list_resp = client.get(f"/api/v1/transactions?account_id={account_id}")
    assert list_resp.status_code == 200
    rows = list_resp.json()
    assert len(rows) == len(_SLICE_ROWS)

    assert rows[0]["date"] == "2026-03-20"
    assert rows[0]["amount_paise"] == 500000

    # Flat shape — TransactionRead's exact key set (no `confirmed_at` leakage).
    assert set(rows[0].keys()) == {
        "id",
        "account_id",
        "date",
        "amount_paise",
        "transaction_type",
        "merchant_raw",
        "category_id",
        "transfer_pair_id",
        "labels",
    }
    assert {row["account_id"] for row in rows} == {account_id}

    # 8. Review queue is empty post-commit.
    post_commit_candidates = client.get(f"/api/v1/imports/{batch_id}/candidates")
    assert post_commit_candidates.status_code == 200
    assert post_commit_candidates.json() == []
