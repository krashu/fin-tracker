"""End-to-end tests for the import review/commit lifecycle (PRD §F1 step 5).

Covers:

* Post-import row state — ``confirmed_at IS NULL``.
* ``GET /imports/{batch_id}/candidates`` — pending filter, ``prior_matches``
  COALESCE, confidence threshold boundaries, cross-user 404.
* ``POST /imports/{batch_id}/commit`` — happy path, atomic 422 (missing /
  cross-batch / already-confirmed), untagged spend/refund → "Other" default
  (with 422 fallback when no "Other" exists), income/transfer null-category
  branch, same-merchant-twice ``hit_count`` accounting.
* ``DELETE /imports/{batch_id}`` — partial cancel keeps batch, full cancel
  deletes batch (re-upload re-runs parser), idempotency, cross-user 404.

Uses the ``_StubAxisParser`` pattern from ``test_imports.py`` — no real PDF.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.models import (
    Account,
    Category,
    ImportBatch,
    MerchantTagMap,
    Transaction,
    User,
)
from app.parsers import RawTransaction
from app.services import import_service

# -----------------------------------------------------------------------------
# Stub parser — copies test_imports.py's pattern.
# -----------------------------------------------------------------------------
_REVIEW_ROWS: list[RawTransaction] = [
    RawTransaction(
        date=date(2026, 3, 5),
        amount_paise=-15000,
        merchant_raw="SWIGGY BLR",
        txn_type="purchase",
    ),
    RawTransaction(
        date=date(2026, 3, 6),
        amount_paise=-22000,
        merchant_raw="SWIGGY BLR",
        txn_type="purchase",
    ),
    RawTransaction(
        date=date(2026, 3, 7),
        amount_paise=-9500,
        merchant_raw="UBER MUM",
        txn_type="purchase",
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
    def parse(cls, pdf_bytes: bytes, password: str | None) -> list[RawTransaction]:
        return list(cls.rows)


@pytest.fixture
def stub_parser(monkeypatch: pytest.MonkeyPatch) -> type[_StubAxisParser]:
    monkeypatch.setitem(import_service.PARSERS, ("axis", "credit_card"), _StubAxisParser)
    _StubAxisParser.rows = list(_REVIEW_ROWS)
    return _StubAxisParser


def _import_and_get_batch_id(
    client: TestClient, account_id: int, content: bytes = b"file-A"
) -> int:
    resp = client.post(
        "/api/v1/imports",
        data={"account_id": str(account_id)},
        files={"file": ("statement.pdf", content, "application/pdf")},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["batch_id"]


# -----------------------------------------------------------------------------
# Post-import state.
# -----------------------------------------------------------------------------
def test_imported_rows_land_pending(
    client: TestClient,
    axis_account: Account,
    stub_parser: type[_StubAxisParser],
    session_factory: sessionmaker[Session],
) -> None:
    _import_and_get_batch_id(client, axis_account.id)

    with session_factory() as s:
        rows = list(s.scalars(select(Transaction)))
        assert len(rows) == len(_REVIEW_ROWS)
        assert all(r.confirmed_at is None for r in rows)


# -----------------------------------------------------------------------------
# GET /candidates.
# -----------------------------------------------------------------------------
def test_candidates_returns_only_pending_rows_of_this_batch(
    client: TestClient,
    axis_account: Account,
    stub_parser: type[_StubAxisParser],
) -> None:
    batch_id = _import_and_get_batch_id(client, axis_account.id)

    resp = client.get(f"/api/v1/imports/{batch_id}/candidates")
    assert resp.status_code == 200
    candidates = resp.json()
    assert len(candidates) == len(_REVIEW_ROWS)
    # Sorted newest-first.
    assert [c["date"] for c in candidates] == [
        "2026-03-08",
        "2026-03-07",
        "2026-03-06",
        "2026-03-05",
    ]
    # Wire shape — same as TransactionRead + prior_matches + confidence + pinned.
    assert set(candidates[0].keys()) == {
        "id",
        "account_id",
        "date",
        "amount_paise",
        "transaction_type",
        "merchant_raw",
        "category_id",
        "transfer_pair_id",
        "labels",
        "prior_matches",
        "confidence",
        "pinned",
    }


def test_candidates_unknown_batch_returns_404(
    client: TestClient,
    seeded_user: User,
) -> None:
    resp = client.get("/api/v1/imports/99999/candidates")
    assert resp.status_code == 404


def test_candidates_cross_user_returns_404(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    # Manually create another user + their import batch. Two commits because
    # accounts.user_id FK requires the user row to exist first; SQLAlchemy's
    # INSERT ordering on a single commit can flush in the wrong order.
    other_user = User(id=uuid.UUID("00000000-0000-0000-0000-000000000099"))
    session.add(other_user)
    session.commit()
    other_account = Account(
        user_id=other_user.id,
        name="Other CC",
        type="credit_card",
        issuer="axis",
        last4="9999",
    )
    session.add(other_account)
    session.commit()
    session.refresh(other_account)

    batch = ImportBatch(
        user_id=other_user.id,
        account_id=other_account.id,
        source_file_hash="other-hash",
        parser_name="AxisCC",
        status="completed",
    )
    session.add(batch)
    session.commit()
    session.refresh(batch)

    # v1 single-user client always acts as seeded_user; this batch is not theirs.
    resp = client.get(f"/api/v1/imports/{batch.id}/candidates")
    assert resp.status_code == 404


@pytest.mark.parametrize(
    ("hit_count", "expected"),
    [(0, "none"), (1, "uncertain"), (2, "uncertain"), (3, "confident"), (99, "confident")],
)
def test_candidates_confidence_thresholds(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    stub_parser: type[_StubAxisParser],
    session: Session,
    hit_count: int,
    expected: str,
) -> None:
    """Confidence label is derived from prior_matches at (>=3, 1-2, 0)."""
    food = next(c for c in seeded_categories if c.name == "Food")

    # Force one parser row + its auto-tag so the imported row arrives with
    # category_id=food.id. Then bump merchant_tag_map.hit_count to the
    # parametrized value, re-import a different file, and check the candidate.
    _StubAxisParser.rows = [
        RawTransaction(
            date=date(2026, 3, 5),
            amount_paise=-15000,
            merchant_raw="SWIGGY BLR",
            txn_type="purchase",
        ),
    ]

    # Pre-seed the merchant_tag_map row so import's auto-tag applies on first import.
    if hit_count > 0:
        session.add(
            MerchantTagMap(
                user_id=axis_account.user_id,
                merchant_normalized="swiggy blr",
                category_id=food.id,
                hit_count=hit_count,
            )
        )
        session.commit()

    batch_id = _import_and_get_batch_id(client, axis_account.id)

    resp = client.get(f"/api/v1/imports/{batch_id}/candidates")
    assert resp.status_code == 200
    candidates = resp.json()
    assert len(candidates) == 1
    assert candidates[0]["prior_matches"] == hit_count
    assert candidates[0]["confidence"] == expected


def test_candidates_surface_pinned_flag(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    stub_parser: type[_StubAxisParser],
    session: Session,
) -> None:
    """A freshly-pinned rule prefills at hit_count=1 (→ "uncertain") but the
    candidate must carry ``pinned=True`` so the picker renders the authored
    state instead of the "only 1 prior" tint (#4)."""
    food = next(c for c in seeded_categories if c.name == "Food")
    _StubAxisParser.rows = [
        RawTransaction(
            date=date(2026, 3, 5),
            amount_paise=-15000,
            merchant_raw="SWIGGY BLR",
            txn_type="purchase",
        ),
    ]
    session.add(
        MerchantTagMap(
            user_id=axis_account.user_id,
            merchant_normalized="swiggy blr",
            category_id=food.id,
            hit_count=1,
            pinned=True,
        )
    )
    session.commit()

    batch_id = _import_and_get_batch_id(client, axis_account.id)
    candidates = client.get(f"/api/v1/imports/{batch_id}/candidates").json()
    assert len(candidates) == 1
    row = candidates[0]
    assert row["category_id"] == food.id
    assert row["prior_matches"] == 1
    assert row["confidence"] == "uncertain"  # hit_count-derived, unchanged
    assert row["pinned"] is True  # …but the authored flag overrides the tint


def test_candidates_pinned_false_without_rule(
    client: TestClient,
    axis_account: Account,
    stub_parser: type[_StubAxisParser],
) -> None:
    """No merchant_tag_map winner → pinned coalesces to False."""
    batch_id = _import_and_get_batch_id(client, axis_account.id)
    candidates = client.get(f"/api/v1/imports/{batch_id}/candidates").json()
    assert all(c["pinned"] is False for c in candidates)


def test_candidates_null_category_collapses_to_prior_matches_zero(
    client: TestClient,
    axis_account: Account,
    stub_parser: type[_StubAxisParser],
) -> None:
    """income/transfer rows have category_id IS NULL → no JOIN match → prior_matches=0."""
    batch_id = _import_and_get_batch_id(client, axis_account.id)
    resp = client.get(f"/api/v1/imports/{batch_id}/candidates")
    candidates = resp.json()

    income_row = next(c for c in candidates if c["amount_paise"] > 0)
    assert income_row["category_id"] is None
    assert income_row["prior_matches"] == 0
    assert income_row["confidence"] == "none"


# -----------------------------------------------------------------------------
# POST /commit.
# -----------------------------------------------------------------------------
def _tag_spend_rows_to_food(
    client: TestClient,
    candidates: list[dict],
    food_id: int,
) -> list[int]:
    """PATCH every spend row to a category; return spend row ids."""
    spend_ids: list[int] = []
    for c in candidates:
        if c["transaction_type"] == "spend":
            patch = client.patch(
                f"/api/v1/transactions/{c['id']}",
                json={"category_id": food_id},
            )
            assert patch.status_code == 200, patch.text
            spend_ids.append(c["id"])
    return spend_ids


def test_commit_happy_path_stamps_confirmed_at_and_returns_to_board(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    stub_parser: type[_StubAxisParser],
    session_factory: sessionmaker[Session],
) -> None:
    batch_id = _import_and_get_batch_id(client, axis_account.id)
    food = next(c for c in seeded_categories if c.name == "Food")

    candidates = client.get(f"/api/v1/imports/{batch_id}/candidates").json()
    _tag_spend_rows_to_food(client, candidates, food.id)

    ids = [c["id"] for c in candidates]
    commit = client.post(
        f"/api/v1/imports/{batch_id}/commit",
        json={"transaction_ids": ids},
    )
    assert commit.status_code == 204, commit.text

    # Board now contains all committed rows.
    board = client.get(f"/api/v1/transactions?account_id={axis_account.id}").json()
    assert {r["id"] for r in board} == set(ids)

    with session_factory() as s:
        rows = list(s.scalars(select(Transaction)))
        assert all(r.confirmed_at is not None for r in rows)


def test_commit_atomic_422_with_invalid_ids(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    stub_parser: type[_StubAxisParser],
    session_factory: sessionmaker[Session],
) -> None:
    """One bad id rolls back the whole commit — no partial writes."""
    batch_id = _import_and_get_batch_id(client, axis_account.id)
    food = next(c for c in seeded_categories if c.name == "Food")

    candidates = client.get(f"/api/v1/imports/{batch_id}/candidates").json()
    _tag_spend_rows_to_food(client, candidates, food.id)

    good_ids = [c["id"] for c in candidates]
    # Add a non-existent id.
    payload_ids = good_ids + [99999]

    commit = client.post(
        f"/api/v1/imports/{batch_id}/commit",
        json={"transaction_ids": payload_ids},
    )
    assert commit.status_code == 422, commit.text
    body = commit.json()
    assert body["detail"]["message"] == "some transactions are not eligible to commit"
    assert body["detail"]["invalid_ids"] == [99999]

    # No writes — every row still pending.
    with session_factory() as s:
        confirmed = s.scalars(
            select(Transaction).where(Transaction.confirmed_at.is_not(None))
        ).all()
        assert confirmed == []


def test_commit_defaults_untagged_spend_to_other(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    stub_parser: type[_StubAxisParser],
    session_factory: sessionmaker[Session],
) -> None:
    """Untagged spend/refund rows commit under the spend "Other" category (PRD §F5
    fallback), not rejected; income stays null. The Other default is NOT learned."""
    batch_id = _import_and_get_batch_id(client, axis_account.id)
    other = next(c for c in seeded_categories if c.name == "Other")

    candidates = client.get(f"/api/v1/imports/{batch_id}/candidates").json()
    spend_ids = [c["id"] for c in candidates if c["transaction_type"] == "spend"]
    income_id = next(c["id"] for c in candidates if c["transaction_type"] == "income")

    commit = client.post(
        f"/api/v1/imports/{batch_id}/commit",
        json={"transaction_ids": [*spend_ids, income_id]},
    )
    assert commit.status_code == 204, commit.text

    with session_factory() as s:
        for sid in spend_ids:
            row = s.get(Transaction, sid)
            assert row is not None
            assert row.category_id == other.id
        income = s.get(Transaction, income_id)
        assert income is not None and income.category_id is None
        # "Other" defaults are a fallback, not a merchant decision — no learning.
        assert s.scalar(select(func.count()).select_from(MerchantTagMap)) == 0


def test_commit_allows_income_with_null_category(
    client: TestClient,
    axis_account: Account,
    stub_parser: type[_StubAxisParser],
    session_factory: sessionmaker[Session],
) -> None:
    batch_id = _import_and_get_batch_id(client, axis_account.id)
    candidates = client.get(f"/api/v1/imports/{batch_id}/candidates").json()
    income_id = next(c["id"] for c in candidates if c["transaction_type"] == "income")

    commit = client.post(
        f"/api/v1/imports/{batch_id}/commit",
        json={"transaction_ids": [income_id]},
    )
    assert commit.status_code == 204, commit.text

    # No merchant_tag_map row created for the null-category income commit.
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(MerchantTagMap)) == 0


