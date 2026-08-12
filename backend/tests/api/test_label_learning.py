"""End-to-end tests for F3a Phase 2 — auto-learn merchant→label + import prefill.

Covers the four learning sites (ADR-0004 "taught once per lifecycle", mirrored
for labels) plus the import-review prefill:

* **Import prefill** — a merchant with a ``hit_count ≥ LABEL_PREFILL_MIN`` map
  entry gets those labels written onto its pending rows; a below-threshold entry
  does not.
* **Import commit** learns the row's final label set (incl. passive-accept of a
  prefilled label); a **pending** review-queue label PATCH does not learn.
* **F2 POST** learns each label on a spend row; an income row's labels do not.
* **PATCH of a board row** learns additions only and never un-learns removals.

Uses the ``_StubAxisParser`` pattern from ``test_imports.py``.
"""

from __future__ import annotations

from datetime import date
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import (
    Account,
    Category,
    Label,
    MerchantLabelMap,
    Transaction,
    TransactionLabel,
    User,
)
from app.parsers import ParsedStatement, RawTransaction, StatementSummary
from app.services import import_service
from app.services.merchant_labels import LABEL_PREFILL_MIN

# Two SWIGGY spends (auto-taggable) + one income row (hand-classified — must never
# learn/prefill labels). merchant_normalized: "SWIGGY BLR" → "swiggy blr".
_ROWS: list[RawTransaction] = [
    RawTransaction(
        date=date(2026, 3, 5), amount_paise=-15000, merchant_raw="SWIGGY BLR", txn_type="purchase"
    ),
    RawTransaction(
        date=date(2026, 3, 6), amount_paise=-22000, merchant_raw="SWIGGY BLR", txn_type="purchase"
    ),
    RawTransaction(
        date=date(2026, 3, 8),
        amount_paise=500000,
        merchant_raw="PAYMENT RECEIVED",
        txn_type="payment",
    ),
]


class _StubAxisParser:
    rows: ClassVar[list[RawTransaction]] = []

    @classmethod
    def parse(cls, pdf_bytes: bytes, password: str | None) -> ParsedStatement:
        return ParsedStatement(rows=list(cls.rows), summary=StatementSummary())


@pytest.fixture
def stub_parser(monkeypatch: pytest.MonkeyPatch) -> type[_StubAxisParser]:
    monkeypatch.setitem(import_service.PARSERS, ("axis", "credit_card"), _StubAxisParser)
    _StubAxisParser.rows = list(_ROWS)
    return _StubAxisParser


