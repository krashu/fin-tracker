"""End-to-end tests for the import review/commit lifecycle (PRD §F1 step 5).

Covers:

* Post-import row state — ``confirmed_at IS NULL``.
* ``GET /imports/{batch_id}/candidates`` — pending filter, ``prior_matches``
  COALESCE, confidence threshold boundaries, cross-user 404.
* ``POST /imports/{batch_id}/commit`` — happy path, atomic 422 (missing /
  cross-batch / already-confirmed), untagged spend-kind (spend + refund) →
  "Other" default (with 422 fallback when it doesn't exist),
  income/transfer null-category branch, same-merchant-twice ``hit_count``
  accounting.
* ``DELETE /imports/{batch_id}`` — partial cancel keeps batch, full cancel
  deletes batch (re-upload re-runs parser), idempotency, cross-user 404.

Uses the ``_StubAxisParser`` pattern from ``test_imports.py`` — no real PDF.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
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
    MerchantAlias,
    MerchantTagMap,
    Transaction,
    User,
)
from app.parsers import HdfcBank, ParsedStatement, RawTransaction, StatementSummary
from app.services import import_service
from app.services.category_service import CASHBACK_CATEGORY_NAME, FALLBACK_CATEGORY_NAME
from app.services.merchant import normalize_merchant
from tests.fk_bypass import foreign_keys_off

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
    # Defaults to an all-None summary (no balance block) — the reconciliation
    # tests below override this per-case; every other test in this module is
    # unaffected since reset happens in the stub_parser fixture.
    summary: ClassVar[StatementSummary] = StatementSummary()

    @classmethod
    def parse(cls, pdf_bytes: bytes, password: str | None) -> ParsedStatement:
        return ParsedStatement(rows=list(cls.rows), summary=cls.summary)


@pytest.fixture
def stub_parser(monkeypatch: pytest.MonkeyPatch) -> type[_StubAxisParser]:
    monkeypatch.setitem(import_service.PARSERS, ("axis", "credit_card"), _StubAxisParser)
    _StubAxisParser.rows = list(_REVIEW_ROWS)
    _StubAxisParser.summary = StatementSummary()
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
    # Wire shape — same as TransactionRead + prior_matches + confidence + pinned
    # + cc_payment_candidate + uncategorised_commits_as_transfer.
    assert set(candidates[0].keys()) == {
        "id",
        "account_id",
        "date",
        "amount_paise",
        "transaction_type",
        "merchant_raw",
        "category_id",
        "category_name",
        "category_parent_name",
        "transfer_pair_id",
        "refund_of_id",
        "labels",
        "card_bill_link_offered",
        "prior_matches",
        "confidence",
        "pinned",
        "cc_payment_candidate",
        "uncategorised_commits_as_transfer",
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


def test_candidates_cc_payment_candidate_flag(
    client: TestClient,
    axis_account: Account,
    stub_parser: type[_StubAxisParser],
) -> None:
    """``cc_payment_candidate`` is the type+merchant gate ``is_cc_payment`` uses
    (``auto_link_cc_bill``'s gate 3), surfaced so the review queue can offer the
    "Card bill payment" action (PRD §F4a-1). True only for an income row whose
    merchant matches — a cashback credit (a positive spend, ADR-0013), an
    ordinary spend, and an income row with an unrelated merchant must all read
    False.
    """
    _StubAxisParser.rows = [
        # income (payment) + merchant match -> True.
        RawTransaction(
            date=date(2026, 3, 10),
            amount_paise=500000,
            merchant_raw="PAYMENT RECEIVED",
            txn_type="payment",
        ),
        # cashback is a positive spend, and not a card-bill merchant -> False.
        RawTransaction(
            date=date(2026, 3, 9),
            amount_paise=17500,
            merchant_raw="CASHBACK FOR SWIGGY TRANSACTIONS",
            txn_type="other",
        ),
        # ordinary spend, merchant text is irrelevant -> False (type gate).
        RawTransaction(
            date=date(2026, 3, 8),
            amount_paise=-15000,
            merchant_raw="SWIGGY BLR",
            txn_type="purchase",
        ),
        # income (payment) but an unrelated merchant -> False (merchant gate).
        # Only "payment" reaches `income` here — _map_type folds every other
        # positive credit to `spend` (a refund or cashback).
        RawTransaction(
            date=date(2026, 3, 7),
            amount_paise=250000,
            merchant_raw="SALARY CREDITED VIA NEFT",
            txn_type="payment",
        ),
    ]
    batch_id = _import_and_get_batch_id(client, axis_account.id)

    candidates = {
        c["merchant_raw"]: c for c in client.get(f"/api/v1/imports/{batch_id}/candidates").json()
    }
    assert candidates["PAYMENT RECEIVED"]["transaction_type"] == "income"
    assert candidates["PAYMENT RECEIVED"]["cc_payment_candidate"] is True
    assert candidates["CASHBACK FOR SWIGGY TRANSACTIONS"]["transaction_type"] == "spend"
    assert candidates["CASHBACK FOR SWIGGY TRANSACTIONS"]["cc_payment_candidate"] is False
    assert candidates["SWIGGY BLR"]["transaction_type"] == "spend"
    assert candidates["SWIGGY BLR"]["cc_payment_candidate"] is False
    assert candidates["SALARY CREDITED VIA NEFT"]["transaction_type"] == "income"
    assert candidates["SALARY CREDITED VIA NEFT"]["cc_payment_candidate"] is False


# -----------------------------------------------------------------------------
# GET /candidates — merchant-alias layer (ADR-0011, Phase A3).
# -----------------------------------------------------------------------------


def test_candidates_confidence_follows_alias_across_different_raw_descriptor(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    stub_parser: type[_StubAxisParser],
    session: Session,
) -> None:
    """The core scenario Phase A3 exists for: a category confirmed under one raw
    descriptor makes a *different* raw descriptor's candidate — aliased to the
    same canonical — read non-zero prior_matches. Under the old exact-string
    LEFT JOIN this candidate would have read prior_matches=0/"none", because
    "swiggy hyd 67890" never had its own merchant_tag_map row.
    """
    food = next(c for c in seeded_categories if c.name == "Food")
    session.add(MerchantAlias(user_id=axis_account.user_id, pattern="swiggy", canonical="swiggy"))
    session.add(
        MerchantTagMap(
            user_id=axis_account.user_id,
            merchant_normalized="swiggy blr 12345",
            category_id=food.id,
            hit_count=1,
        )
    )
    session.commit()

    _StubAxisParser.rows = [
        RawTransaction(
            date=date(2026, 3, 5),
            amount_paise=-15000,
            merchant_raw="SWIGGY HYD 67890",
            txn_type="purchase",
        ),
    ]
    batch_id = _import_and_get_batch_id(client, axis_account.id)

    candidates = client.get(f"/api/v1/imports/{batch_id}/candidates").json()
    assert len(candidates) == 1
    row = candidates[0]
    # Import-time auto-tag (Phase A2) already resolves the alias.
    assert row["category_id"] == food.id
    assert row["prior_matches"] == 1
    assert row["confidence"] == "uncertain"


def test_candidates_confidence_seeded_when_present_at_hit_count_zero(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    stub_parser: type[_StubAxisParser],
    session: Session,
) -> None:
    """A (canonical, category) pair present at hit_count == 0 — what Phase A5's
    seed dictionary will insert — reads "seeded", distinct from an absent
    pair's "none" even though both carry prior_matches == 0."""
    food = next(c for c in seeded_categories if c.name == "Food")
    session.add(
        MerchantTagMap(
            user_id=axis_account.user_id,
            merchant_normalized="swiggy blr",
            category_id=food.id,
            hit_count=0,
        )
    )
    session.commit()

    _StubAxisParser.rows = [
        RawTransaction(
            date=date(2026, 3, 5),
            amount_paise=-15000,
            merchant_raw="SWIGGY BLR",
            txn_type="purchase",
        ),
    ]
    batch_id = _import_and_get_batch_id(client, axis_account.id)

    candidates = client.get(f"/api/v1/imports/{batch_id}/candidates").json()
    assert len(candidates) == 1
    row = candidates[0]
    assert row["category_id"] == food.id
    assert row["prior_matches"] == 0
    assert row["confidence"] == "seeded"


def test_candidates_pinned_survives_seeded_hit_count_zero(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    stub_parser: type[_StubAxisParser],
    session: Session,
) -> None:
    """A PINNED seed row must surface ``pinned: true``, and is not "seeded".

    ``pin_tag`` never bumps ``hit_count`` on an existing row, so pinning a seed
    row is the one way to reach ``hit_count == 0, pinned=True``. Hard-coding
    ``pinned=False`` in ``_candidate_strength``'s zero-hit branch made the user's
    own authored rule render as an unconfirmed dictionary suggestion — TagPicker
    checks ``pinned`` before ``confidence``, so carrying the real flag is the
    whole fix. A pin is user-authored (ADR-0011 rule 11), so the pair reads
    ``confidence: "none"`` — "seeded" is only a never-confirmed, unpinned row.
    """
    food = next(c for c in seeded_categories if c.name == "Food")
    session.add(
        MerchantTagMap(
            user_id=axis_account.user_id,
            merchant_normalized="swiggy blr",
            category_id=food.id,
            hit_count=0,
            pinned=True,
        )
    )
    session.commit()

    _StubAxisParser.rows = [
        RawTransaction(
            date=date(2026, 3, 5),
            amount_paise=-15000,
            merchant_raw="SWIGGY BLR",
            txn_type="purchase",
        ),
    ]
    batch_id = _import_and_get_batch_id(client, axis_account.id)

    row = client.get(f"/api/v1/imports/{batch_id}/candidates").json()[0]
    assert row["category_id"] == food.id
    assert row["prior_matches"] == 0
    assert row["confidence"] == "none"
    assert row["pinned"] is True


def test_candidates_confidence_drops_to_none_once_category_archived(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    stub_parser: type[_StubAxisParser],
    session: Session,
) -> None:
    """Named, not discovered: the old LEFT JOIN never touched ``Category``, so
    archiving a category left an existing candidate's prior_matches untouched.
    ``prefetch_tag_strength`` (via ``_aggregate_tag_rows``) inherits the
    archived-category filter, so the same candidate now reads "none" — more
    consistent with the real prefill, and a deliberate behaviour change."""
    food = next(c for c in seeded_categories if c.name == "Food")
    session.add(
        MerchantTagMap(
            user_id=axis_account.user_id,
            merchant_normalized="swiggy blr",
            category_id=food.id,
            hit_count=5,
        )
    )
    session.commit()

    _StubAxisParser.rows = [
        RawTransaction(
            date=date(2026, 3, 5),
            amount_paise=-15000,
            merchant_raw="SWIGGY BLR",
            txn_type="purchase",
        ),
    ]
    batch_id = _import_and_get_batch_id(client, axis_account.id)

    before = client.get(f"/api/v1/imports/{batch_id}/candidates").json()[0]
    assert before["prior_matches"] == 5
    assert before["confidence"] == "confident"

    food.archived_at = datetime.now(UTC)
    session.add(food)
    session.commit()

    after = client.get(f"/api/v1/imports/{batch_id}/candidates").json()[0]
    assert after["prior_matches"] == 0
    assert after["confidence"] == "none"


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
    assert body["detail"] == {
        "message": "some transactions are not eligible to commit or discard",
        "invalid_ids": [99999],
    }

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
    """Untagged spend rows commit under the spend "Other" category (PRD §F5
    fallback), not rejected; income stays null. The Other default is NOT learned.
    Refunds share this one spend-kind fallback — see
    ``test_commit_defaults_untagged_refund_to_other`` below."""
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


def test_commit_defaults_untagged_refund_to_other(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    stub_parser: type[_StubAxisParser],
    session_factory: sessionmaker[Session],
) -> None:
    """An untagged refund row commits under the spend "Other" category — the
    same spend-kind fallback an untagged spend row gets, since a refund nets
    against spend in the same category (PRD §F5). The default is NOT learned,
    mirroring the spend case."""
    _StubAxisParser.rows = [
        RawTransaction(
            date=date(2026, 3, 10),
            amount_paise=50000,
            merchant_raw="SWIGGY BLR REFUND",
            txn_type="refund",
        ),
    ]
    batch_id = _import_and_get_batch_id(client, axis_account.id)
    other = next(c for c in seeded_categories if c.name == "Other")

    candidates = client.get(f"/api/v1/imports/{batch_id}/candidates").json()
    assert len(candidates) == 1
    assert candidates[0]["transaction_type"] == "spend"  # a refund IS spend (ADR-0009)
    assert candidates[0]["amount_paise"] == 50000  # positive — the refund
    assert candidates[0]["category_id"] is None

    commit = client.post(
        f"/api/v1/imports/{batch_id}/commit",
        json={"transaction_ids": [candidates[0]["id"]]},
    )
    assert commit.status_code == 204, commit.text

    with session_factory() as s:
        row = s.get(Transaction, candidates[0]["id"])
        assert row is not None
        assert row.category_id == other.id
        # A fallback default isn't a merchant→category decision worth teaching F3.
        assert s.scalar(select(func.count()).select_from(MerchantTagMap)) == 0


def test_commit_keeps_the_cashback_prefill_and_does_not_teach_it(
    client: TestClient,
    session: Session,
    axis_account: Account,
    seeded_categories: list[Category],
    stub_parser: type[_StubAxisParser],
    session_factory: sessionmaker[Session],
) -> None:
    """A credit naming cashback stages as a positive ``spend`` pre-filled into the
    spend "Cashback" (ADR-0013 rules 1-2), and commits there. Committed still in
    that keyword prefill, it is a server default and teaches no rule (ADR-0004
    rule 5).

    ``seeded_categories`` mirrors the old migration seed, which has no spend
    "Cashback" — so it is added here rather than widening that shared fixture.
    """
    cashback_cat = Category(
        color="#2a78d6",
        user_id=axis_account.user_id,
        name=CASHBACK_CATEGORY_NAME,
        kind="spend",
        is_seeded=True,
    )
    session.add(cashback_cat)
    session.commit()
    session.refresh(cashback_cat)

    _StubAxisParser.rows = [
        RawTransaction(
            date=date(2026, 3, 10),
            amount_paise=17500,
            merchant_raw="ADDITIONAL CASHBACK FOR SWIGGY TRANSACTIONS",
            txn_type="other",
        ),
    ]
    batch_id = _import_and_get_batch_id(client, axis_account.id)

    (candidate,) = client.get(f"/api/v1/imports/{batch_id}/candidates").json()
    assert candidate["transaction_type"] == "spend"
    assert candidate["amount_paise"] == 17500
    assert candidate["category_id"] == cashback_cat.id

    commit = client.post(
        f"/api/v1/imports/{batch_id}/commit", json={"transaction_ids": [candidate["id"]]}
    )
    assert commit.status_code == 204, commit.text

    with session_factory() as s:
        row = s.get(Transaction, candidate["id"])
        assert row is not None
        assert row.category_id == cashback_cat.id
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


def test_patch_pending_row_type_flip_does_not_learn(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    stub_parser: type[_StubAxisParser],
    session_factory: sessionmaker[Session],
) -> None:
    """The review queue's merged Spend/Income category picker (review-queue.tsx)
    PATCHes `transaction_type` directly on a still-pending row: picking a spend
    category retypes an `income` credit to `spend` (its positive amount is what
    makes it a refund, ADR-0009). Same no-learning contract as a category-only
    PATCH (mirrors test_patch_pending_row_does_not_learn), and the same
    ADR-0007 rule-5 kind-flip requirement as a confirmed row: the new kind's
    category must be sent in the same request."""
    batch_id = _import_and_get_batch_id(client, axis_account.id)
    payment_id = next(
        c["id"]
        for c in client.get(f"/api/v1/imports/{batch_id}/candidates").json()
        if c["transaction_type"] == "income"
    )
    food = next(c for c in seeded_categories if c.name == "Food")

    resp = client.patch(
        f"/api/v1/transactions/{payment_id}",
        json={"transaction_type": "spend", "category_id": food.id},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["transaction_type"] == "spend"
    assert resp.json()["category_id"] == food.id

    with session_factory() as s:
        row = s.get(Transaction, payment_id)
        assert row is not None and row.confirmed_at is None
        # Retyped, but still pending — no rule learned yet.
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


def test_commit_income_row_with_archived_category_falls_back_to_other(
    client: TestClient,
    session: Session,
    axis_account: Account,
    seeded_categories: list[Category],
    stub_parser: type[_StubAxisParser],
    session_factory: sessionmaker[Session],
) -> None:
    """An income row whose category was archived mid-review commits under the
    income fallback "Other", never on the dead bucket (PRD §F5, ADR-0012 rule 8).
    ``seeded_categories`` has no income Other, so this test adds the root the
    migration and provisioning give every user."""
    income_other = Category(
        color="#2a78d6",
        user_id=axis_account.user_id,
        name=FALLBACK_CATEGORY_NAME,
        kind="income",
        is_seeded=True,
    )
    bonus = Category(color="#2a78d6", user_id=axis_account.user_id, name="Bonus", kind="income")
    session.add_all([income_other, bonus])
    session.commit()
    session.refresh(income_other)
    session.refresh(bonus)

    # A "payment" that matches no card-bill merchant, so the F4a flip leaves it income.
    _StubAxisParser.rows = [
        RawTransaction(
            date=date(2026, 3, 7),
            amount_paise=250000,
            merchant_raw="SALARY CREDITED VIA NEFT",
            txn_type="payment",
        ),
    ]
    batch_id = _import_and_get_batch_id(client, axis_account.id)
    (candidate,) = client.get(f"/api/v1/imports/{batch_id}/candidates").json()
    assert candidate["transaction_type"] == "income"

    patch = client.patch(f"/api/v1/transactions/{candidate['id']}", json={"category_id": bonus.id})
    assert patch.status_code == 200, patch.text
    assert client.delete(f"/api/v1/categories/{bonus.id}").status_code == 204

    commit = client.post(
        f"/api/v1/imports/{batch_id}/commit", json={"transaction_ids": [candidate["id"]]}
    )
    assert commit.status_code == 204, commit.text

    with session_factory() as s:
        row = s.get(Transaction, candidate["id"])
        assert row is not None
        assert row.transaction_type == "income"
        assert row.category_id == income_other.id


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
        "zero_amount_count": 0,
        "already_imported": True,
        "pending_count": n_purchases,
        "duplicate_of_account_id": None,
        "duplicate_of_account_archived": False,
        "reconciliation_delta_paise": None,
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


@pytest.mark.parametrize("body", [{}, {"transaction_ids": [], "discard_ids": []}])
def test_import_commit_body_rejects_an_empty_request(
    client: TestClient,
    axis_account: Account,
    stub_parser: type[_StubAxisParser],
    body: dict,
) -> None:
    """Neither list names a row: a frontend bug, not a no-op (cancel exists for that)."""
    batch_id = _import_and_get_batch_id(client, axis_account.id)
    resp = client.post(f"/api/v1/imports/{batch_id}/commit", json=body)
    assert resp.status_code == 422


# -----------------------------------------------------------------------------
# Discard in the commit request (ADR-0002 rule 5, A70).
# -----------------------------------------------------------------------------
def _batch_with_three_rows(client: TestClient, account: Account) -> tuple[int, int, int, int]:
    """Import and return ``(batch_id, a, b, c)``: three distinct pending row ids."""
    batch_id = _import_and_get_batch_id(client, account.id)
    ids = sorted(c["id"] for c in client.get(f"/api/v1/imports/{batch_id}/candidates").json())
    return batch_id, ids[0], ids[1], ids[2]


def test_commit_discards_listed_pending_rows_in_the_same_request(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    stub_parser: type[_StubAxisParser],
    session_factory: sessionmaker[Session],
) -> None:
    """ADR-0002 rule 5: the discard is the shared delete. A confirmed refund pointing at the
    discarded row is released (a raw DELETE would fail ``fk_transactions_refund_of_id``)."""
    batch_id, a, b, c = _batch_with_three_rows(client, axis_account)
    with session_factory() as s:
        refund = Transaction(
            user_id=axis_account.user_id,
            account_id=axis_account.id,
            date=date(2026, 3, 9),
            amount_paise=4200,
            transaction_type="spend",
            merchant_raw="REFUND",
            merchant_normalized="refund",
            fingerprint="a70-refund-fingerprint",
            source="manual",
            confirmed_at=datetime(2026, 3, 9, 10, 0, 0),
            refund_of_id=b,
        )
        s.add(refund)
        s.commit()
        refund_id = refund.id

    resp = client.post(
        f"/api/v1/imports/{batch_id}/commit",
        json={"transaction_ids": [a], "discard_ids": [b]},
    )

    assert resp.status_code == 204, resp.text
    with session_factory() as s:
        assert s.get(Transaction, a).confirmed_at is not None
        assert s.get(Transaction, b) is None
        assert s.get(Transaction, refund_id).refund_of_id is None
        c_row = s.get(Transaction, c)
        assert c_row is not None
        assert c_row.confirmed_at is None


@pytest.mark.parametrize("kind", ["other_batch", "confirmed", "missing"])
def test_commit_with_an_ineligible_discard_id_422s_and_writes_nothing(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    stub_parser: type[_StubAxisParser],
    session_factory: sessionmaker[Session],
    kind: str,
) -> None:
    batch_id, a, b, _ = _batch_with_three_rows(client, axis_account)
    stub_parser.rows = [
        RawTransaction(
            date=date(2026, 4, 1),
            amount_paise=-7700,
            merchant_raw="OTHER FILE",
            txn_type="purchase",
        )
    ]
    other_batch_id = _import_and_get_batch_id(client, axis_account.id, content=b"file-B")
    with session_factory() as s:
        other_row = s.scalars(
            select(Transaction).where(Transaction.import_batch_id == other_batch_id)
        ).first()
        assert other_row is not None
        bad = {"other_batch": other_row.id, "confirmed": b, "missing": 99999}[kind]
        if kind == "confirmed":
            s.get(Transaction, b).confirmed_at = datetime(2026, 3, 9, 10, 0, 0)
            s.commit()

    resp = client.post(
        f"/api/v1/imports/{batch_id}/commit",
        json={"transaction_ids": [a], "discard_ids": [bad]},
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == {
        "message": "some transactions are not eligible to commit or discard",
        "invalid_ids": [bad],
    }
    with session_factory() as s:
        assert s.get(Transaction, a).confirmed_at is None
        if kind != "missing":
            assert s.get(Transaction, bad) is not None


def test_commit_discard_only_request_is_accepted(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    stub_parser: type[_StubAxisParser],
    session_factory: sessionmaker[Session],
) -> None:
    batch_id, _, b, _ = _batch_with_three_rows(client, axis_account)

    resp = client.post(f"/api/v1/imports/{batch_id}/commit", json={"discard_ids": [b]})

    assert resp.status_code == 204, resp.text
    with session_factory() as s:
        assert s.get(Transaction, b) is None


def test_commit_id_in_both_lists_422s(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    stub_parser: type[_StubAxisParser],
    session_factory: sessionmaker[Session],
) -> None:
    batch_id, a, _, _ = _batch_with_three_rows(client, axis_account)

    resp = client.post(
        f"/api/v1/imports/{batch_id}/commit",
        json={"transaction_ids": [a], "discard_ids": [a]},
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == {
        "message": "some transactions are not eligible to commit or discard",
        "invalid_ids": [a],
    }
    with session_factory() as s:
        row = s.get(Transaction, a)
        assert row is not None
        assert row.confirmed_at is None


def test_commit_teaches_the_committed_row_not_the_discarded_one(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    stub_parser: type[_StubAxisParser],
    session_factory: sessionmaker[Session],
) -> None:
    """A discarded row is deleted, not decided: it never teaches F3 (ADR-0004)."""
    food = next(c for c in seeded_categories if c.name == "Food")
    stub_parser.rows = [
        RawTransaction(
            date=date(2026, 3, 5), amount_paise=-15000, merchant_raw="NETFLIX", txn_type="purchase"
        ),
        RawTransaction(
            date=date(2026, 3, 6), amount_paise=-22000, merchant_raw="ZOMATO", txn_type="purchase"
        ),
    ]
    batch_id = _import_and_get_batch_id(client, axis_account.id)
    by_merchant = {
        c["merchant_raw"]: c["id"]
        for c in client.get(f"/api/v1/imports/{batch_id}/candidates").json()
    }
    for txn_id in by_merchant.values():
        patch = client.patch(f"/api/v1/transactions/{txn_id}", json={"category_id": food.id})
        assert patch.status_code == 200, patch.text

    resp = client.post(
        f"/api/v1/imports/{batch_id}/commit",
        json={"transaction_ids": [by_merchant["NETFLIX"]], "discard_ids": [by_merchant["ZOMATO"]]},
    )

    assert resp.status_code == 204, resp.text
    with session_factory() as s:
        learned = {m.merchant_normalized: m.category_id for m in s.scalars(select(MerchantTagMap))}
    assert learned == {"netflix": food.id}


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
        "reconciliation_delta_paise": None,
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

    ADR-0003 rule 7: the read restates ``Account.user_id`` even though the composite
    same-user FK on ``import_batches.account_id`` (rule 10) refuses this row. The two
    are independent defences, so the row is stored with FKs off and the read must hold
    on its own. This is the only one of the three such reads that returns
    ``Account.last4``.
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

    with foreign_keys_off(session):
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
    ``ck_transactions_no_self_pair`` cannot fire because the CC row is positive on a card
    while candidates are negative on its parent bank, so they are different rows on different
    accounts. So the only way to observe
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
    income_cat = Category(
        color="#2a78d6", user_id=seeded_user.id, name="Salary", kind="income", is_seeded=False
    )
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


# -----------------------------------------------------------------------------
# Balance reconciliation (PRD §F1/§F4a) — ImportSummary/PendingImportBatch's
# reconciliation_delta_paise field, and GET /imports/{batch_id}/reconciliation.
# -----------------------------------------------------------------------------
# _REVIEW_ROWS sums to 453500 paise (-15000 -22000 -9500 +500000), spanning
# 2026-03-05..2026-03-08 — used below as the "clean" statement window.
_REVIEW_ROWS_SUM = sum(r.amount_paise for r in _REVIEW_ROWS)


def test_import_response_surfaces_reconciliation_delta(
    client: TestClient,
    axis_account: Account,
    stub_parser: type[_StubAxisParser],
) -> None:
    """POST /imports proxies the batch's just-computed delta (Phase 3) verbatim."""
    _StubAxisParser.summary = StatementSummary(
        opening_balance_paise=0,
        closing_balance_paise=_REVIEW_ROWS_SUM + 12345,  # mismatch by 12345
        period_start=date(2026, 3, 5),
        period_end=date(2026, 3, 8),
    )
    resp = client.post(
        "/api/v1/imports",
        data={"account_id": str(axis_account.id)},
        files={"file": ("statement.pdf", b"file-recon-A", "application/pdf")},
    )
    assert resp.status_code == 200, resp.text
    # actual (453500) - expected (453500 + 12345) = -12345
    assert resp.json()["reconciliation_delta_paise"] == -12345


def test_pending_surfaces_persisted_reconciliation_delta(
    client: TestClient,
    axis_account: Account,
    stub_parser: type[_StubAxisParser],
) -> None:
    """GET /imports/pending selects reconciliation_delta_paise off ImportBatch
    (the outer query, not the pending-count subquery's GROUP BY)."""
    _StubAxisParser.summary = StatementSummary(
        opening_balance_paise=0,
        closing_balance_paise=_REVIEW_ROWS_SUM,  # reconciles clean
        period_start=date(2026, 3, 5),
        period_end=date(2026, 3, 8),
    )
    _import_and_get_batch_id(client, axis_account.id, content=b"file-recon-B")

    rows = client.get("/api/v1/imports/pending").json()
    assert len(rows) == 1
    assert rows[0]["reconciliation_delta_paise"] == 0


def test_reconciliation_route_matched(
    client: TestClient,
    axis_account: Account,
    stub_parser: type[_StubAxisParser],
) -> None:
    _StubAxisParser.summary = StatementSummary(
        opening_balance_paise=0,
        closing_balance_paise=_REVIEW_ROWS_SUM,
        period_start=date(2026, 3, 5),
        period_end=date(2026, 3, 8),
    )
    batch_id = _import_and_get_batch_id(client, axis_account.id, content=b"file-recon-C")

    resp = client.get(f"/api/v1/imports/{batch_id}/reconciliation")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "batch_id": batch_id,
        "opening_balance_paise": 0,
        "closing_balance_paise": _REVIEW_ROWS_SUM,
        "period_start": "2026-03-05",
        "period_end": "2026-03-08",
        "expected_paise": _REVIEW_ROWS_SUM,
        "actual_paise": _REVIEW_ROWS_SUM,
        "delta_paise": 0,
        "status": "matched",
        "rows_removed_since_import": 0,
    }


def test_reconciliation_route_mismatched_reports_the_breakdown(
    client: TestClient,
    axis_account: Account,
    stub_parser: type[_StubAxisParser],
    session_factory: sessionmaker[Session],
) -> None:
    """actual/expected/delta stay algebraically consistent (actual = expected + delta),
    and the fresh delta is persisted onto the ImportBatch row."""
    expected = _REVIEW_ROWS_SUM + 12345
    _StubAxisParser.summary = StatementSummary(
        opening_balance_paise=0,
        closing_balance_paise=expected,
        period_start=date(2026, 3, 5),
        period_end=date(2026, 3, 8),
    )
    batch_id = _import_and_get_batch_id(client, axis_account.id, content=b"file-recon-D")

    resp = client.get(f"/api/v1/imports/{batch_id}/reconciliation")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["expected_paise"] == expected
    assert body["actual_paise"] == _REVIEW_ROWS_SUM
    assert body["delta_paise"] == _REVIEW_ROWS_SUM - expected
    assert body["status"] == "mismatched"
    # Nothing discarded here — this mismatch is a genuine statement/record gap,
    # not the discard-noise false-positive class.
    assert body["rows_removed_since_import"] == 0

    with session_factory() as s:
        batch = s.get(ImportBatch, batch_id)
        assert batch is not None
        assert batch.reconciliation_delta_paise == _REVIEW_ROWS_SUM - expected


def test_reconciliation_route_unavailable_without_a_summary_block(
    client: TestClient,
    axis_account: Account,
    stub_parser: type[_StubAxisParser],
) -> None:
    """No printed balance block (e.g. the Axis Flipkart layout) → "unavailable",
    never a 4xx — absent metadata is not a parse failure."""
    batch_id = _import_and_get_batch_id(client, axis_account.id)  # default: StatementSummary()

    resp = client.get(f"/api/v1/imports/{batch_id}/reconciliation")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "batch_id": batch_id,
        "opening_balance_paise": None,
        "closing_balance_paise": None,
        "period_start": None,
        "period_end": None,
        "expected_paise": None,
        "actual_paise": None,
        "delta_paise": None,
        "status": "unavailable",
        "rows_removed_since_import": 0,
    }


def test_reconciliation_route_recomputes_after_a_row_is_discarded(
    client: TestClient,
    axis_account: Account,
    stub_parser: type[_StubAxisParser],
) -> None:
    """The route recomputes on every call — it doesn't just echo the stored
    column — so a discard between two GETs is picked up without a re-import."""
    _StubAxisParser.summary = StatementSummary(
        opening_balance_paise=0,
        closing_balance_paise=_REVIEW_ROWS_SUM,
        period_start=date(2026, 3, 5),
        period_end=date(2026, 3, 8),
    )
    batch_id = _import_and_get_batch_id(client, axis_account.id, content=b"file-recon-E")

    first = client.get(f"/api/v1/imports/{batch_id}/reconciliation")
    assert first.json()["status"] == "matched"
    assert first.json()["rows_removed_since_import"] == 0

    # Discard the pending -9500 UBER row (the review-queue "not mine" action).
    uber_id = next(
        c["id"]
        for c in client.get(f"/api/v1/imports/{batch_id}/candidates").json()
        if c["merchant_raw"] == "UBER MUM"
    )
    assert client.delete(f"/api/v1/transactions/{uber_id}").status_code == 204

    second = client.get(f"/api/v1/imports/{batch_id}/reconciliation")
    assert second.status_code == 200, second.text
    body = second.json()
    # actual dropped by -9500 (the deleted row's own amount) while expected held.
    assert body["actual_paise"] == _REVIEW_ROWS_SUM - (-9500)
    assert body["delta_paise"] == 9500
    assert body["status"] == "mismatched"
    # The discard-noise qualifier explains exactly this mismatch: one of the
    # batch's 4 originally-staged rows (imported_count) no longer exists.
    assert body["rows_removed_since_import"] == 1


def test_reconciliation_route_unknown_batch_returns_404(
    client: TestClient,
    seeded_user: User,
) -> None:
    resp = client.get("/api/v1/imports/99999/reconciliation")
    assert resp.status_code == 404


def test_reconciliation_route_cross_user_returns_404(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    other_user = User(id=uuid.UUID("00000000-0000-0000-0000-000000000096"))
    session.add(other_user)
    session.commit()
    other_account = Account(
        user_id=other_user.id,
        name="Other CC",
        type="credit_card",
        issuer="axis",
        last4="4444",
    )
    session.add(other_account)
    session.commit()
    session.refresh(other_account)

    batch = ImportBatch(
        user_id=other_user.id,
        account_id=other_account.id,
        source_file_hash="other-recon-hash",
        parser_name="AxisCC",
        status="completed",
    )
    session.add(batch)
    session.commit()
    session.refresh(batch)

    # v1 single-user client always acts as seeded_user; this batch is not theirs.
    resp = client.get(f"/api/v1/imports/{batch.id}/reconciliation")
    assert resp.status_code == 404


def _hdfc_rows(*lines: tuple[date, str, int]) -> list[RawTransaction]:
    """Rows the real HDFC parser reads from a hand-built Layout A table: ``(date, narration,
    signed paise)`` per line, each a debit (negative) or a deposit. No PDF is involved, so the
    narration is typed by the parser's own column and reversal rules."""
    table: list[list[str]] = [
        ["Txn Date", "Narration", "Withdrawals", "Deposits", "Closing Balance"]
    ]
    balance = 100_000_000
    for when, narration, paise in lines:
        balance += paise
        day = when.strftime("%d/%m/%Y")
        table.append(
            [
                day,
                f"{narration} Value Dt {day}",
                f"{-paise / 100:.2f}" if paise < 0 else "0.00",
                f"{paise / 100:.2f}" if paise > 0 else "0.00",
                f"{balance / 100:.2f}",
            ]
        )
    return HdfcBank.interpret_tables([table])


def _stub_bank_parser(monkeypatch: pytest.MonkeyPatch, rows: list[RawTransaction]) -> None:
    """The bank side of the commit is driven through a stubbed ``("hdfc", "bank")`` parser
    whose ``parse`` returns ``rows`` (built by :func:`_hdfc_rows`), so no PDF bytes are needed."""

    class _StubBankParser:
        @classmethod
        def parse(cls, pdf_bytes: bytes, password: str | None) -> ParsedStatement:
            return ParsedStatement(rows=list(rows), summary=StatementSummary())

    monkeypatch.setitem(import_service.PARSERS, ("hdfc", "bank"), _StubBankParser)


def test_commit_bank_batch_links_to_confirmed_card_payment(
    client: TestClient,
    seeded_user: User,
    seeded_categories: list[Category],
    axis_account: Account,
    stub_parser: type[_StubAxisParser],
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """PRD §Verification 6, "Either side second": commit the card statement first, then
    import and commit the bank statement carrying the bill debit; the pair links at the bank
    commit, each leg moves its category aside, and nothing is taught."""
    _StubAxisParser.rows = list(_F4A_ROWS)
    bank = Account(user_id=seeded_user.id, name="HDFC Bank", type="bank", issuer="hdfc")
    session.add(bank)
    session.flush()
    axis_account.parent_account_id = bank.id
    session.commit()

    card_batch = _import_and_get_batch_id(client, axis_account.id)
    cc_id = client.get(f"/api/v1/imports/{card_batch}/candidates").json()[0]["id"]
    assert (
        client.post(
            f"/api/v1/imports/{card_batch}/commit", json={"transaction_ids": [cc_id]}
        ).status_code
        == 204
    )
    with session_factory() as s:
        card_before = s.get(Transaction, cc_id)
        assert card_before is not None and card_before.transfer_pair_id is None

    _stub_bank_parser(
        monkeypatch,
        _hdfc_rows(
            (_F4A_CC_DATE - timedelta(days=1), "CC 000000XXXXXX0000 AUTOPAY SI-TAD", -_F4A_AMOUNT)
        ),
    )
    bank_batch = _import_and_get_batch_id(client, bank.id, content=b"bank-file")
    bank_candidate = client.get(f"/api/v1/imports/{bank_batch}/candidates").json()[0]
    assert bank_candidate["cc_payment_candidate"] is True
    bank_id = bank_candidate["id"]
    commit = client.post(
        f"/api/v1/imports/{bank_batch}/commit", json={"transaction_ids": [bank_id]}
    )
    assert commit.status_code == 204, commit.text

    with session_factory() as s:
        cc_after, bank_after = s.get(Transaction, cc_id), s.get(Transaction, bank_id)
        assert cc_after is not None and bank_after is not None
        assert (cc_after.transfer_pair_id, bank_after.transfer_pair_id) == (bank_id, cc_id)
        assert (cc_after.transaction_type, bank_after.transaction_type) == ("transfer", "transfer")
        other = s.scalars(
            select(Category).where(
                Category.user_id == seeded_user.id,
                Category.name == FALLBACK_CATEGORY_NAME,
                Category.kind == "spend",
            )
        ).one()
        assert bank_after.category_id is None
        assert bank_after.pre_link_category_id == other.id
        assert s.scalars(select(MerchantTagMap)).all() == []


_BANK_DAY = date(2026, 4, 5)
_CARD_PERIOD = (date(2026, 3, 16), date(2026, 4, 15))
_CARD_PURCHASE = RawTransaction(
    date=date(2026, 4, 1), amount_paise=-50_000, merchant_raw="SWIGGY BLR", txn_type="purchase"
)
_BILL = "CC 000000XXXXXX0000 AUTOPAY SI-TAD"


def _bank_paying(
    session: Session, user: User, card: Account | None, *, name: str = "HDFC Bank"
) -> Account:
    """An HDFC bank account; when ``card`` is given, it names this bank as "Paid from"."""
    bank = Account(user_id=user.id, name=name, type="bank", issuer="hdfc")
    session.add(bank)
    session.flush()
    if card is not None:
        card.parent_account_id = bank.id
    session.commit()
    return bank


def _card_statement(
    client: TestClient,
    card: Account,
    *,
    rows: list[RawTransaction],
    period: tuple[date, date] | None,
    content: bytes,
    commit: bool = True,
) -> int:
    """Import a stubbed card statement into ``card`` and, by default, commit every row."""
    _StubAxisParser.rows = list(rows)
    _StubAxisParser.summary = (
        StatementSummary(period_start=period[0], period_end=period[1])
        if period is not None
        else StatementSummary()
    )
    batch_id = _import_and_get_batch_id(client, card.id, content=content)
    if commit:
        ids = [c["id"] for c in client.get(f"/api/v1/imports/{batch_id}/candidates").json()]
        resp = client.post(f"/api/v1/imports/{batch_id}/commit", json={"transaction_ids": ids})
        assert resp.status_code == 204, resp.text
    return batch_id


def _bank_bill(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    bank: Account,
    *,
    day: date,
    paise: int = -123_456,
) -> dict:
    """Import a one-debit bank statement naming a card-bill payment; return its candidate."""
    _stub_bank_parser(monkeypatch, _hdfc_rows((day, _BILL, paise)))
    batch_id = _import_and_get_batch_id(client, bank.id, content=b"bank-file")
    (candidate,) = client.get(f"/api/v1/imports/{batch_id}/candidates").json()
    candidate["batch_id"] = batch_id
    return candidate


def _commit_one(client: TestClient, candidate: dict) -> None:
    resp = client.post(
        f"/api/v1/imports/{candidate['batch_id']}/commit",
        json={"transaction_ids": [candidate["id"]]},
    )
    assert resp.status_code == 204, resp.text


def _other_spend(session: Session, user: User) -> Category:
    return session.scalars(
        select(Category).where(
            Category.user_id == user.id,
            Category.name == FALLBACK_CATEGORY_NAME,
            Category.kind == "spend",
        )
    ).one()


def test_commit_keeps_a_covered_card_bill_debit_out_of_spend(
    client: TestClient,
    seeded_user: User,
    seeded_categories: list[Category],
    axis_account: Account,
    stub_parser: type[_StubAxisParser],
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """PRD §Verification 6, "Unlinked card-bill debit" (ADR-0002 rule 7b): a flagged bank
    debit committed uncategorised, with a committed statement of its "Paid from" card covering
    its date, is an unpaired ``transfer`` in no category, and nothing is taught."""
    bank = _bank_paying(session, seeded_user, axis_account)
    _card_statement(
        client, axis_account, rows=[_CARD_PURCHASE], period=_CARD_PERIOD, content=b"card-1"
    )
    cand = _bank_bill(client, monkeypatch, bank, day=_BANK_DAY)
    assert cand.get("uncategorised_commits_as_transfer") is True

    _commit_one(client, cand)

    with session_factory() as s:
        row = s.get(Transaction, cand["id"])
        assert row is not None
        assert (
            row.transaction_type,
            row.transfer_pair_id,
            row.category_id,
            row.pre_link_type,
        ) == ("transfer", None, None, None)
        assert (
            s.scalars(
                select(MerchantTagMap).where(MerchantTagMap.merchant_normalized.contains("autopay"))
            ).all()
            == []
        )


@pytest.mark.parametrize(
    "case",
    [
        "no_paid_from",
        "card_paid_from_another_bank",
        "card_archived",
        "another_period",
        "card_batch_pending",
        "card_batch_cancelled",
    ],
)
def test_commit_card_bill_debit_counts_as_spend_when_no_committed_card_statement_covers_it(
    case: str,
    client: TestClient,
    seeded_user: User,
    seeded_categories: list[Category],
    axis_account: Account,
    stub_parser: type[_StubAxisParser],
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """ADR-0002 rule 7b, otherwise branch: without a live "Paid from" card holding a committed
    statement that covers the debit's date, the flagged debit is ordinary spend in Other."""
    if case == "card_paid_from_another_bank":
        _bank_paying(session, seeded_user, axis_account, name="ICICI Bank")
        bank = _bank_paying(session, seeded_user, None)
    else:
        bank = _bank_paying(session, seeded_user, None if case == "no_paid_from" else axis_account)
    period, purchase = _CARD_PERIOD, _CARD_PURCHASE
    if case == "another_period":
        period = (date(2026, 5, 16), date(2026, 6, 15))
        purchase = RawTransaction(
            date=date(2026, 5, 20),
            amount_paise=-50_000,
            merchant_raw="SWIGGY BLR",
            txn_type="purchase",
        )
    card_batch = _card_statement(
        client,
        axis_account,
        rows=[purchase],
        period=period,
        content=b"card-1",
        commit=case not in ("card_batch_pending", "card_batch_cancelled"),
    )
    if case == "card_batch_cancelled":
        assert client.delete(f"/api/v1/imports/{card_batch}").status_code == 204
    if case == "card_archived":
        axis_account.archived_at = datetime(2026, 1, 1)
        session.commit()

    cand = _bank_bill(client, monkeypatch, bank, day=_BANK_DAY)
    assert cand["uncategorised_commits_as_transfer"] is False
    _commit_one(client, cand)

    with session_factory() as s:
        row = s.get(Transaction, cand["id"])
        assert row is not None
        assert row.transaction_type == "spend"
        assert row.category_id == _other_spend(s, seeded_user).id


@pytest.mark.parametrize(
    ("debit_day", "expected"),
    [(date(2026, 4, 5), "transfer"), (date(2026, 4, 12), "spend")],
)
def test_commit_card_bill_debit_covered_by_a_statement_with_no_printed_period(
    debit_day: date,
    expected: str,
    client: TestClient,
    seeded_user: User,
    seeded_categories: list[Category],
    axis_account: Account,
    stub_parser: type[_StubAxisParser],
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """ADR-0002 rule 7b + ADR-0010 rule 8: a card statement that prints no period covers the
    first to last date of its own rows (1-10 April here), the same window the reconciler uses."""
    bank = _bank_paying(session, seeded_user, axis_account)
    _card_statement(
        client,
        axis_account,
        rows=[
            _CARD_PURCHASE,
            RawTransaction(
                date=date(2026, 4, 10),
                amount_paise=-20_000,
                merchant_raw="UBER MUM",
                txn_type="purchase",
            ),
        ],
        period=None,
        content=b"card-1",
    )
    cand = _bank_bill(client, monkeypatch, bank, day=debit_day)
    assert cand.get("uncategorised_commits_as_transfer") is (expected == "transfer")
    _commit_one(client, cand)

    with session_factory() as s:
        row = s.get(Transaction, cand["id"])
        assert row is not None
        assert row.transaction_type == expected


def test_commit_card_bill_debit_given_a_category_stays_spend(
    client: TestClient,
    seeded_user: User,
    seeded_categories: list[Category],
    axis_account: Account,
    stub_parser: type[_StubAxisParser],
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """ADR-0002 rule 7b: a flagged debit given a category in review is spend in that category,
    covered or not."""
    food = next(c for c in seeded_categories if c.name == "Food")
    bank = _bank_paying(session, seeded_user, axis_account)
    _card_statement(
        client, axis_account, rows=[_CARD_PURCHASE], period=_CARD_PERIOD, content=b"card-1"
    )
    cand = _bank_bill(client, monkeypatch, bank, day=_BANK_DAY)
    patched = client.patch(f"/api/v1/transactions/{cand['id']}", json={"category_id": food.id})
    assert patched.status_code == 200, patched.text

    _commit_one(client, cand)

    with session_factory() as s:
        row = s.get(Transaction, cand["id"])
        assert row is not None
        assert (row.transaction_type, row.category_id) == ("spend", food.id)


@pytest.mark.parametrize("paid_from", [True, False])
def test_commit_keeps_an_unmatched_card_payment_out_of_income(
    paid_from: bool,
    client: TestClient,
    seeded_user: User,
    seeded_categories: list[Category],
    axis_account: Account,
    stub_parser: type[_StubAxisParser],
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """PRD §Verification 4 and 6 (ADR-0002 rule 7c): a flagged card credit committed with no
    category is an unpaired ``transfer`` in no category, whatever "Paid from" says, and the
    month's income does not include it."""
    if paid_from:
        _bank_paying(session, seeded_user, axis_account)
    batch_id = _card_statement(
        client,
        axis_account,
        rows=[_F4A_ROWS[0]],
        period=_CARD_PERIOD,
        content=b"card-1",
        commit=False,
    )
    (cand,) = client.get(f"/api/v1/imports/{batch_id}/candidates").json()
    assert cand["uncategorised_commits_as_transfer"] is True
    cand["batch_id"] = batch_id

    _commit_one(client, cand)

    with session_factory() as s:
        row = s.get(Transaction, cand["id"])
        assert row is not None
        assert (
            row.transaction_type,
            row.transfer_pair_id,
            row.category_id,
            row.pre_link_type,
        ) == ("transfer", None, None, None)
    overview = client.get("/api/v1/dashboards/overview?month=2026-04")
    assert overview.status_code == 200, overview.text
    assert overview.json()["income_paise"] == 0


def test_commit_card_payment_given_a_category_keeps_its_kind(
    client: TestClient,
    seeded_user: User,
    seeded_categories: list[Category],
    axis_account: Account,
    stub_parser: type[_StubAxisParser],
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """ADR-0002 rule 7c: a flagged credit given a category in review commits in that category's
    kind, here income, so only the uncategorised credit is retyped."""
    bonus = Category(color="#2a78d6", user_id=seeded_user.id, name="Bonus", kind="income")
    session.add(bonus)
    session.commit()
    batch_id = _card_statement(
        client,
        axis_account,
        rows=[_F4A_ROWS[0]],
        period=_CARD_PERIOD,
        content=b"card-1",
        commit=False,
    )
    (cand,) = client.get(f"/api/v1/imports/{batch_id}/candidates").json()
    patched = client.patch(f"/api/v1/transactions/{cand['id']}", json={"category_id": bonus.id})
    assert patched.status_code == 200, patched.text
    cand["batch_id"] = batch_id

    _commit_one(client, cand)

    with session_factory() as s:
        row = s.get(Transaction, cand["id"])
        assert row is not None
        assert (row.transaction_type, row.category_id) == ("income", bonus.id)


def test_import_never_prefills_a_category_onto_a_card_bill_debit(
    client: TestClient,
    seeded_user: User,
    seeded_categories: list[Category],
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
) -> None:
    """ADR-0004 rule 8 and ADR-0002 rule 7b: a tag rule learned from one card bill never
    prefills the next flagged debit, so it reaches review uncategorised; an ordinary merchant on
    the same statement is still prefilled."""
    food = next(c for c in seeded_categories if c.name == "Food")
    bank = _bank_paying(session, seeded_user, None)
    rows = _hdfc_rows((_BANK_DAY, _BILL, -123_456), (_BANK_DAY, "SWIGGY BLR", -50_000))
    for row in rows:
        session.add(
            MerchantTagMap(
                user_id=seeded_user.id,
                merchant_normalized=normalize_merchant(row.merchant_raw),
                category_id=food.id,
                hit_count=5,
            )
        )
    session.commit()
    _stub_bank_parser(monkeypatch, rows)

    batch_id = _import_and_get_batch_id(client, bank.id, content=b"bank-file")

    candidates = client.get(f"/api/v1/imports/{batch_id}/candidates").json()
    by_kind = {("bill" if c["cc_payment_candidate"] else "other"): c for c in candidates}
    assert by_kind["bill"]["category_id"] is None
    assert by_kind["other"]["category_id"] == food.id


def _row_on(s: Session, account: Account) -> Transaction:
    return s.scalars(select(Transaction).where(Transaction.account_id == account.id)).one()


def test_commit_links_a_covered_card_bill_debit_to_a_card_payment_on_the_board(
    client: TestClient,
    seeded_user: User,
    seeded_categories: list[Category],
    axis_account: Account,
    stub_parser: type[_StubAxisParser],
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """PRD §Verification 6, "CC-bill double-count, from two imports" (card first): the card's
    payment credit is on the board, so the covered debit links to it at the bank commit and
    keeps ``transfer`` as its pre-link type (ADR-0002 rules 7b, 9)."""
    bank = _bank_paying(session, seeded_user, axis_account)
    _card_statement(client, axis_account, rows=[_F4A_ROWS[0]], period=_CARD_PERIOD, content=b"c-1")
    cand = _bank_bill(
        client, monkeypatch, bank, day=_F4A_CC_DATE - timedelta(days=1), paise=-_F4A_AMOUNT
    )
    _commit_one(client, cand)

    with session_factory() as s:
        bank_row, card_row = _row_on(s, bank), _row_on(s, axis_account)
        assert (bank_row.transfer_pair_id, card_row.transfer_pair_id) == (card_row.id, bank_row.id)
        assert (bank_row.transaction_type, card_row.transaction_type) == ("transfer", "transfer")
        assert bank_row.pre_link_type == "transfer"
        assert bank_row.pre_link_category_id is None
        assert card_row.pre_link_type == "transfer"


def test_commit_links_a_later_card_payment_to_a_covered_card_bill_debit(
    client: TestClient,
    seeded_user: User,
    seeded_categories: list[Category],
    axis_account: Account,
    stub_parser: type[_StubAxisParser],
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """ADR-0002 rules 7b, 7, 9: an unpaired covered debit links when the card's next statement
    brings the matching payment, and an unlink leaves the debit an unpaired transfer."""
    bank = _bank_paying(session, seeded_user, axis_account)
    _card_statement(
        client, axis_account, rows=[_CARD_PURCHASE], period=_CARD_PERIOD, content=b"card-1"
    )
    cand = _bank_bill(client, monkeypatch, bank, day=date(2026, 4, 15), paise=-_F4A_AMOUNT)
    _commit_one(client, cand)
    with session_factory() as s:
        bank_row = _row_on(s, bank)
        assert (bank_row.transaction_type, bank_row.transfer_pair_id) == ("transfer", None)

    _card_statement(
        client,
        axis_account,
        rows=[
            RawTransaction(
                date=date(2026, 4, 16),
                amount_paise=_F4A_AMOUNT,
                merchant_raw="PAYMENT RECEIVED THANK YOU",
                txn_type="payment",
            )
        ],
        period=(date(2026, 4, 16), date(2026, 5, 15)),
        content=b"card-2",
    )

    with session_factory() as s:
        bank_row = _row_on(s, bank)
        card_payment = s.scalars(
            select(Transaction).where(
                Transaction.account_id == axis_account.id, Transaction.amount_paise > 0
            )
        ).one()
        assert (bank_row.transfer_pair_id, card_payment.transfer_pair_id) == (
            card_payment.id,
            bank_row.id,
        )
        assert (bank_row.pre_link_type, card_payment.pre_link_type) == ("transfer", "transfer")
        card_payment_id = card_payment.id

    assert client.post(f"/api/v1/transactions/{card_payment_id}/unlink").status_code == 204
    with session_factory() as s:
        bank_row = _row_on(s, bank)
        card_payment = s.get(Transaction, card_payment_id)
        assert card_payment is not None
        assert (bank_row.transaction_type, bank_row.transfer_pair_id) == ("transfer", None)
        assert (card_payment.transaction_type, card_payment.transfer_pair_id) == ("transfer", None)


def test_commit_links_a_card_payment_to_an_earlier_card_bill_debit_counted_as_spend(
    client: TestClient,
    seeded_user: User,
    seeded_categories: list[Category],
    axis_account: Account,
    stub_parser: type[_StubAxisParser],
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """PRD §Verification 6, "CC-bill double-count, from two imports" (bank first): with no card
    statement in yet the debit commits as spend in Other (ADR-0002 rule 7b), and the card's
    later payment links to it, moving Other aside (rule 9)."""
    bank = _bank_paying(session, seeded_user, axis_account)
    cand = _bank_bill(
        client, monkeypatch, bank, day=_F4A_CC_DATE - timedelta(days=1), paise=-_F4A_AMOUNT
    )
    _commit_one(client, cand)
    with session_factory() as s:
        bank_row = _row_on(s, bank)
        assert bank_row.transaction_type == "spend"
        assert bank_row.category_id == _other_spend(s, seeded_user).id

    _card_statement(client, axis_account, rows=[_F4A_ROWS[0]], period=_CARD_PERIOD, content=b"c-1")

    with session_factory() as s:
        bank_row, card_row = _row_on(s, bank), _row_on(s, axis_account)
        assert (bank_row.transfer_pair_id, card_row.transfer_pair_id) == (card_row.id, bank_row.id)
        assert bank_row.pre_link_type == "spend"
        assert bank_row.pre_link_category_id == _other_spend(s, seeded_user).id


@pytest.mark.parametrize(
    ("kind", "narration", "paise", "expected"),
    [
        ("bank", "CC 000000XXXXXX0000 AUTOPAY SI-TAD", -123_456, True),
        ("bank", "UPI-SENTINEL GROCER-sentinelgrocer@example-EXAM0000001", -50_000, False),
        # A bank income row is not a card-side "payment received" (the card clause is gated).
        ("bank", "NEFT CR-EXAM0000002-SENTINEL PAYMENT RECEIVED", 70_000, False),
        # A positive bank row is never a card-bill debit, even naming the vocabulary.
        ("bank", "CC PAYMENT SENTINEL", 80_000, False),
        # A returned bill payment is a positive spend (a reversal), not a debit to link.
        ("bank", "REV-CC PAYMENT SENTINEL", 90_000, False),
        # A card-batch negative row naming a bill payment is not a candidate either.
        ("card", "CREDIT CARD PAYMENT", -30_000, False),
        # A positive payment credit on a card is flagged, and commits as a transfer (rule 7c).
        ("card", "PAYMENT RECEIVED THANK YOU", 30_000, True),
    ],
)
def test_candidates_flag_a_bank_card_bill_debit(
    kind: str,
    narration: str,
    paise: int,
    expected: bool,
    client: TestClient,
    seeded_user: User,
    seeded_categories: list[Category],
    axis_account: Account,
    stub_parser: type[_StubAxisParser],
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
) -> None:
    """ADR-0010 rule 12: a bank debit naming a card-bill payment is ``cc_payment_candidate``,
    so the review queue offers the link; the card-side clause does not fire on a bank batch."""
    if kind == "bank":
        account = Account(user_id=seeded_user.id, name="HDFC Bank", type="bank", issuer="hdfc")
        session.add(account)
        session.commit()
        _stub_bank_parser(monkeypatch, _hdfc_rows((_BANK_DAY, narration, paise)))
    else:
        account = axis_account
        txn_type = "payment" if paise > 0 else "purchase"
        _StubAxisParser.rows = [
            RawTransaction(
                date=_BANK_DAY, amount_paise=paise, merchant_raw=narration, txn_type=txn_type
            )
        ]
    batch_id = _import_and_get_batch_id(client, account.id)
    candidates = client.get(f"/api/v1/imports/{batch_id}/candidates").json()
    assert [c["cc_payment_candidate"] for c in candidates] == [expected]
    # No "Paid from" is set here, so no flagged bank debit commits as a transfer (rule 7b); a
    # flagged card credit does whatever "Paid from" says (rule 7c).
    assert [c["uncategorised_commits_as_transfer"] for c in candidates] == [
        kind == "card" and expected
    ]
    # A pending row is never offered the manual link: it needs confirmed rows (rule 11b).
    assert [c["card_bill_link_offered"] for c in candidates] == [False]