def test_commit_same_merchant_twice_bumps_hit_count_per_row(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    stub_parser: type[_StubAxisParser],
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """Commit emits one record_tag per row, even when the same merchant repeats.

    Direct-assign the category in the DB (bypassing PATCH) so the test
    measures *only* commit's contribution. PATCH-based tagging would add 1
    bump per row before commit; that's the realistic UI flow but obscures
    what this test is locking down.
    """
    batch_id = _import_and_get_batch_id(client, axis_account.id)
    food = next(c for c in seeded_categories if c.name == "Food")

    candidates = client.get(f"/api/v1/imports/{batch_id}/candidates").json()
    swiggy_ids = [c["id"] for c in candidates if "SWIGGY" in c["merchant_raw"]]
    assert len(swiggy_ids) == 2

    # Bypass PATCH so we're measuring commit's bumps only.
    for tid in swiggy_ids:
        txn = session.get(Transaction, tid)
        assert txn is not None
        txn.category_id = food.id
    session.commit()

    commit = client.post(
        f"/api/v1/imports/{batch_id}/commit",
        json={"transaction_ids": swiggy_ids},
    )
    assert commit.status_code == 204

    with session_factory() as s:
        tag = s.scalar(
            select(MerchantTagMap).where(
                MerchantTagMap.merchant_normalized == "swiggy blr",
                MerchantTagMap.category_id == food.id,
            )
        )
        assert tag is not None
        assert tag.hit_count == 2


# -----------------------------------------------------------------------------
# F3 learning lifecycle — PATCH-on-pending gating, single-learn, archived-cat.
# -----------------------------------------------------------------------------
def test_patch_pending_row_does_not_learn(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    stub_parser: type[_StubAxisParser],
    session_factory: sessionmaker[Session],
) -> None:
    """Tagging a still-pending review-queue row must NOT write merchant_tag_map.

    The row learns once at commit (pass-3). Learning on the PATCH would let a
    discarded row leave an orphan rule and self-inflate its /candidates
    confidence; only board (confirmed) rows learn on PATCH.
    """
    batch_id = _import_and_get_batch_id(client, axis_account.id)
    food = next(c for c in seeded_categories if c.name == "Food")

    candidates = client.get(f"/api/v1/imports/{batch_id}/candidates").json()
    swiggy_id = next(c["id"] for c in candidates if "SWIGGY" in c["merchant_raw"])

    resp = client.patch(f"/api/v1/transactions/{swiggy_id}", json={"category_id": food.id})
    assert resp.status_code == 200

    with session_factory() as s:
        row = s.get(Transaction, swiggy_id)
        # Category set on the row, but no rule learned yet — it's still pending.
        assert row is not None and row.category_id == food.id and row.confirmed_at is None
        assert s.scalar(select(func.count()).select_from(MerchantTagMap)) == 0


def test_patched_pending_row_learns_once_at_commit(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    stub_parser: type[_StubAxisParser],
    session_factory: sessionmaker[Session],
) -> None:
    """A row corrected in the queue then committed learns exactly once (#1).

    Before the fix PATCH (+1) and commit pass-3 (+1) double-counted one
    decision; now the pending PATCH is a no-op for learning, so the single bump
    is commit's.
    """
    batch_id = _import_and_get_batch_id(client, axis_account.id)
    food = next(c for c in seeded_categories if c.name == "Food")

    candidates = client.get(f"/api/v1/imports/{batch_id}/candidates").json()
    swiggy_id = next(c["id"] for c in candidates if "SWIGGY" in c["merchant_raw"])

    client.patch(f"/api/v1/transactions/{swiggy_id}", json={"category_id": food.id})
    commit = client.post(
        f"/api/v1/imports/{batch_id}/commit",
        json={"transaction_ids": [swiggy_id]},
    )
    assert commit.status_code == 204, commit.text

    with session_factory() as s:
        tag = s.scalar(
            select(MerchantTagMap).where(
                MerchantTagMap.merchant_normalized == "swiggy blr",
                MerchantTagMap.category_id == food.id,
            )
        )
        assert tag is not None
        assert tag.hit_count == 1  # one decision → one bump, not two


def test_candidates_confidence_not_self_inflated_by_own_patch(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    stub_parser: type[_StubAxisParser],
) -> None:
    """PATCHing a pending row must not raise its own prior_matches/confidence (#4).

    prior_matches should reflect PRIOR decisions, not the user's in-session pick.
    With no map row written on the pending PATCH, a brand-new merchant stays at
    prior_matches=0 / "none".
    """
    batch_id = _import_and_get_batch_id(client, axis_account.id)
    food = next(c for c in seeded_categories if c.name == "Food")

    before = client.get(f"/api/v1/imports/{batch_id}/candidates").json()
    swiggy = next(c for c in before if "SWIGGY" in c["merchant_raw"])
    assert swiggy["prior_matches"] == 0
    assert swiggy["confidence"] == "none"

    client.patch(f"/api/v1/transactions/{swiggy['id']}", json={"category_id": food.id})

    after = client.get(f"/api/v1/imports/{batch_id}/candidates").json()
    swiggy_after = next(c for c in after if c["id"] == swiggy["id"])
    assert swiggy_after["category_id"] == food.id  # category set…
    assert swiggy_after["prior_matches"] == 0  # …but no self-vote
    assert swiggy_after["confidence"] == "none"


def test_commit_defaults_archived_category_row_to_other(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    stub_parser: type[_StubAxisParser],
    session_factory: sessionmaker[Session],
) -> None:
    """A pending row whose category is archived mid-review commits under "Other"
    and never resurrects a merchant_tag_map row for the archived bucket (#3)."""
    batch_id = _import_and_get_batch_id(client, axis_account.id)
    food = next(c for c in seeded_categories if c.name == "Food")
    other = next(c for c in seeded_categories if c.name == "Other")

    candidates = client.get(f"/api/v1/imports/{batch_id}/candidates").json()
    swiggy_id = next(c["id"] for c in candidates if "SWIGGY" in c["merchant_raw"])

    # Tag the pending row to Food, then archive Food out from under it.
    client.patch(f"/api/v1/transactions/{swiggy_id}", json={"category_id": food.id})
    assert client.delete(f"/api/v1/categories/{food.id}").status_code == 204

    commit = client.post(
        f"/api/v1/imports/{batch_id}/commit",
        json={"transaction_ids": [swiggy_id]},
    )
    assert commit.status_code == 204, commit.text

    with session_factory() as s:
        row = s.get(Transaction, swiggy_id)
        assert row is not None
        assert row.category_id == other.id  # re-bucketed, not a dead reference
        # No zombie rule pointing at the archived Food; nothing learned at all.
        assert (
            s.scalar(
                select(func.count())
                .select_from(MerchantTagMap)
                .where(MerchantTagMap.category_id == food.id)
            )
            == 0
        )
        assert s.scalar(select(func.count()).select_from(MerchantTagMap)) == 0


def test_commit_accumulates_onto_existing_rule(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    stub_parser: type[_StubAxisParser],
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """Two same-merchant rows committed onto a pre-existing rule bump it +2.

    Locks the read-modify-write increment against a regression to a deferred
    SQL-expression form, which would collapse the two same-triple bumps in one
    autoflush=False commit pass to +1 (see the deferred #6 note in the plan).
    """
    user_id = axis_account.user_id
    food = next(c for c in seeded_categories if c.name == "Food")
    session.add(
        MerchantTagMap(
            user_id=user_id,
            merchant_normalized="swiggy blr",
            category_id=food.id,
            hit_count=5,
        )
    )
    session.commit()

    batch_id = _import_and_get_batch_id(client, axis_account.id)
    candidates = client.get(f"/api/v1/imports/{batch_id}/candidates").json()
    swiggy_ids = [c["id"] for c in candidates if "SWIGGY" in c["merchant_raw"]]
    assert len(swiggy_ids) == 2  # both auto-tagged to Food from the existing rule

    commit = client.post(
        f"/api/v1/imports/{batch_id}/commit",
        json={"transaction_ids": swiggy_ids},
    )
    assert commit.status_code == 204, commit.text

    with session_factory() as s:
        tag = s.scalar(
            select(MerchantTagMap).where(
                MerchantTagMap.merchant_normalized == "swiggy blr",
                MerchantTagMap.category_id == food.id,
            )
        )
        assert tag is not None
        assert tag.hit_count == 7  # 5 + 2, not 6


def test_commit_rejects_already_confirmed_row(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    stub_parser: type[_StubAxisParser],
) -> None:
    batch_id = _import_and_get_batch_id(client, axis_account.id)
    food = next(c for c in seeded_categories if c.name == "Food")

    candidates = client.get(f"/api/v1/imports/{batch_id}/candidates").json()
    swiggy_id = next(c["id"] for c in candidates if "SWIGGY" in c["merchant_raw"])

    # Tag + commit once.
    client.patch(f"/api/v1/transactions/{swiggy_id}", json={"category_id": food.id})
    first = client.post(
        f"/api/v1/imports/{batch_id}/commit",
        json={"transaction_ids": [swiggy_id]},
    )
    assert first.status_code == 204

    # Second commit on the same id → 422.
    second = client.post(
        f"/api/v1/imports/{batch_id}/commit",
        json={"transaction_ids": [swiggy_id]},
    )
    assert second.status_code == 422
    assert second.json()["detail"]["invalid_ids"] == [swiggy_id]


# -----------------------------------------------------------------------------
# DELETE /imports/{batch_id} — cancel.
# -----------------------------------------------------------------------------
def test_cancel_full_deletes_batch_row_and_reupload_reruns_parser(
    client: TestClient,
    axis_account: Account,
    stub_parser: type[_StubAxisParser],
    session_factory: sessionmaker[Session],
) -> None:
    batch_id = _import_and_get_batch_id(client, axis_account.id, content=b"file-A")

    cancel = client.delete(f"/api/v1/imports/{batch_id}")
    assert cancel.status_code == 204

    with session_factory() as s:
        # ImportBatch row gone — no survivors.
        assert s.get(ImportBatch, batch_id) is None
        # Pending rows of this batch gone too.
        assert s.scalar(select(func.count()).select_from(Transaction)) == 0

    # Re-upload of the same file re-runs the parser (no short-circuit).
    # Don't rely on batch_id inequality — SQLite reuses rowids after the
    # last delete. Check `imported` > 0 and `already_imported = False`
    # instead; the short-circuit branch would return imported=0, already=True.
    reup_resp = client.post(
        "/api/v1/imports",
        data={"account_id": str(axis_account.id)},
        files={"file": ("statement.pdf", b"file-A", "application/pdf")},
    )
    assert reup_resp.status_code == 200
    body = reup_resp.json()
    assert body["imported"] == len(_REVIEW_ROWS)
    assert body["already_imported"] is False


def test_cancel_partial_keeps_batch_row(
    client: TestClient,
    axis_account: Account,
    stub_parser: type[_StubAxisParser],
    session_factory: sessionmaker[Session],
) -> None:
    batch_id = _import_and_get_batch_id(client, axis_account.id)

    # Commit only the income row; cancel will leave it on the board.
    candidates = client.get(f"/api/v1/imports/{batch_id}/candidates").json()
    income_id = next(c["id"] for c in candidates if c["transaction_type"] == "income")
    commit = client.post(
        f"/api/v1/imports/{batch_id}/commit",
        json={"transaction_ids": [income_id]},
    )
    assert commit.status_code == 204

    cancel = client.delete(f"/api/v1/imports/{batch_id}")
    assert cancel.status_code == 204

    with session_factory() as s:
        # ImportBatch row still exists because confirmed rows survive.
        assert s.get(ImportBatch, batch_id) is not None
        # Only the one confirmed row remains.
        rows = list(s.scalars(select(Transaction)))
        assert len(rows) == 1
        assert rows[0].id == income_id


def test_reupload_after_partial_cancel_resurfaces_cancelled_rows(
    client: TestClient,
    axis_account: Account,
    stub_parser: type[_StubAxisParser],
    session_factory: sessionmaker[Session],
) -> None:
    """Partial-cancel then re-upload: the cancelled rows re-surface on the batch.

    Pins the reconciliation behaviour at the route layer. Commit the income row,
    partial-cancel (deletes the 3 pending purchases, keeps the batch), then
    re-upload the same file: the 3 deleted rows return as pending on the same
    batch; the surviving confirmed income row is skipped as still-present.
    """
    batch_id = _import_and_get_batch_id(client, axis_account.id)

    candidates = client.get(f"/api/v1/imports/{batch_id}/candidates").json()
    income_id = next(c["id"] for c in candidates if c["transaction_type"] == "income")
    commit = client.post(
        f"/api/v1/imports/{batch_id}/commit",
        json={"transaction_ids": [income_id]},
    )
    assert commit.status_code == 204

    cancel = client.delete(f"/api/v1/imports/{batch_id}")
    assert cancel.status_code == 204

    reup = client.post(
        "/api/v1/imports",
        data={"account_id": str(axis_account.id)},
        files={"file": ("statement.pdf", b"file-A", "application/pdf")},
    )
    assert reup.status_code == 200, reup.text
    body = reup.json()
    n_purchases = len(_REVIEW_ROWS) - 1  # everything except the income row
    assert body == {
        "batch_id": batch_id,
        "imported": n_purchases,
        "skipped": 1,  # the surviving income row
        "already_imported": True,
        "pending_count": n_purchases,
        "duplicate_of_account_id": None,
        "duplicate_of_account_archived": False,
    }

    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(ImportBatch)) == 1
        # income (confirmed) + resurfaced purchases (pending) = all rows again.
        pending = list(s.scalars(select(Transaction).where(Transaction.confirmed_at.is_(None))))
        assert len(pending) == n_purchases


def test_cancel_full_then_second_cancel_returns_404(
    client: TestClient,
    axis_account: Account,
    stub_parser: type[_StubAxisParser],
) -> None:
    """Second cancel on a full-cancelled batch sees a missing batch row → 404.

    Not idempotent in the response-shape sense (204 then 404). It IS
    idempotent in effect (the state after two cancels is the same as after
    one). For the response-idempotent partial-cancel branch see
    :func:`test_partial_cancel_second_call_returns_204`.
    """
    batch_id = _import_and_get_batch_id(client, axis_account.id)

    first = client.delete(f"/api/v1/imports/{batch_id}")
    assert first.status_code == 204

    second = client.delete(f"/api/v1/imports/{batch_id}")
    assert second.status_code == 404


def test_partial_cancel_second_call_returns_204(
    client: TestClient,
    axis_account: Account,
    stub_parser: type[_StubAxisParser],
) -> None:
    """Partial cancel keeps the batch row → second cancel is a true 204 no-op."""
    batch_id = _import_and_get_batch_id(client, axis_account.id)

    # Commit the income row so the batch has a confirmed survivor.
    candidates = client.get(f"/api/v1/imports/{batch_id}/candidates").json()
    income_id = next(c["id"] for c in candidates if c["transaction_type"] == "income")
    commit = client.post(
        f"/api/v1/imports/{batch_id}/commit",
        json={"transaction_ids": [income_id]},
    )
    assert commit.status_code == 204

    first = client.delete(f"/api/v1/imports/{batch_id}")
    assert first.status_code == 204

    # Batch row still exists (income row survives). Second cancel is a no-op.
    second = client.delete(f"/api/v1/imports/{batch_id}")
    assert second.status_code == 204


def test_cancel_unknown_batch_returns_404(
    client: TestClient,
    seeded_user: User,
) -> None:
    resp = client.delete("/api/v1/imports/99999")
    assert resp.status_code == 404


# -----------------------------------------------------------------------------
# Sibling-batch enumeration + duplicate-id + record_tag failure + body validation.
# -----------------------------------------------------------------------------
def test_commit_rejects_sibling_batch_id(
    client: TestClient,
    seeded_user: User,
    session: Session,
    seeded_categories: list[Category],
    stub_parser: type[_StubAxisParser],
) -> None:
    """An id from a *sibling* batch (same user, different batch) must 422.

    Enumeration-hardening: the route never silently accepts an id that
    belongs to a different batch — even when the user owns both.
    """
    # Two Axis accounts, two imports → two batches.
    other = Account(
        user_id=seeded_user.id,
        name="Axis CC B",
        type="credit_card",
        issuer="axis",
        last4="5678",
    )
    session.add(other)
    session.commit()
    session.refresh(other)

    batch_a = _import_and_get_batch_id(client, other.id, content=b"file-A")

    # New file content to avoid the source_file_hash short-circuit on import B.
    batch_b = _import_and_get_batch_id(client, other.id, content=b"file-B-different")

    # Grab one id from batch_a and try to commit it under batch_b.
    candidates_a = client.get(f"/api/v1/imports/{batch_a}/candidates").json()
    sibling_id = candidates_a[0]["id"]

    commit = client.post(
        f"/api/v1/imports/{batch_b}/commit",
        json={"transaction_ids": [sibling_id]},
    )
    assert commit.status_code == 422
    assert commit.json()["detail"]["invalid_ids"] == [sibling_id]


def test_commit_duplicate_ids_dedupe_silently(
    client: TestClient,
    axis_account: Account,
    stub_parser: type[_StubAxisParser],
    session_factory: sessionmaker[Session],
) -> None:
    """``transaction_ids=[5, 5]`` commits row 5 once. Set-conversion is intentional."""
    batch_id = _import_and_get_batch_id(client, axis_account.id)
    candidates = client.get(f"/api/v1/imports/{batch_id}/candidates").json()
    income_id = next(c["id"] for c in candidates if c["transaction_type"] == "income")

    commit = client.post(
        f"/api/v1/imports/{batch_id}/commit",
        json={"transaction_ids": [income_id, income_id, income_id]},
    )
    assert commit.status_code == 204

    with session_factory() as s:
        confirmed = list(
            s.scalars(select(Transaction).where(Transaction.confirmed_at.is_not(None)))
        )
        assert len(confirmed) == 1
        # MerchantTagMap should not have a duplicate row either (income has null
        # category, so record_tag was skipped anyway — but the dedupe protects
        # the more general spend case from N×N writes).
        assert s.scalar(select(func.count()).select_from(MerchantTagMap)) == 0


def test_commit_learning_failure_rolls_back_atomically(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    stub_parser: type[_StubAxisParser],
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
) -> None:
    """Non-IntegrityError from the learning pass → no row gets confirmed_at
    stamped.

    Locks the "all or none" atomicity claim against a future refactor that
    might silently commit pass-1 UPDATEs before catching a pass-3 failure.
    Learning now flows through the shared ``learn_merchant_memory`` (one call
    per committed row), so we make its second invocation blow up.
    """
    batch_id = _import_and_get_batch_id(client, axis_account.id)
    food = next(c for c in seeded_categories if c.name == "Food")

    candidates = client.get(f"/api/v1/imports/{batch_id}/candidates").json()
    spend_ids = [c["id"] for c in candidates if c["transaction_type"] == "spend"]
    for tid in spend_ids:
        client.patch(f"/api/v1/transactions/{tid}", json={"category_id": food.id})

    # Make the second learn_merchant_memory call blow up with a non-IntegrityError.
    from app.api.v1 import imports as imports_module

    real_learn = imports_module.learn_merchant_memory
    call_count = {"n": 0}

    def boom(*args: object, **kwargs: object) -> None:
        call_count["n"] += 1
        if call_count["n"] >= 2:
            raise RuntimeError("simulated learning failure")
        real_learn(*args, **kwargs)

    monkeypatch.setattr(imports_module, "learn_merchant_memory", boom)

    # TestClient re-raises uncaught exceptions rather than returning 500;
    # wrap to verify the RuntimeError propagated *and* check post-state.
    with pytest.raises(RuntimeError, match="simulated learning failure"):
        client.post(
            f"/api/v1/imports/{batch_id}/commit",
            json={"transaction_ids": spend_ids},
        )

    # No partial state: no row got confirmed_at stamped. Implicit
    # Session.close() rollback in get_db reverts pass-1 UPDATEs.
    with session_factory() as s:
        confirmed = s.scalars(
            select(Transaction).where(Transaction.confirmed_at.is_not(None))
        ).all()
        assert confirmed == []


def test_commit_refund_with_null_category_rejected(
    client: TestClient,
    axis_account: Account,
    seeded_user: User,
    stub_parser: type[_StubAxisParser],
) -> None:
    """Untagged refund falls back to 422 when there's no "Other" category to
    default to (this test seeds no categories). Also guards that the null-category
    rule covers ``refund``, not just ``spend``."""
    _StubAxisParser.rows = [
        RawTransaction(
            date=date(2026, 3, 10),
            amount_paise=50000,
            merchant_raw="SWIGGY BLR REFUND",
            txn_type="refund",
        ),
    ]
    batch_id = _import_and_get_batch_id(client, axis_account.id)

    candidates = client.get(f"/api/v1/imports/{batch_id}/candidates").json()
    assert len(candidates) == 1
    assert candidates[0]["transaction_type"] == "refund"
    assert candidates[0]["category_id"] is None

    commit = client.post(
        f"/api/v1/imports/{batch_id}/commit",
        json={"transaction_ids": [candidates[0]["id"]]},
    )
    assert commit.status_code == 422
    assert commit.json()["detail"]["invalid_ids"] == [candidates[0]["id"]]


def test_import_commit_body_rejects_empty_id_list(
    client: TestClient,
    axis_account: Account,
    stub_parser: type[_StubAxisParser],
) -> None:
    batch_id = _import_and_get_batch_id(client, axis_account.id)
    resp = client.post(
        f"/api/v1/imports/{batch_id}/commit",
        json={"transaction_ids": []},
    )
    assert resp.status_code == 422


def test_import_commit_body_rejects_unknown_field(
    client: TestClient,
    axis_account: Account,
    stub_parser: type[_StubAxisParser],
) -> None:
    """``extra='forbid'`` rejects typos like ``transaction_id`` (missing s)."""
    batch_id = _import_and_get_batch_id(client, axis_account.id)
    resp = client.post(
        f"/api/v1/imports/{batch_id}/commit",
        json={"transaction_ids": [1], "unknown_field": "x"},
    )
    assert resp.status_code == 422


# -----------------------------------------------------------------------------
# GET /imports/pending — notification-bell feed.
# -----------------------------------------------------------------------------
def test_pending_empty_when_no_imports(
    client: TestClient,
    seeded_user: User,
) -> None:
    resp = client.get("/api/v1/imports/pending")
    assert resp.status_code == 200
    assert resp.json() == []


def test_pending_lists_open_batch_with_count_and_label(
    client: TestClient,
    axis_account: Account,
    stub_parser: type[_StubAxisParser],
) -> None:
    batch_id = _import_and_get_batch_id(client, axis_account.id)

    resp = client.get("/api/v1/imports/pending")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0] == {
        "batch_id": batch_id,
        "account_name": "Axis CC",
        "account_last4": "1234",
        "pending_count": len(_REVIEW_ROWS),
    }