def _import(client: TestClient, account_id: int, content: bytes = b"file-A") -> int:
    resp = client.post(
        "/api/v1/imports",
        data={"account_id": str(account_id)},
        files={"file": ("statement.pdf", content, "application/pdf")},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["batch_id"]


def _make_label(session: Session, user_id: object, name: str) -> Label:
    label = Label(user_id=user_id, name=name)
    session.add(label)
    session.commit()
    session.refresh(label)
    return label


def _seed_map(session: Session, user_id: object, merchant: str, label_id: int, hits: int) -> None:
    session.add(
        MerchantLabelMap(
            user_id=user_id, merchant_normalized=merchant, label_id=label_id, hit_count=hits
        )
    )
    session.commit()


def _label_ids_on(session: Session, txn_id: int) -> set[int]:
    return set(
        session.scalars(
            select(TransactionLabel.label_id).where(TransactionLabel.transaction_id == txn_id)
        )
    )


# -----------------------------------------------------------------------------
# Import prefill.
# -----------------------------------------------------------------------------
def test_import_prefills_labels_at_threshold_not_below(
    client: TestClient,
    axis_account: Account,
    seeded_user: User,
    stub_parser: type[_StubAxisParser],
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    online = _make_label(session, seeded_user.id, "online")
    rare = _make_label(session, seeded_user.id, "rare")
    _seed_map(session, seeded_user.id, "swiggy blr", online.id, LABEL_PREFILL_MIN)
    _seed_map(session, seeded_user.id, "swiggy blr", rare.id, LABEL_PREFILL_MIN - 1)

    _import(client, axis_account.id)

    with session_factory() as s:
        swiggy_rows = list(
            s.scalars(select(Transaction).where(Transaction.merchant_normalized == "swiggy blr"))
        )
        assert len(swiggy_rows) == 2
        for row in swiggy_rows:
            # The threshold-clearing label is prefilled; the one-off is not.
            assert _label_ids_on(s, row.id) == {online.id}
        # The income row never gets a label prefill (spend/refund gate).
        income = s.scalar(
            select(Transaction).where(Transaction.merchant_normalized == "payment received")
        )
        assert income is not None and _label_ids_on(s, income.id) == set()


# -----------------------------------------------------------------------------
# Import commit learning + passive accept.
# -----------------------------------------------------------------------------
def test_commit_learns_final_label_set_including_passive_accept(
    client: TestClient,
    axis_account: Account,
    seeded_user: User,
    seeded_categories: list[Category],
    stub_parser: type[_StubAxisParser],
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """A prefilled label (hit_count 3) that the user passively accepts at commit
    bumps to 4 — the same inertia-bump decision categories use."""
    online = _make_label(session, seeded_user.id, "online")
    _seed_map(session, seeded_user.id, "swiggy blr", online.id, LABEL_PREFILL_MIN)
    food = next(c for c in seeded_categories if c.name == "Food")

    batch_id = _import(client, axis_account.id)
    candidates = client.get(f"/api/v1/imports/{batch_id}/candidates").json()
    swiggy_ids = [c["id"] for c in candidates if "SWIGGY" in c["merchant_raw"]]
    assert len(swiggy_ids) == 2
    # Prefilled label surfaces in the candidate payload.
    assert all(
        any(lbl["name"] == "online" for lbl in c["labels"])
        for c in candidates
        if c["id"] in swiggy_ids
    )

    # Give them a category (bypass PATCH — measure commit's contribution only).
    with session_factory() as s:
        for tid in swiggy_ids:
            s.get(Transaction, tid).category_id = food.id  # type: ignore[union-attr]
        s.commit()

    commit = client.post(f"/api/v1/imports/{batch_id}/commit", json={"transaction_ids": swiggy_ids})
    assert commit.status_code == 204

    with session_factory() as s:
        row = s.scalar(select(MerchantLabelMap).where(MerchantLabelMap.label_id == online.id))
        # Seeded 3, +1 per committed row (2 rows) = 5.
        assert row is not None and row.hit_count == LABEL_PREFILL_MIN + 2


def test_pending_row_label_patch_does_not_learn(
    client: TestClient,
    axis_account: Account,
    stub_parser: type[_StubAxisParser],
    session_factory: sessionmaker[Session],
) -> None:
    """A label PATCH on a still-pending review-queue row must not write the map —
    it learns only at commit (pass 4), so a discarded row leaves no orphan rule."""
    batch_id = _import(client, axis_account.id)
    candidates = client.get(f"/api/v1/imports/{batch_id}/candidates").json()
    swiggy_id = next(c["id"] for c in candidates if "SWIGGY" in c["merchant_raw"])

    resp = client.patch(f"/api/v1/transactions/{swiggy_id}", json={"labels": ["online"]})
    assert resp.status_code == 200

    with session_factory() as s:
        row = s.get(Transaction, swiggy_id)
        assert row is not None and row.confirmed_at is None  # still pending
        # Label applied to the row, but nothing learned.
        assert _label_ids_on(s, swiggy_id) != set()
        assert s.scalar(select(func.count()).select_from(MerchantLabelMap)) == 0


# -----------------------------------------------------------------------------
# F2 manual POST learning.
# -----------------------------------------------------------------------------
def test_f2_post_learns_spend_labels_not_income(
    client: TestClient,
    axis_account: Account,
    session_factory: sessionmaker[Session],
) -> None:
    spend = client.post(
        "/api/v1/transactions",
        json={
            "date": "2026-03-01",
            "account_id": axis_account.id,
            "amount_paise": -12000,
            "transaction_type": "spend",
            "merchant_raw": "STARBUCKS",
            "labels": ["coffee", "treats"],
        },
    )
    assert spend.status_code == 201, spend.text

    income = client.post(
        "/api/v1/transactions",
        json={
            "date": "2026-03-02",
            "account_id": axis_account.id,
            "amount_paise": 500000,
            "transaction_type": "income",
            "merchant_raw": "EMPLOYER",
            "labels": ["salary"],
        },
    )
    assert income.status_code == 201, income.text

    with session_factory() as s:
        learned = {
            (r.merchant_normalized, r.hit_count)
            for r in s.scalars(
                select(MerchantLabelMap).join(Label, Label.id == MerchantLabelMap.label_id)
            )
        }
        # Two spend labels learned at hit_count 1; the income label ("salary") is
        # never learned — income is hand-classified.
        assert learned == {("starbucks", 1)}  # both coffee+treats share this key
        names = set(
            s.scalars(
                select(Label.name).join(MerchantLabelMap, MerchantLabelMap.label_id == Label.id)
            )
        )
        assert names == {"coffee", "treats"}
        assert "salary" not in names


# -----------------------------------------------------------------------------
# PATCH of a board row — additions only, no un-learning.
# -----------------------------------------------------------------------------
def test_patch_board_row_learns_additions_only_no_unlearn(
    client: TestClient,
    axis_account: Account,
    session_factory: sessionmaker[Session],
) -> None:
    # Born on the board (F2 POST) with one label — learns coffee=1.
    created = client.post(
        "/api/v1/transactions",
        json={
            "date": "2026-03-01",
            "account_id": axis_account.id,
            "amount_paise": -12000,
            "transaction_type": "spend",
            "merchant_raw": "STARBUCKS",
            "labels": ["coffee"],
        },
    )
    assert created.status_code == 201, created.text
    txn_id = created.json()["id"]

    # Add "treats" → additions={treats} learns treats=1; coffee not re-bumped.
    r1 = client.patch(f"/api/v1/transactions/{txn_id}", json={"labels": ["coffee", "treats"]})
    assert r1.status_code == 200
    # Remove "treats" → additions empty → nothing learned; treats not decremented.
    r2 = client.patch(f"/api/v1/transactions/{txn_id}", json={"labels": ["coffee"]})
    assert r2.status_code == 200

    with session_factory() as s:
        counts = {
            name: hit
            for name, hit in s.execute(
                select(Label.name, MerchantLabelMap.hit_count).join(
                    MerchantLabelMap, MerchantLabelMap.label_id == Label.id
                )
            )
        }
        assert counts == {"coffee": 1, "treats": 1}