def test_pending_drops_batch_once_all_rows_committed(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    stub_parser: type[_StubAxisParser],
) -> None:
    """Committing every row clears the batch."""
    batch_id = _import_and_get_batch_id(client, axis_account.id)
    food = next(c for c in seeded_categories if c.name == "Food")

    candidates = client.get(f"/api/v1/imports/{batch_id}/candidates").json()
    # Tag the spends to Food (they'd otherwise default to "Other" on commit).
    _tag_spend_rows_to_food(client, candidates, food.id)
    commit = client.post(
        f"/api/v1/imports/{batch_id}/commit",
        json={"transaction_ids": [c["id"] for c in candidates]},
    )
    assert commit.status_code == 204, commit.text

    assert client.get("/api/v1/imports/pending").json() == []


def test_pending_partial_commit_keeps_batch_with_reduced_count(
    client: TestClient,
    axis_account: Account,
    stub_parser: type[_StubAxisParser],
) -> None:
    batch_id = _import_and_get_batch_id(client, axis_account.id)
    candidates = client.get(f"/api/v1/imports/{batch_id}/candidates").json()
    income_id = next(c["id"] for c in candidates if c["transaction_type"] == "income")

    commit = client.post(
        f"/api/v1/imports/{batch_id}/commit",
        json={"transaction_ids": [income_id]},
    )
    assert commit.status_code == 204

    rows = client.get("/api/v1/imports/pending").json()
    assert len(rows) == 1
    assert rows[0]["pending_count"] == len(_REVIEW_ROWS) - 1


def test_pending_drops_batch_after_cancel(
    client: TestClient,
    axis_account: Account,
    stub_parser: type[_StubAxisParser],
) -> None:
    batch_id = _import_and_get_batch_id(client, axis_account.id)
    assert len(client.get("/api/v1/imports/pending").json()) == 1

    cancel = client.delete(f"/api/v1/imports/{batch_id}")
    assert cancel.status_code == 204

    assert client.get("/api/v1/imports/pending").json() == []


def test_pending_isolated_to_current_user(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    """Another user's open batch + pending row must not leak into this user's feed."""
    other_user = User(id=uuid.UUID("00000000-0000-0000-0000-000000000098"))
    session.add(other_user)
    session.commit()
    other_account = Account(
        user_id=other_user.id,
        name="Other CC",
        type="credit_card",
        issuer="axis",
        last4="9999",
    )
    session.add(other_account)
    session.commit()
    session.refresh(other_account)

    batch = ImportBatch(
        user_id=other_user.id,
        account_id=other_account.id,
        source_file_hash="other-pending-hash",
        parser_name="AxisCC",
        status="pending",
    )
    session.add(batch)
    session.commit()
    session.refresh(batch)
    session.add(
        Transaction(
            user_id=other_user.id,
            account_id=other_account.id,
            date=date(2026, 3, 5),
            amount_paise=-15000,
            transaction_type="spend",
            merchant_raw="OTHER SPEND",
            merchant_normalized="other spend",
            fingerprint="other-user-fp",
            source="import",
            import_batch_id=batch.id,
            confirmed_at=None,
        )
    )
    session.commit()

    # v1 single-user client acts as seeded_user; the other user's batch is invisible.
    assert client.get("/api/v1/imports/pending").json() == []


def test_pending_never_labels_a_batch_with_another_users_account(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    """OUR batch pointing at THEIR account must render a null label, not their PII.

    ``import_batches.account_id`` is a plain ``ForeignKey("accounts.id")``
    (import_batch.py:41), unlike the owned-link tables' ADR-0002 composite FKs, so the
    DB permits this row — which is exactly why the read has to restate the predicate.
    This is the only one of the three such reads that returns ``Account.last4``.
    """
    other_user = User(id=uuid.UUID("00000000-0000-0000-0000-000000000097"))
    session.add(other_user)
    session.commit()
    their_account = Account(
        user_id=other_user.id,
        name="Their Secret Card",
        type="credit_card",
        issuer="axis",
        last4="4321",
    )
    session.add(their_account)
    session.commit()
    session.refresh(their_account)

    batch = ImportBatch(
        user_id=seeded_user.id,  # our batch...
        account_id=their_account.id,  # ...pointing at their account
        source_file_hash="cross-user-account-hash",
        parser_name="AxisCC",
        status="pending",
    )
    session.add(batch)
    session.commit()
    session.refresh(batch)
    session.add(
        Transaction(
            user_id=seeded_user.id,
            account_id=their_account.id,
            date=date(2026, 3, 6),
            amount_paise=-15000,
            transaction_type="spend",
            merchant_raw="SOME SPEND",
            merchant_normalized="some spend",
            fingerprint="cross-user-account-fp",
            source="import",
            import_batch_id=batch.id,
            confirmed_at=None,
        )
    )
    session.commit()

    rows = client.get("/api/v1/imports/pending").json()
    assert len(rows) == 1  # the batch is ours, so it still appears
    assert rows[0]["batch_id"] == batch.id
    assert rows[0]["account_name"] is None
    assert rows[0]["account_last4"] is None  # never "4321"


def test_pending_still_lists_an_account_less_batch(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    """The user predicate belongs in the outerjoin's ON clause, not the WHERE.

    ``import_batches.account_id`` is nullable — backup restore builds its BATCH with
    ``account_id=None`` (backup_import_service.py:103) while resolving a real account
    per row, and inserts each row with the CSV's ``confirmed_at``, typed
    ``datetime | None`` (backup_csv.py:118). So an unconfirmed restore lands in this
    feed with rows to count and no account to label it. Putting
    ``Account.user_id == user_id`` in the WHERE would collapse the LEFT JOIN to an
    inner one and drop the batch entirely. This is the guard for that.
    """
    own_account = Account(
        user_id=seeded_user.id,
        name="My Bank",
        type="bank",
        issuer="axis",
        last4="0011",
    )
    session.add(own_account)
    session.commit()
    session.refresh(own_account)

    batch = ImportBatch(
        user_id=seeded_user.id,
        account_id=None,
        source_file_hash="account-less-hash",
        parser_name="backup_csv",
        status="pending",
    )
    session.add(batch)
    session.commit()
    session.refresh(batch)
    session.add(
        Transaction(
            user_id=seeded_user.id,
            account_id=own_account.id,
            date=date(2026, 3, 7),
            amount_paise=-2500,
            transaction_type="spend",
            merchant_raw="RESTORED SPEND",
            merchant_normalized="restored spend",
            fingerprint="account-less-fp",
            source="import",
            import_batch_id=batch.id,
            confirmed_at=None,
        )
    )
    session.commit()

    rows = client.get("/api/v1/imports/pending").json()
    assert len(rows) == 1
    assert rows[0]["batch_id"] == batch.id
    assert rows[0]["account_name"] is None
    assert rows[0]["pending_count"] == 1


# -----------------------------------------------------------------------------
# F2 manual entry — landed on board immediately (verifies confirmed_at stamp).
# -----------------------------------------------------------------------------
def test_f2_manual_entry_appears_on_board_immediately(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
) -> None:
    """POST /transactions stamps confirmed_at on insert → row visible via GET."""
    food = next(c for c in seeded_categories if c.name == "Food")
    create = client.post(
        "/api/v1/transactions",
        json={
            "date": "2026-04-01",
            "account_id": axis_account.id,
            "amount_paise": -42000,
            "transaction_type": "spend",
            "merchant_raw": "Hand-entered cafe",
            "category_id": food.id,
        },
    )
    assert create.status_code == 201, create.text
    created_id = create.json()["id"]

    board = client.get(f"/api/v1/transactions?account_id={axis_account.id}").json()
    assert any(row["id"] == created_id for row in board), (
        "F2 manual entry should appear on the board without a commit step"
    )


# -----------------------------------------------------------------------------
# F4a-1 CC-bill auto-link — reverse-flow integration through POST /commit.
# -----------------------------------------------------------------------------


# Single-row stub fixture so date/amount don't collide with _REVIEW_ROWS — we
# need a unique sentinel pair the test fully owns.
_F4A_CC_DATE = date(2026, 4, 10)
_F4A_AMOUNT = 750_000

_F4A_ROWS: list[RawTransaction] = [
    RawTransaction(
        date=_F4A_CC_DATE,
        amount_paise=_F4A_AMOUNT,
        merchant_raw="PAYMENT RECEIVED THANK YOU",
        txn_type="payment",
    ),
]


def test_commit_f4a_links_cc_payment_to_manual_bank_transfer(
    client: TestClient,
    seeded_user: User,
    axis_account: Account,
    stub_parser: type[_StubAxisParser],
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """Reverse-flow: bank-side row via F2 first, then CC import + commit auto-links.

    Locks the full pass-1 → pass-2 (F4a) → pass-3 commit pipeline end-to-end.
    Bank-side row arrives via POST /transactions (not direct ORM) so the F2
    confirmed_at-stamp path is exercised too. The CC parser is the existing
    stub_parser fixture; we override its row list with a one-row local
    sentinel so this test fully owns its date/amount and doesn't entangle
    with the shared ``_REVIEW_ROWS`` constant.
    """
    _StubAxisParser.rows = list(_F4A_ROWS)

    # Bank account + parent link on the CC. Seed directly — the PATCH route
    # is already covered by tests/api/test_accounts.py; this test focuses on
    # the F4a auto-link itself.
    bank = Account(
        user_id=seeded_user.id,
        name="HDFC Bank",
        type="bank",
        issuer="hdfc",
    )
    session.add(bank)
    session.flush()
    axis_account.parent_account_id = bank.id
    session.commit()

    # Bank-side row via F2 manual POST — one day before the CC payment date.
    bank_post = client.post(
        "/api/v1/transactions",
        json={
            "date": (_F4A_CC_DATE - timedelta(days=1)).isoformat(),
            "account_id": bank.id,
            "amount_paise": -_F4A_AMOUNT,
            "transaction_type": "transfer",
            "merchant_raw": "Axis CC bill",
        },
    )
    assert bank_post.status_code == 201, bank_post.text
    bank_id = bank_post.json()["id"]

    # Import the CC statement → candidate is type=income (parser "payment"
    # folds to "income" per import_service._map_type).
    batch_id = _import_and_get_batch_id(client, axis_account.id)
    candidates = client.get(f"/api/v1/imports/{batch_id}/candidates").json()
    assert len(candidates) == 1
    cc_candidate = candidates[0]
    assert cc_candidate["transaction_type"] == "income"
    cc_id = cc_candidate["id"]

    # Commit. Pass-2 (F4a) should fire BEFORE pass-3 (record_tag) and link.
    commit = client.post(
        f"/api/v1/imports/{batch_id}/commit",
        json={"transaction_ids": [cc_id]},
    )
    assert commit.status_code == 204, commit.text

    # Direct DB read — TransactionRead doesn't expose transfer_pair_id.
    with session_factory() as s:
        cc_after = s.get(Transaction, cc_id)
        bank_after = s.get(Transaction, bank_id)
        assert cc_after is not None and bank_after is not None
        # Symmetric pair.
        assert cc_after.transfer_pair_id == bank_after.id
        assert bank_after.transfer_pair_id == cc_after.id
        # Both flipped to transfer.
        assert cc_after.transaction_type == "transfer"
        assert bank_after.transaction_type == "transfer"


def test_commit_f4a_pair_write_conflict_aborts_the_whole_batch(
    client: TestClient,
    seeded_user: User,
    axis_account: Account,
    stub_parser: type[_StubAxisParser],
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """A constraint failure on the pass-2 pair write is NOT swallowed.

    Fault injection by necessity: the branch is unreachable through the public
    surface. Both pair rows come from one ``user_id``-scoped SELECT, and
    ``ck_transactions_no_self_pair`` cannot fire because the CC row is ``income``
    while candidates must be ``spend``/``transfer``. So the only way to observe
    the handler's behaviour is to make the flush raise.

    The listener predicate is load-bearing: nothing in this session is dirty with
    a non-null ``transfer_pair_id`` until pass 2 sets one, so the injected error
    can only land on the pair write. Do NOT relax it to "any dirty Transaction" —
    pass 1's ``confirmed_at`` flush would then trip it and the test would pass for
    the wrong reason.

    Asserts the all-or-none post-state, the same contract
    ``test_commit_learning_failure_rolls_back_atomically`` locks for pass 3: the
    savepoint rolls back the partial pair, the exception propagates, and
    ``get_db``'s close() reverts pass 1 — so no row keeps ``confirmed_at``.
    """
    _StubAxisParser.rows = list(_F4A_ROWS)

    bank = Account(
        user_id=seeded_user.id,
        name="HDFC Bank",
        type="bank",
        issuer="hdfc",
    )
    session.add(bank)
    session.flush()
    axis_account.parent_account_id = bank.id
    session.commit()

    # Bank-side leg via plain POST /transactions — deliberately NOT the transfer
    # endpoint, which would itself write a transfer_pair_id and trip the listener.
    bank_post = client.post(
        "/api/v1/transactions",
        json={
            "date": (_F4A_CC_DATE - timedelta(days=1)).isoformat(),
            "account_id": bank.id,
            "amount_paise": -_F4A_AMOUNT,
            "transaction_type": "transfer",
            "merchant_raw": "Axis CC bill",
        },
    )
    assert bank_post.status_code == 201, bank_post.text

    batch_id = _import_and_get_batch_id(client, axis_account.id)
    candidates = client.get(f"/api/v1/imports/{batch_id}/candidates").json()
    assert len(candidates) == 1
    cc_id = candidates[0]["id"]

    fired = {"n": 0}

    def _fail_the_pair_write(sess: Session, _flush_context: object, _instances: object) -> None:
        if fired["n"]:
            return
        if any(
            isinstance(obj, Transaction) and obj.transfer_pair_id is not None for obj in sess.dirty
        ):
            fired["n"] += 1
            raise IntegrityError("simulated pair conflict", None, Exception("forced"))

    event.listen(Session, "before_flush", _fail_the_pair_write)
    try:
        # TestClient re-raises uncaught exceptions rather than returning the 500.
        with pytest.raises(IntegrityError):
            client.post(
                f"/api/v1/imports/{batch_id}/commit",
                json={"transaction_ids": [cc_id]},
            )
    finally:
        event.remove(Session, "before_flush", _fail_the_pair_write)

    assert fired["n"] == 1, "the injected conflict never reached the pair write"

    # All-or-none: pass 1's confirmed_at stamps are gone with the request.
    with session_factory() as s:
        cc_after = s.get(Transaction, cc_id)
        assert cc_after is not None
        assert cc_after.confirmed_at is None
        assert cc_after.transfer_pair_id is None
        assert cc_after.transaction_type == "income"


def test_commit_f4a_does_not_learn_tag_for_flipped_row(
    client: TestClient,
    seeded_user: User,
    axis_account: Account,
    seeded_categories: list[Category],
    stub_parser: type[_StubAxisParser],
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """Income/transfer never learn a tag — neither at PATCH time nor at commit.

    The 'PAYMENT RECEIVED' CC row is pre-PATCHed with a category in the review
    queue. That PATCH no-ops for learning (the row is 'income', not in
    AUTO_TAGGABLE_TYPES). At commit, pass-2 flips its type to 'transfer' and
    pass-3's gate excludes it again. Net: no merchant_tag_map row for that
    merchant — the type gate holds on both the manual-PATCH and import-commit
    paths. This asserts the type gate only, not pass ordering: the row is
    excluded whether read as income (pre-flip) or transfer (post-flip).
    """
    _StubAxisParser.rows = list(_F4A_ROWS)

    bank = Account(
        user_id=seeded_user.id,
        name="HDFC Bank",
        type="bank",
        issuer="hdfc",
    )
    session.add(bank)
    session.flush()
    axis_account.parent_account_id = bank.id
    session.commit()

    # Bank-side row first (so pass-2 finds a match and flips the CC row).
    client.post(
        "/api/v1/transactions",
        json={
            "date": _F4A_CC_DATE.isoformat(),
            "account_id": bank.id,
            "amount_paise": -_F4A_AMOUNT,
            "transaction_type": "transfer",
            "merchant_raw": "Axis CC bill",
        },
    )

    # An income-kind category is the only valid pre-tag for the income CC-payment
    # row (kind is now API-enforced). Its kind is irrelevant to what this test
    # asserts — that income/transfer never *learn* — but it must be a valid
    # assignment so the PATCH itself succeeds.
    income_cat = Category(user_id=seeded_user.id, name="Salary", kind="income", is_seeded=False)
    session.add(income_cat)
    session.commit()
    session.refresh(income_cat)

    batch_id = _import_and_get_batch_id(client, axis_account.id)
    candidates = client.get(f"/api/v1/imports/{batch_id}/candidates").json()
    cc_id = candidates[0]["id"]

    # Pre-PATCH a category onto the CC row — simulates the user tagging
    # the row in the review queue before commit.
    patch = client.patch(
        f"/api/v1/transactions/{cc_id}",
        json={"category_id": income_cat.id},
    )
    assert patch.status_code == 200, patch.text

    commit = client.post(
        f"/api/v1/imports/{batch_id}/commit",
        json={"transaction_ids": [cc_id]},
    )
    assert commit.status_code == 204, commit.text

    # The merchant_tag_map must not contain a row for 'payment received ...'.
    with session_factory() as s:
        rows = list(s.scalars(select(MerchantTagMap)))
        # Manual entry is type-gated now: the pre-PATCH onto the CC 'PAYMENT
        # RECEIVED' row no-ops because it's income at PATCH time
        # (AUTO_TAGGABLE_TYPES), so nothing is written there; pass-2 flips it to
        # 'transfer' and pass-3 skips it as well. Net: zero rows for the merchant.
        f4a_rows = [r for r in rows if "payment received" in r.merchant_normalized]
        assert f4a_rows == [], (
            "income/transfer never learn: the income-time PATCH no-op plus the "
            "pass-3 skip should leave zero tag-map rows for the CC-payment merchant"
        )


# -----------------------------------------------------------------------------
# Cross-import learning lifecycle (end-to-end).
#
# The slice tests above prove learning (record_tag at commit) and prefill
# (prefetch_tag_map at import) SEPARATELY. These chain them through the real HTTP
# endpoints across multiple imports — the actual "auto-tagging works" contract.
# Each import uses distinct file bytes so it lands as its own batch (identical
# bytes reconcile onto the prior completed batch); each re-imported same-merchant
# row uses a fresh date so it survives the date+amount+merchant+account
# fingerprint dedup. Every taggable row is an explicit `purchase` (→ spend), so a
# "nothing learned" assertion can't pass for the wrong (type-gated) reason.
# -----------------------------------------------------------------------------


def _purchase(merchant: str, txn_date: date, *, amount_paise: int = -15000) -> RawTransaction:
    """A spend row for the stub parser (txn_type='purchase' → 'spend')."""
    return RawTransaction(
        date=txn_date,
        amount_paise=amount_paise,
        merchant_raw=merchant,
        txn_type="purchase",
    )


def test_e2e_learn_then_prefill_and_passive_accept(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    stub_parser: type[_StubAxisParser],
    session_factory: sessionmaker[Session],
) -> None:
    """Import a brand-new merchant → tag + commit (learn) → re-import → the row
    arrives auto-tagged with real confidence (Scenario 1); then commit the
    prefilled row unchanged → the passive accept still bumps hit_count (Scenario 2).

    Scenarios 1 and 2 share one function because the DB fixtures are function-
    scoped (a fresh in-memory DB per test), so 2 cannot "continue" as its own test.
    """
    food = next(c for c in seeded_categories if c.name == "Food")

    # --- Scenario 1: learn from a new merchant, then prefill on re-import. ---
    # Batch A: three SWIGGY purchases, brand-new merchant (no prior map).
    _StubAxisParser.rows = [
        _purchase("SWIGGY BLR", date(2026, 3, 5)),
        _purchase("SWIGGY BLR", date(2026, 3, 6), amount_paise=-22000),
        _purchase("SWIGGY BLR", date(2026, 3, 7), amount_paise=-9500),
    ]
    batch_a = _import_and_get_batch_id(client, axis_account.id, content=b"file-A")

    cand_a = client.get(f"/api/v1/imports/{batch_a}/candidates").json()
    swiggy_a = [c for c in cand_a if "SWIGGY" in c["merchant_raw"]]
    assert len(swiggy_a) == 3
    for c in swiggy_a:
        assert c["category_id"] is None
        assert c["prior_matches"] == 0
        assert c["confidence"] == "none"

    # Tag all three to Food, then commit → learn swiggy blr → Food (one bump each).
    for c in swiggy_a:
        patch = client.patch(f"/api/v1/transactions/{c['id']}", json={"category_id": food.id})
        assert patch.status_code == 200, patch.text
    commit_a = client.post(
        f"/api/v1/imports/{batch_a}/commit",
        json={"transaction_ids": [c["id"] for c in swiggy_a]},
    )
    assert commit_a.status_code == 204, commit_a.text

    with session_factory() as s:
        tag = s.scalar(
            select(MerchantTagMap).where(
                MerchantTagMap.merchant_normalized == "swiggy blr",
                MerchantTagMap.category_id == food.id,
            )
        )
        assert tag is not None
        assert tag.hit_count == 3  # three committed rows, one bump each

    # Batch B: same merchant, fresh date (survives dedup), distinct bytes (own batch).
    _StubAxisParser.rows = [_purchase("SWIGGY BLR", date(2026, 4, 10))]
    batch_b = _import_and_get_batch_id(client, axis_account.id, content=b"file-B")

    cand_b = client.get(f"/api/v1/imports/{batch_b}/candidates").json()
    swiggy_b = next(c for c in cand_b if "SWIGGY" in c["merchant_raw"])
    assert swiggy_b["category_id"] == food.id  # prefilled from the earned rule
    assert swiggy_b["prior_matches"] == 3
    assert swiggy_b["confidence"] == "confident"

    with session_factory() as s:
        row = s.get(Transaction, swiggy_b["id"])
        assert row is not None
        assert row.auto_category_id == food.id  # frozen suggestion for the metric

    # --- Scenario 2: commit the prefilled row unchanged → passive accept teaches. ---
    commit_b = client.post(
        f"/api/v1/imports/{batch_b}/commit",
        json={"transaction_ids": [swiggy_b["id"]]},
    )
    assert commit_b.status_code == 204, commit_b.text

    with session_factory() as s:
        tag = s.scalar(
            select(MerchantTagMap).where(
                MerchantTagMap.merchant_normalized == "swiggy blr",
                MerchantTagMap.category_id == food.id,
            )
        )
        assert tag is not None
        # 3 → 4: a passive accept still teaches. (A prefill regression would have
        # defaulted this row to Other and skipped pass-3 learning → stuck at 3, so
        # this also implicitly guards the prefill read path.)
        assert tag.hit_count == 4


def test_e2e_discarded_pending_row_does_not_teach(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    stub_parser: type[_StubAxisParser],
    session_factory: sessionmaker[Session],
) -> None:
    """A pending row tagged then discarded (never committed) writes no rule, so a
    later import of the same merchant is NOT prefilled — the #2 fix, end-to-end."""
    food = next(c for c in seeded_categories if c.name == "Food")

    _StubAxisParser.rows = [_purchase("NETFLIX", date(2026, 3, 5))]
    batch_a = _import_and_get_batch_id(client, axis_account.id, content=b"file-A")

    cand_a = client.get(f"/api/v1/imports/{batch_a}/candidates").json()
    netflix = next(c for c in cand_a if "NETFLIX" in c["merchant_raw"])
    # Self-documents the type guard: this row IS spend, so "nothing learned" below
    # can only be the pending-PATCH refusal, not AUTO_TAGGABLE_TYPES gating it out.
    assert netflix["transaction_type"] == "spend"
    patch = client.patch(f"/api/v1/transactions/{netflix['id']}", json={"category_id": food.id})
    assert patch.status_code == 200, patch.text

    # Discard the pending batch without committing.
    assert client.delete(f"/api/v1/imports/{batch_a}").status_code == 204

    # Re-import the same merchant (fresh date, distinct bytes).
    _StubAxisParser.rows = [_purchase("NETFLIX", date(2026, 4, 10))]
    batch_b = _import_and_get_batch_id(client, axis_account.id, content=b"file-B")

    cand_b = client.get(f"/api/v1/imports/{batch_b}/candidates").json()
    netflix_b = next(c for c in cand_b if "NETFLIX" in c["merchant_raw"])
    assert netflix_b["category_id"] is None  # NOT prefilled — no orphan rule
    assert netflix_b["prior_matches"] == 0
    assert netflix_b["confidence"] == "none"

    with session_factory() as s:
        assert (
            s.scalar(select(MerchantTagMap).where(MerchantTagMap.merchant_normalized == "netflix"))
            is None
        )


def test_e2e_user_correction_flips_winner_across_imports(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    stub_parser: type[_StubAxisParser],
    session_factory: sessionmaker[Session],
) -> None:
    """A user correction propagates and flips the winning suggestion across imports:
    after re-tagging a prefilled X to Y enough to outweigh X, the next import of the
    same merchant prefills Y. X = Shopping, Y = Food."""
    shopping = next(c for c in seeded_categories if c.name == "Shopping")  # X
    food = next(c for c in seeded_categories if c.name == "Food")  # Y

    # Batch A: establish AMAZON → Shopping (hit_count 1).
    _StubAxisParser.rows = [_purchase("AMAZON", date(2026, 3, 5))]
    batch_a = _import_and_get_batch_id(client, axis_account.id, content=b"file-A")
    amazon_a = next(
        c
        for c in client.get(f"/api/v1/imports/{batch_a}/candidates").json()
        if "AMAZON" in c["merchant_raw"]
    )
    assert (
        client.patch(
            f"/api/v1/transactions/{amazon_a['id']}", json={"category_id": shopping.id}
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/v1/imports/{batch_a}/commit",
            json={"transaction_ids": [amazon_a["id"]]},
        ).status_code
        == 204
    )

    # Batch B: two AMAZON rows arrive prefilled Shopping; correct BOTH to Food.
    _StubAxisParser.rows = [
        _purchase("AMAZON", date(2026, 4, 10)),
        _purchase("AMAZON", date(2026, 4, 11)),
    ]
    batch_b = _import_and_get_batch_id(client, axis_account.id, content=b"file-B")
    amazon_b = [
        c
        for c in client.get(f"/api/v1/imports/{batch_b}/candidates").json()
        if "AMAZON" in c["merchant_raw"]
    ]
    assert len(amazon_b) == 2
    for c in amazon_b:
        assert c["category_id"] == shopping.id  # prefilled X
        assert c["prior_matches"] == 1
        assert (
            client.patch(
                f"/api/v1/transactions/{c['id']}", json={"category_id": food.id}
            ).status_code
            == 200
        )
    assert (
        client.post(
            f"/api/v1/imports/{batch_b}/commit",
            json={"transaction_ids": [c["id"] for c in amazon_b]},
        ).status_code
        == 204
    )

    with session_factory() as s:
        food_rule = s.scalar(
            select(MerchantTagMap).where(
                MerchantTagMap.merchant_normalized == "amazon",
                MerchantTagMap.category_id == food.id,
            )
        )
        shop_rule = s.scalar(
            select(MerchantTagMap).where(
                MerchantTagMap.merchant_normalized == "amazon",
                MerchantTagMap.category_id == shopping.id,
            )
        )
        assert food_rule is not None and food_rule.hit_count == 2  # strict winner
        assert shop_rule is not None and shop_rule.hit_count == 1

    # Batch C: the winner has flipped X → Y.
    _StubAxisParser.rows = [_purchase("AMAZON", date(2026, 5, 1))]
    batch_c = _import_and_get_batch_id(client, axis_account.id, content=b"file-C")
    amazon_c = next(
        c
        for c in client.get(f"/api/v1/imports/{batch_c}/candidates").json()
        if "AMAZON" in c["merchant_raw"]
    )
    assert amazon_c["category_id"] == food.id  # flipped to Y
    assert amazon_c["prior_matches"] == 2

    with session_factory() as s:
        row = s.get(Transaction, amazon_c["id"])
        assert row is not None
        assert row.auto_category_id == food.id


def test_reupload_after_editing_a_committed_row_restages_only_the_cancelled_rows(
    client: TestClient,
    axis_account: Account,
    stub_parser: type[_StubAxisParser],
    session_factory: sessionmaker[Session],
) -> None:
    """ADR-0007 §Verification 1, end to end over HTTP — the test the widening is unsafe without.

    Import → commit one row, cancel the rest → PATCH an identity field on the
    committed row → re-import the same file. The edited row must NOT re-stage, and
    the cancelled rows must.

    Without ``origin_fingerprint`` the DB would hold ``fp'`` while the file still
    yields ``fp``, so the corrected row would come back wearing its original wrong
    value — visually indistinguishable in the queue from a deliberately-cancelled
    one. Committing that gives two rows for one real transaction whose fingerprints
    differ *by construction*, so F4 can never detect the duplicate and signed sums
    double-count. That is strictly worse than the delete case, and the likeliest
    everyday edit (cleaning up an unreadable UPI descriptor) is exactly its trigger.
    """
    batch_id = _import_and_get_batch_id(client, axis_account.id)

    candidates = client.get(f"/api/v1/imports/{batch_id}/candidates").json()
    income_id = next(c["id"] for c in candidates if c["transaction_type"] == "income")
    assert (
        client.post(
            f"/api/v1/imports/{batch_id}/commit", json={"transaction_ids": [income_id]}
        ).status_code
        == 204
    )
    assert client.delete(f"/api/v1/imports/{batch_id}").status_code == 204

    # The user corrects the committed row's amount on the board.
    patched = client.patch(f"/api/v1/transactions/{income_id}", json={"amount_paise": 512345})
    assert patched.status_code == 200, patched.text
    with session_factory() as s:
        edited = s.get(Transaction, income_id)
        assert edited is not None
        # Identity moved; provenance did not. That gap is the whole mechanism.
        assert edited.fingerprint != edited.origin_fingerprint

    reup = client.post(
        "/api/v1/imports",
        data={"account_id": str(axis_account.id)},
        files={"file": ("statement.pdf", b"file-A", "application/pdf")},
    )
    assert reup.status_code == 200, reup.text
    n_purchases = len(_REVIEW_ROWS) - 1
    body = reup.json()
    # The three cancelled purchases come back; the edited income row does not.
    assert body["imported"] == n_purchases
    assert body["skipped"] == 1
    assert body["pending_count"] == n_purchases

    with session_factory() as s:
        rows = list(s.scalars(select(Transaction)))
        assert len(rows) == len(_REVIEW_ROWS)  # no duplicate of the edited row
        assert [r.amount_paise for r in rows if r.confirmed_at is not None] == [512345]
