"""End-to-end tests for ``/api/v1/dashboards/spend-by-category`` (PRD §F8 view 2).

Covers the locked decisions from the F8 plan:

* signed-sum nets refund against category spend (PRD §F4a rule 3);
* type filter excludes income + transfer;
* pending rows (``confirmed_at IS NULL``) are off the dashboard;
* uncategorized rows surface with ``category_id=null`` and pin to the
  bottom of the response regardless of magnitude;
* a refund-only category (positive total) sorts after every negative-total
  category but before the uncategorized bucket;
* archived categories with historical txns surface with their stored name;
* cross-user isolation holds even when a Transaction carries a foreign-
  user ``category_id`` (FK-permissive scenario the JOIN predicate guards);
* month parsing is calendar-correct (including the December rollover);
* invalid / missing ``?month=`` returns 422 without echoing the rejected value;
* the F1-review → F8 loop end-to-end (pending row absent from aggregate;
  committing the batch makes it appear).
"""

from __future__ import annotations

import math
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import clock
from app.models import (
    Account,
    Category,
    FxRateQuote,
    ImportBatch,
    Instrument,
    InvestmentAccount,
    InvestmentTransaction,
    Label,
    MerchantAlias,
    MerchantTagMap,
    PeerContact,
    PeerEntry,
    Transaction,
    TransactionLabel,
    User,
)
from app.services.category_service import CASHBACK_CATEGORY_NAME
from app.services.merchant import normalize_merchant
from app.services.reconciliation_service import pair_existing_rows
from tests.fk_bypass import foreign_keys_off

# Sentinel for the "use now()" default — distinct from caller-supplied None
# (None = pending row in the review queue, intentionally).
_DEFAULT_CONFIRMED: Any = object()


def _default_inv_account_id(session: Session, user_id: UUID) -> int:
    acc = session.scalar(select(InvestmentAccount).where(InvestmentAccount.user_id == user_id))
    if acc is None:
        acc = InvestmentAccount(
            user_id=user_id,
            name="Default Portfolio",
            account_type="manual",
            provider="manual",
            external_account_ref="default",
            currency="INR",
            is_active=True,
        )
        session.add(acc)
        session.flush()
    return acc.id


def _make_txn(
    *,
    user_id: UUID,
    account_id: int,
    txn_date: date,
    amount_paise: int,
    fingerprint: str,
    transaction_type: str = "spend",
    merchant_raw: str = "TEST MERCHANT",
    category_id: int | None = None,
    auto_category_id: int | None = None,
    import_batch_id: int | None = None,
    confirmed_at: datetime | None = _DEFAULT_CONFIRMED,
    source: str = "import",
) -> Transaction:
    """Mirrors test_transactions._make_txn with extra dashboard-relevant kwargs.

    Copied rather than promoted to conftest: two callers in sibling test
    files; promote on third use per CLAUDE.md §2. The end-to-end commit
    test also needs ``import_batch_id``, which the original helper doesn't
    expose either. ``source`` defaults to "import" (every prior caller relied
    on that default); pass "manual" to build a row the coverage_rate metric
    (Phase A0) must exclude.
    """
    return Transaction(
        user_id=user_id,
        account_id=account_id,
        date=txn_date,
        amount_paise=amount_paise,
        transaction_type=transaction_type,
        merchant_raw=merchant_raw,
        merchant_normalized=normalize_merchant(merchant_raw),
        category_id=category_id,
        auto_category_id=auto_category_id,
        fingerprint=fingerprint,
        source=source,
        import_batch_id=import_batch_id,
        confirmed_at=(datetime.now(UTC) if confirmed_at is _DEFAULT_CONFIRMED else confirmed_at),
    )


# -----------------------------------------------------------------------------
# Empty / happy paths
# -----------------------------------------------------------------------------


def test_empty_month_returns_zero_rows(
    client: TestClient,
    seeded_user: User,
) -> None:
    resp = client.get("/api/v1/dashboards/spend-by-category?month=2026-05")
    assert resp.status_code == 200
    assert resp.json() == {"period": "2026-05", "rows": [], "label_id": None}


def test_single_spend_returns_one_negative_row(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    session: Session,
) -> None:
    food = next(c for c in seeded_categories if c.name == "Food")
    session.add(
        _make_txn(
            user_id=axis_account.user_id,
            account_id=axis_account.id,
            txn_date=date(2026, 5, 10),
            amount_paise=-15000,
            fingerprint="fp-1",
            category_id=food.id,
        )
    )
    session.commit()

    resp = client.get("/api/v1/dashboards/spend-by-category?month=2026-05")
    assert resp.status_code == 200
    body = resp.json()
    assert body["period"] == "2026-05"
    assert body["rows"] == [
        {
            "category_id": food.id,
            "category_name": "Food",
            "parent_id": None,
            "parent_name": None,
            "display_color": "#2a78d6",
            "parent_display_color": None,
            "total_paise": -15000,
        },
    ]


def test_spend_plus_refund_same_category_signed_sum_nets(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    session: Session,
) -> None:
    """PRD §F4a rule 3: refund preserves category; signed sum nets it against spend."""
    food = next(c for c in seeded_categories if c.name == "Food")
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 10),
                amount_paise=-15000,
                fingerprint="fp-spend",
                category_id=food.id,
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 12),
                amount_paise=5000,
                fingerprint="fp-refund",
                transaction_type="spend",  # refund: spend row, positive amount (ADR-0009)
                category_id=food.id,
            ),
        ]
    )
    session.commit()

    resp = client.get("/api/v1/dashboards/spend-by-category?month=2026-05")
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    assert len(rows) == 1
    assert rows[0]["category_id"] == food.id
    assert rows[0]["total_paise"] == -10000


def test_spend_plus_equal_refund_same_category_nets_to_exactly_zero(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    session: Session,
) -> None:
    """A full refund of the same spend nets to precisely 0, not just "small" —
    the exact-zero edge case `test_spend_plus_refund_same_category_signed_sum_nets`
    doesn't cover. The row still surfaces (server does not drop zero-total
    categories); the frontend's own "no signal" filter is a display decision,
    not a server one (PRD §F4a rule 3)."""
    food = next(c for c in seeded_categories if c.name == "Food")
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 10),
                amount_paise=-15000,
                fingerprint="fp-spend-full",
                category_id=food.id,
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 12),
                amount_paise=15000,
                fingerprint="fp-refund-full",
                transaction_type="spend",  # refund: spend row, positive amount (ADR-0009)
                category_id=food.id,
            ),
        ]
    )
    session.commit()

    resp = client.get("/api/v1/dashboards/spend-by-category?month=2026-05")
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    assert len(rows) == 1
    assert rows[0]["category_id"] == food.id
    assert rows[0]["total_paise"] == 0


def test_two_categories_sorted_most_negative_first(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    session: Session,
) -> None:
    food = next(c for c in seeded_categories if c.name == "Food")
    shopping = next(c for c in seeded_categories if c.name == "Shopping")
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 10),
                amount_paise=-10000,
                fingerprint="fp-food",
                category_id=food.id,
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 11),
                amount_paise=-50000,
                fingerprint="fp-shop",
                category_id=shopping.id,
            ),
        ]
    )
    session.commit()

    resp = client.get("/api/v1/dashboards/spend-by-category?month=2026-05")
    rows = resp.json()["rows"]
    assert [r["category_id"] for r in rows] == [shopping.id, food.id]


# -----------------------------------------------------------------------------
# Uncategorized + sort placement
# -----------------------------------------------------------------------------


def test_uncategorized_row_pinned_last(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    session: Session,
) -> None:
    """category_id=null surfaces as a row and sorts last regardless of magnitude.

    The uncategorized row has a massively-negative total (would sort first
    by magnitude alone); assert it lands last via the boolean ``is_(None)``
    sort key.
    """
    food = next(c for c in seeded_categories if c.name == "Food")
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 10),
                amount_paise=-10000,
                fingerprint="fp-food",
                category_id=food.id,
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 11),
                amount_paise=-99999,
                fingerprint="fp-uncat",
                category_id=None,
            ),
        ]
    )
    session.commit()

    resp = client.get("/api/v1/dashboards/spend-by-category?month=2026-05")
    rows = resp.json()["rows"]
    assert len(rows) == 2
    assert rows[-1] == {
        "category_id": None,
        "category_name": None,
        "parent_id": None,
        "parent_name": None,
        "display_color": None,
        "parent_display_color": None,
        "total_paise": -99999,
    }


def test_spend_by_category_rows_carry_their_parent_even_when_archived(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """ADR-0012 rule 6: the rollup follows ``parent_id`` whatever the archive state.

    Each row names its parent so the frontend rolls a subcategory up without
    consulting the (active-only) category list. An archived child still carries its
    parent; a root and the uncategorized bucket carry ``None``.
    """
    uid = axis_account.user_id
    parent = Category(color="#2a78d6", user_id=uid, name="P", kind="spend")
    session.add(parent)
    session.flush()
    active = Category(user_id=uid, name="A", kind="spend", parent_id=parent.id)
    archived = Category(
        user_id=uid, name="B", kind="spend", parent_id=parent.id, archived_at=datetime.now(UTC)
    )
    session.add_all([active, archived])
    session.flush()
    for i, cat_id in enumerate([active.id, archived.id, parent.id, None]):
        session.add(
            _make_txn(
                user_id=uid,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 10),
                amount_paise=-1000 * (i + 1),
                fingerprint=f"fp-parent-{i}",
                category_id=cat_id,
            )
        )
    session.commit()

    rows = client.get("/api/v1/dashboards/spend-by-category?month=2026-05").json()["rows"]
    by_id = {r["category_id"]: r for r in rows}
    for child in (active, archived):
        assert by_id[child.id]["parent_id"] == parent.id
        assert by_id[child.id]["parent_name"] == "P"
    assert by_id[parent.id]["parent_id"] is None
    assert by_id[parent.id]["parent_name"] is None
    assert by_id[None]["parent_id"] is None
    assert by_id[None]["parent_name"] is None


def test_refund_only_category_sorts_between_spends_and_uncategorized(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    session: Session,
) -> None:
    """A category with only refunds in-window has a positive total.

    It must sort AFTER every negative-total category (most-negative-first
    asc order) but BEFORE the uncategorized bucket (which is pinned last).
    Locks UX for the "refunds nullified my spend" edge.
    """
    food = next(c for c in seeded_categories if c.name == "Food")
    shopping = next(c for c in seeded_categories if c.name == "Shopping")
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 10),
                amount_paise=-10000,
                fingerprint="fp-shop",
                category_id=shopping.id,
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 11),
                amount_paise=5000,
                fingerprint="fp-food-refund",
                transaction_type="spend",  # refund: spend row, positive amount (ADR-0009)
                category_id=food.id,
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 12),
                amount_paise=-1000,
                fingerprint="fp-uncat",
                category_id=None,
            ),
        ]
    )
    session.commit()

    resp = client.get("/api/v1/dashboards/spend-by-category?month=2026-05")
    rows = resp.json()["rows"]
    assert [r["category_id"] for r in rows] == [shopping.id, food.id, None]
    assert [r["total_paise"] for r in rows] == [-10000, 5000, -1000]


# -----------------------------------------------------------------------------
# Type / state filters
# -----------------------------------------------------------------------------


def test_income_rows_excluded(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    session: Session,
) -> None:
    income_cat = next(c for c in seeded_categories if c.name == "Income")
    session.add(
        _make_txn(
            user_id=axis_account.user_id,
            account_id=axis_account.id,
            txn_date=date(2026, 5, 10),
            amount_paise=100000,
            fingerprint="fp-inc",
            transaction_type="income",
            category_id=income_cat.id,
        )
    )
    session.commit()

    resp = client.get("/api/v1/dashboards/spend-by-category?month=2026-05")
    assert resp.json()["rows"] == []


def test_transfer_rows_excluded(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    session: Session,
) -> None:
    """Transfers excluded — without F4a auto-pair they'd double-count."""
    transfer_cat = next(c for c in seeded_categories if c.name == "Transfer")
    session.add(
        _make_txn(
            user_id=axis_account.user_id,
            account_id=axis_account.id,
            txn_date=date(2026, 5, 10),
            amount_paise=-50000,
            fingerprint="fp-tfr",
            transaction_type="transfer",
            category_id=transfer_cat.id,
        )
    )
    session.commit()

    resp = client.get("/api/v1/dashboards/spend-by-category?month=2026-05")
    assert resp.json()["rows"] == []


def test_pending_rows_excluded(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    session: Session,
) -> None:
    food = next(c for c in seeded_categories if c.name == "Food")
    session.add(
        _make_txn(
            user_id=axis_account.user_id,
            account_id=axis_account.id,
            txn_date=date(2026, 5, 10),
            amount_paise=-10000,
            fingerprint="fp-pend",
            category_id=food.id,
            confirmed_at=None,
        )
    )
    session.commit()

    resp = client.get("/api/v1/dashboards/spend-by-category?month=2026-05")
    assert resp.json()["rows"] == []


# -----------------------------------------------------------------------------
# F1-review → F8 end-to-end loop
# -----------------------------------------------------------------------------


def test_commit_makes_pending_row_appear_in_aggregate(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    session: Session,
) -> None:
    """The loop this PR closes: pending row absent → commit → row appears.

    Seeds a single pending row in an import batch (category set so the
    commit pre-flight accepts it), verifies the aggregate excludes it,
    posts to /commit, verifies the aggregate now includes it.
    """
    food = next(c for c in seeded_categories if c.name == "Food")
    batch = ImportBatch(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        source_file_hash="hash-commit-binding",
        parser_name="AxisCC",
        status="completed",
    )
    session.add(batch)
    session.commit()
    session.refresh(batch)

    pending = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 5, 10),
        amount_paise=-12000,
        fingerprint="fp-pending-commit",
        category_id=food.id,
        import_batch_id=batch.id,
        confirmed_at=None,
    )
    session.add(pending)
    session.commit()
    session.refresh(pending)

    before = client.get("/api/v1/dashboards/spend-by-category?month=2026-05")
    assert before.status_code == 200
    assert before.json()["rows"] == []

    commit = client.post(
        f"/api/v1/imports/{batch.id}/commit",
        json={"transaction_ids": [pending.id]},
    )
    assert commit.status_code == 204, commit.text

    after = client.get("/api/v1/dashboards/spend-by-category?month=2026-05")
    assert after.status_code == 200
    rows = after.json()["rows"]
    assert len(rows) == 1
    assert rows[0]["category_id"] == food.id
    assert rows[0]["total_paise"] == -12000


# -----------------------------------------------------------------------------
# Month boundary
# -----------------------------------------------------------------------------


def test_month_boundary_inclusive_on_first_and_last(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    session: Session,
) -> None:
    food = next(c for c in seeded_categories if c.name == "Food")
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 4, 30),
                amount_paise=-10000,
                fingerprint="fp-before",
                category_id=food.id,
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 1),
                amount_paise=-20000,
                fingerprint="fp-first",
                category_id=food.id,
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 31),
                amount_paise=-30000,
                fingerprint="fp-last",
                category_id=food.id,
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 6, 1),
                amount_paise=-40000,
                fingerprint="fp-after",
                category_id=food.id,
            ),
        ]
    )
    session.commit()

    resp = client.get("/api/v1/dashboards/spend-by-category?month=2026-05")
    rows = resp.json()["rows"]
    assert len(rows) == 1
    # Only the two May rows count: -20000 + -30000 = -50000.
    assert rows[0]["total_paise"] == -50000


def test_december_rollover(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    session: Session,
) -> None:
    """``calendar.monthrange(2026, 12) == (1, 31)`` — Dec 31 in, Jan 1 out."""
    food = next(c for c in seeded_categories if c.name == "Food")
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 12, 31),
                amount_paise=-10000,
                fingerprint="fp-dec31",
                category_id=food.id,
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2027, 1, 1),
                amount_paise=-20000,
                fingerprint="fp-jan1",
                category_id=food.id,
            ),
        ]
    )
    session.commit()

    resp = client.get("/api/v1/dashboards/spend-by-category?month=2026-12")
    rows = resp.json()["rows"]
    assert len(rows) == 1
    assert rows[0]["total_paise"] == -10000


def test_leap_year_february_includes_feb_29(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    session: Session,
) -> None:
    """``calendar.monthrange(2024, 2) == (3, 29)`` — Feb 29 lands in-window."""
    food = next(c for c in seeded_categories if c.name == "Food")
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2024, 2, 29),
                amount_paise=-10000,
                fingerprint="fp-feb29",
                category_id=food.id,
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2024, 3, 1),
                amount_paise=-20000,
                fingerprint="fp-mar1",
                category_id=food.id,
            ),
        ]
    )
    session.commit()

    resp = client.get("/api/v1/dashboards/spend-by-category?month=2024-02")
    rows = resp.json()["rows"]
    assert len(rows) == 1
    assert rows[0]["total_paise"] == -10000


# -----------------------------------------------------------------------------
# Cross-user isolation + cross-user category-name leak
# -----------------------------------------------------------------------------


def test_cross_user_rows_and_category_name_isolated(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    session: Session,
) -> None:
    """Two-pronged isolation check.

    1. A transaction owned by another user must not appear in v1's dashboard.
    2. A transaction owned by v1 but whose ``category_id`` points at another
       user's Category (the scenario the JOIN's ``Category.user_id == user_id``
       guards, ADR-0003 rule 7) must surface with ``category_name=null``, not
       the foreign user's category name.

    Prong 2 bypasses both the API's ``_assert_category_id_or_422`` write-time
    gate and the composite same-user FK (rule 10) — stored with FKs off — to
    prove the read holds on its own.
    """
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
    leaky_category = Category(
        color="#2a78d6",
        user_id=other_user.id,
        name="STOLEN-PROTECTED-NAME",
        is_seeded=False,
    )
    session.add_all([other_account, leaky_category])
    session.commit()
    session.refresh(other_account)
    session.refresh(leaky_category)

    food = next(c for c in seeded_categories if c.name == "Food")

    with foreign_keys_off(session):
        # Prong 1: another user's row in the same window.
        other_users_txn = _make_txn(
            user_id=other_user.id,
            account_id=other_account.id,
            txn_date=date(2026, 5, 10),
            amount_paise=-99999,
            fingerprint="fp-other-user",
            category_id=leaky_category.id,
        )
        # Prong 2: v1 user's row pointing at the OTHER user's category id.
        v1_txn_pointing_at_other = _make_txn(
            user_id=axis_account.user_id,
            account_id=axis_account.id,
            txn_date=date(2026, 5, 11),
            amount_paise=-5000,
            fingerprint="fp-v1-leak",
            category_id=leaky_category.id,
        )
        # Control: a normal v1 row so we can assert the dashboard isn't empty.
        v1_normal = _make_txn(
            user_id=axis_account.user_id,
            account_id=axis_account.id,
            txn_date=date(2026, 5, 12),
            amount_paise=-2000,
            fingerprint="fp-v1-normal",
            category_id=food.id,
        )
        session.add_all([other_users_txn, v1_txn_pointing_at_other, v1_normal])
        session.commit()

    resp = client.get("/api/v1/dashboards/spend-by-category?month=2026-05")
    assert resp.status_code == 200
    rows = resp.json()["rows"]

    # Other user's row is absent (prong 1).
    assert -99999 not in [r["total_paise"] for r in rows]

    # The v1 row pointing at the foreign category must appear, but the JOIN
    # predicate must drop the foreign category's name (prong 2).
    leaky_row = next(r for r in rows if r["category_id"] == leaky_category.id)
    assert leaky_row["category_name"] is None
    assert leaky_row["total_paise"] == -5000

    # Sanity: the normal v1 row keeps its name.
    food_row = next(r for r in rows if r["category_id"] == food.id)
    assert food_row["category_name"] == "Food"


# -----------------------------------------------------------------------------
# Archived category
# -----------------------------------------------------------------------------


def test_archived_category_surfaces_with_stored_name(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    session: Session,
) -> None:
    """Archived (soft-deleted) categories keep their name on historical rows."""
    food = next(c for c in seeded_categories if c.name == "Food")
    food.archived_at = datetime.now(UTC)
    session.add(
        _make_txn(
            user_id=axis_account.user_id,
            account_id=axis_account.id,
            txn_date=date(2026, 5, 10),
            amount_paise=-10000,
            fingerprint="fp-archived",
            category_id=food.id,
        )
    )
    session.commit()

    resp = client.get("/api/v1/dashboards/spend-by-category?month=2026-05")
    rows = resp.json()["rows"]
    assert len(rows) == 1
    assert rows[0]["category_id"] == food.id
    assert rows[0]["category_name"] == "Food"


def test_archived_account_transactions_stay_in_totals(
    client: TestClient,
    seeded_user: User,
    seeded_categories: list[Category],
    session: Session,
) -> None:
    """Closing a card must not retroactively erase its historical spend.

    The aggregate query intentionally does NOT join accounts, so
    archived-account rows are included. This test pins that as deliberate
    rather than incidental — flipping to exclude would be a behavior change
    that surfaces here.
    """
    food = next(c for c in seeded_categories if c.name == "Food")
    archived_card = Account(
        user_id=seeded_user.id,
        name="Closed CC",
        type="credit_card",
        issuer="axis",
        last4="0000",
        archived_at=datetime.now(UTC),
    )
    session.add(archived_card)
    session.commit()
    session.refresh(archived_card)

    session.add(
        _make_txn(
            user_id=seeded_user.id,
            account_id=archived_card.id,
            txn_date=date(2026, 5, 10),
            amount_paise=-13000,
            fingerprint="fp-archived-acct",
            category_id=food.id,
        )
    )
    session.commit()

    resp = client.get("/api/v1/dashboards/spend-by-category?month=2026-05")
    rows = resp.json()["rows"]
    assert len(rows) == 1
    assert rows[0]["category_id"] == food.id
    assert rows[0]["total_paise"] == -13000


def test_aggregate_total_matches_transaction_list_for_window(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    session: Session,
) -> None:
    """Invariant: sum(dashboard rows) == sum(spend+refund txns in the window).

    Cross-checks the aggregate against the list endpoint so future drift
    between the two (e.g. a type-filter change applied to only one) is
    caught. GET /transactions has no type filter, so the list side filters
    spend+refund client-side to match the dashboard's contract.
    """
    food = next(c for c in seeded_categories if c.name == "Food")
    transport = next(c for c in seeded_categories if c.name == "Transport")
    income_cat = next(c for c in seeded_categories if c.name == "Income")
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 3),
                amount_paise=-15000,
                fingerprint="fp-food",
                category_id=food.id,
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 7),
                amount_paise=-9000,
                fingerprint="fp-transport",
                category_id=transport.id,
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 9),
                amount_paise=3000,
                fingerprint="fp-food-refund",
                transaction_type="spend",  # refund: spend row, positive amount (ADR-0009)
                category_id=food.id,
            ),
            # Uncategorized spend — counts toward both sides.
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 11),
                amount_paise=-2000,
                fingerprint="fp-uncat",
                category_id=None,
            ),
            # Income — must be excluded from BOTH sides (list side via the
            # client-side type filter).
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 13),
                amount_paise=80000,
                fingerprint="fp-income",
                transaction_type="income",
                category_id=income_cat.id,
            ),
        ]
    )
    session.commit()

    dash = client.get("/api/v1/dashboards/spend-by-category?month=2026-05")
    dash_total = sum(r["total_paise"] for r in dash.json()["rows"])

    listing = client.get("/api/v1/transactions?date_from=2026-05-01&date_to=2026-05-31&limit=500")
    list_total = sum(t["amount_paise"] for t in listing.json() if t["transaction_type"] == "spend")

    assert dash_total == list_total
    # Sanity: the income row exists on the board but is in neither total.
    assert dash_total == -15000 - 9000 + 3000 - 2000


# -----------------------------------------------------------------------------
# Invalid month
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "month",
    ["2026-13", "2026-1", "2026-00", "2026/05", "abc", ""],
)
def test_invalid_month_returns_422_without_echoing_input(
    client: TestClient,
    seeded_user: User,
    month: str,
) -> None:
    resp = client.get(f"/api/v1/dashboards/spend-by-category?month={month}")
    assert resp.status_code == 422
    body = resp.json()
    # Route-side validation returns a flat ``detail`` string; the original
    # value must not appear anywhere in the response (input-echo discipline).
    if isinstance(body.get("detail"), str):
        assert body["detail"] == "month must match YYYY-MM"
    # Whatever shape FastAPI / our route returns, the raw `month` value
    # must never round-trip in the response body.
    assert month == "" or month not in resp.text


def test_missing_month_returns_422(
    client: TestClient,
    seeded_user: User,
) -> None:
    """Server doesn't default to 'current month' — frontend owns that."""
    resp = client.get("/api/v1/dashboards/spend-by-category")
    assert resp.status_code == 422


# =============================================================================
# spend-by-period (PRD §F8 view 3 — the weekly/monthly spend bar)
#
# Period matrix, NOT a mirror of the category matrix above: this endpoint has
# no JOIN / category / uncategorized-bucket machinery, so those tests don't
# port. The shared semantics (type filter, confirmed-only, cross-user, signed
# sum, no-accounts-join) ARE re-asserted here so a future change to only one
# endpoint is caught. Headline behaviours: zero-fill gaps, ISO-year-boundary
# week labels, and clip (window means literally [start, end], no outward snap).
#
# Anchor dates (verified): 2026-01-01 is a Thursday, so ISO week 1 of 2026 is
# Mon 2025-12-29 .. Sun 2026-01-04 (a late-December date lands in 2026-W01).
# 2026-06-01 is a Monday, the start of ISO 2026-W23.
# =============================================================================

_PERIOD_URL = "/api/v1/dashboards/spend-by-period"


# ---- month bucket ----------------------------------------------------------


def test_period_month_empty_window_zero_filled(
    client: TestClient,
    seeded_user: User,
) -> None:
    """No spend anywhere → every month in the window is a zero bar, and the
    envelope echoes the requested grain + window."""
    resp = client.get(f"{_PERIOD_URL}?bucket=month&start=2026-01-01&end=2026-03-31")
    assert resp.status_code == 200
    assert resp.json() == {
        "bucket": "month",
        "start": "2026-01-01",
        "end": "2026-03-31",
        "buckets": [
            {"period": "2026-01", "total_paise": 0},
            {"period": "2026-02", "total_paise": 0},
            {"period": "2026-03", "total_paise": 0},
        ],
        "label_id": None,
    }


def test_period_month_single_bucket(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    session.add(
        _make_txn(
            user_id=axis_account.user_id,
            account_id=axis_account.id,
            txn_date=date(2026, 2, 15),
            amount_paise=-15000,
            fingerprint="fp-feb",
        )
    )
    session.commit()

    resp = client.get(f"{_PERIOD_URL}?bucket=month&start=2026-02-01&end=2026-02-28")
    assert resp.status_code == 200
    assert resp.json()["buckets"] == [{"period": "2026-02", "total_paise": -15000}]


def test_period_month_three_buckets_chronological(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 1, 15),
                amount_paise=-10000,
                fingerprint="fp-jan",
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 2, 15),
                amount_paise=-20000,
                fingerprint="fp-feb",
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 3, 15),
                amount_paise=-30000,
                fingerprint="fp-mar",
            ),
        ]
    )
    session.commit()

    resp = client.get(f"{_PERIOD_URL}?bucket=month&start=2026-01-01&end=2026-03-31")
    assert resp.json()["buckets"] == [
        {"period": "2026-01", "total_paise": -10000},
        {"period": "2026-02", "total_paise": -20000},
        {"period": "2026-03", "total_paise": -30000},
    ]


def test_period_month_zero_fill_gap(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """Headline behaviour: a month with no in-window spend is a ₹0 bar, not a gap."""
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 1, 15),
                amount_paise=-10000,
                fingerprint="fp-jan",
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 3, 15),
                amount_paise=-30000,
                fingerprint="fp-mar",
            ),
        ]
    )
    session.commit()

    resp = client.get(f"{_PERIOD_URL}?bucket=month&start=2026-01-01&end=2026-03-31")
    assert resp.json()["buckets"] == [
        {"period": "2026-01", "total_paise": -10000},
        {"period": "2026-02", "total_paise": 0},
        {"period": "2026-03", "total_paise": -30000},
    ]


def test_period_month_refund_nets_within_bucket(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """Signed sum nets refund against spend in the same bucket (PRD §F4a rule 3)."""
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 2, 10),
                amount_paise=-15000,
                fingerprint="fp-spend",
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 2, 20),
                amount_paise=5000,
                fingerprint="fp-refund",
                transaction_type="spend",  # refund: spend row, positive amount (ADR-0009)
            ),
        ]
    )
    session.commit()

    resp = client.get(f"{_PERIOD_URL}?bucket=month&start=2026-02-01&end=2026-02-28")
    assert resp.json()["buckets"] == [{"period": "2026-02", "total_paise": -10000}]


def test_period_month_year_rollover(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """A window spanning the calendar-year boundary labels each month correctly."""
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2025, 12, 15),
                amount_paise=-10000,
                fingerprint="fp-dec",
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 1, 10),
                amount_paise=-20000,
                fingerprint="fp-jan",
            ),
        ]
    )
    session.commit()

    resp = client.get(f"{_PERIOD_URL}?bucket=month&start=2025-12-01&end=2026-01-31")
    assert resp.json()["buckets"] == [
        {"period": "2025-12", "total_paise": -10000},
        {"period": "2026-01", "total_paise": -20000},
    ]


def test_period_month_clip_excludes_out_of_window_days(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """Clip, not snap: a mid-month window sums only its in-window days.

    Request Jun 10–20. A Jun 5 spend (same month, before `start`) must be
    EXCLUDED; only the Jun 15 spend counts. An outward snap would have widened
    the window to all of June and included Jun 5.
    """
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 6, 5),
                amount_paise=-99999,
                fingerprint="fp-before-window",
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 6, 15),
                amount_paise=-12000,
                fingerprint="fp-in-window",
            ),
        ]
    )
    session.commit()

    resp = client.get(f"{_PERIOD_URL}?bucket=month&start=2026-06-10&end=2026-06-20")
    assert resp.json()["buckets"] == [{"period": "2026-06", "total_paise": -12000}]


# ---- week bucket (ISO-8601, Monday start) ----------------------------------


def test_period_week_single_iso_week_label(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """One full ISO week (Mon 2026-06-01 .. Sun 2026-06-07) → one 2026-W23 bar.

    Both the Monday and Sunday land in the same bucket; the label string is
    asserted directly.
    """
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 6, 1),  # Monday
                amount_paise=-10000,
                fingerprint="fp-mon",
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 6, 7),  # Sunday
                amount_paise=-5000,
                fingerprint="fp-sun",
            ),
        ]
    )
    session.commit()

    resp = client.get(f"{_PERIOD_URL}?bucket=week&start=2026-06-01&end=2026-06-07")
    assert resp.json()["buckets"] == [{"period": "2026-W23", "total_paise": -15000}]


def test_period_week_zero_fill_gap(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """Three consecutive ISO weeks; spend in W01 and W03, none in W02 → W02=0.

    Window: Mon 2025-12-29 (2026-W01) .. Sun 2026-01-18 (2026-W03).
    """
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2025, 12, 30),  # 2026-W01
                amount_paise=-10000,
                fingerprint="fp-w01",
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 1, 13),  # 2026-W03
                amount_paise=-30000,
                fingerprint="fp-w03",
            ),
        ]
    )
    session.commit()

    resp = client.get(f"{_PERIOD_URL}?bucket=week&start=2025-12-29&end=2026-01-18")
    assert resp.json()["buckets"] == [
        {"period": "2026-W01", "total_paise": -10000},
        {"period": "2026-W02", "total_paise": 0},
        {"period": "2026-W03", "total_paise": -30000},
    ]


def test_period_week_iso_year_boundary_label(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """A late-December date in ISO week 1 of the next year labels as 2026-W01.

    2025-12-30 is a Tuesday in the ISO week starting Mon 2025-12-29 = 2026-W01.
    The label must use the ISO year (2026), not the calendar year (2025) — this
    asserts the literal string to catch an iso.year -> d.year regression.
    """
    session.add(
        _make_txn(
            user_id=axis_account.user_id,
            account_id=axis_account.id,
            txn_date=date(2025, 12, 30),
            amount_paise=-10000,
            fingerprint="fp-boundary",
        )
    )
    session.commit()

    resp = client.get(f"{_PERIOD_URL}?bucket=week&start=2025-12-29&end=2026-01-04")
    assert resp.json()["buckets"] == [{"period": "2026-W01", "total_paise": -10000}]


def test_period_week_clip_partial_sum_within_edge_bucket(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """The sharp anti-snap test: a window starting mid-week sums only its
    in-window days, even within the partial first bucket.

    Window starts Wed 2026-06-03. A spend on Mon 2026-06-01 — same ISO week
    (2026-W23) but BEFORE `start` — must be EXCLUDED from that week's total;
    only the Thu 2026-06-04 spend counts. This fails if the sum ever snapped
    back to the Monday of the edge week.
    """
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 6, 1),  # Monday, same week, before window
                amount_paise=-99999,
                fingerprint="fp-mon-before",
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 6, 4),  # Thursday, in window
                amount_paise=-7000,
                fingerprint="fp-thu-in",
            ),
        ]
    )
    session.commit()

    resp = client.get(f"{_PERIOD_URL}?bucket=week&start=2026-06-03&end=2026-06-07")
    # Pin the whole list: the partial edge total is asserted, not just the key.
    assert resp.json()["buckets"] == [{"period": "2026-W23", "total_paise": -7000}]


# ---- shared semantics (re-asserted; a change to only one endpoint is caught) -


def test_period_excludes_income_and_transfer(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    session: Session,
) -> None:
    income_cat = next(c for c in seeded_categories if c.name == "Income")
    transfer_cat = next(c for c in seeded_categories if c.name == "Transfer")
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 2, 10),
                amount_paise=-15000,
                fingerprint="fp-spend",
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 2, 11),
                amount_paise=100000,
                fingerprint="fp-income",
                transaction_type="income",
                category_id=income_cat.id,
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 2, 12),
                amount_paise=-50000,
                fingerprint="fp-transfer",
                transaction_type="transfer",
                category_id=transfer_cat.id,
            ),
        ]
    )
    session.commit()

    resp = client.get(f"{_PERIOD_URL}?bucket=month&start=2026-02-01&end=2026-02-28")
    # Only the -15000 spend; income + transfer are excluded.
    assert resp.json()["buckets"] == [{"period": "2026-02", "total_paise": -15000}]


def test_period_excludes_pending(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """confirmed_at IS NULL (review queue) rows are off the dashboard."""
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 2, 10),
                amount_paise=-15000,
                fingerprint="fp-confirmed",
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 2, 11),
                amount_paise=-99999,
                fingerprint="fp-pending",
                confirmed_at=None,
            ),
        ]
    )
    session.commit()

    resp = client.get(f"{_PERIOD_URL}?bucket=month&start=2026-02-01&end=2026-02-28")
    assert resp.json()["buckets"] == [{"period": "2026-02", "total_paise": -15000}]


def test_period_cross_user_isolation(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
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

    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 2, 10),
                amount_paise=-15000,
                fingerprint="fp-v1",
            ),
            _make_txn(
                user_id=other_user.id,
                account_id=other_account.id,
                txn_date=date(2026, 2, 11),
                amount_paise=-99999,
                fingerprint="fp-other-user",
            ),
        ]
    )
    session.commit()

    resp = client.get(f"{_PERIOD_URL}?bucket=month&start=2026-02-01&end=2026-02-28")
    # Only v1's spend; the other user's -99999 is absent.
    assert resp.json()["buckets"] == [{"period": "2026-02", "total_paise": -15000}]


def test_period_archived_account_spend_still_counts(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    """No accounts join → spend on a closed card stays in totals (mirrors the
    spend-by-category guard)."""
    archived_card = Account(
        user_id=seeded_user.id,
        name="Closed CC",
        type="credit_card",
        issuer="axis",
        last4="0000",
        archived_at=datetime.now(UTC),
    )
    session.add(archived_card)
    session.commit()
    session.refresh(archived_card)

    session.add(
        _make_txn(
            user_id=seeded_user.id,
            account_id=archived_card.id,
            txn_date=date(2026, 2, 10),
            amount_paise=-13000,
            fingerprint="fp-archived-acct",
        )
    )
    session.commit()

    resp = client.get(f"{_PERIOD_URL}?bucket=month&start=2026-02-01&end=2026-02-28")
    assert resp.json()["buckets"] == [{"period": "2026-02", "total_paise": -13000}]


def test_period_total_matches_window_sum_and_category_dashboard(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    session: Session,
) -> None:
    """Invariant over a month-aligned window: Σ bucket totals == Σ confirmed
    spend+refund in the window == the spend-by-category grand total.

    Cross-checks the new endpoint against the trusted list + category views, so
    a future type-filter change to only one of them is caught. Month-aligned
    start/end keep the equality exact under clip (no partial edge bucket).
    """
    food = next(c for c in seeded_categories if c.name == "Food")
    income_cat = next(c for c in seeded_categories if c.name == "Income")
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 3),
                amount_paise=-15000,
                fingerprint="fp-food",
                category_id=food.id,
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 9),
                amount_paise=3000,
                fingerprint="fp-refund",
                transaction_type="spend",  # refund: spend row, positive amount (ADR-0009)
                category_id=food.id,
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 11),
                amount_paise=-2000,
                fingerprint="fp-uncat",
            ),
            # Income — excluded from all three sides.
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 13),
                amount_paise=80000,
                fingerprint="fp-income",
                transaction_type="income",
                category_id=income_cat.id,
            ),
        ]
    )
    session.commit()

    period = client.get(f"{_PERIOD_URL}?bucket=month&start=2026-05-01&end=2026-05-31")
    period_total = sum(b["total_paise"] for b in period.json()["buckets"])

    listing = client.get("/api/v1/transactions?date_from=2026-05-01&date_to=2026-05-31&limit=500")
    list_total = sum(t["amount_paise"] for t in listing.json() if t["transaction_type"] == "spend")

    category = client.get("/api/v1/dashboards/spend-by-category?month=2026-05")
    category_total = sum(r["total_paise"] for r in category.json()["rows"])

    assert period_total == list_total == category_total
    assert period_total == -15000 + 3000 - 2000  # income excluded


def test_period_week_total_matches_window_sum(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """Week-grain "nothing dropped / double-counted" guard.

    The week path's correctness rests on _bucket_of and _iter_periods agreeing
    on keys; the month invariant above doesn't exercise it. Over a week-aligned
    window (Mon 2025-12-29 .. Sun 2026-01-11 = 2026-W01..W02), assert Σ bucket
    totals == Σ confirmed spend+refund in the window (from the trusted list
    endpoint), and pin the per-bucket split.
    """
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2025, 12, 30),  # 2026-W01
                amount_paise=-15000,
                fingerprint="fp-w01-spend",
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 1, 2),  # 2026-W01
                amount_paise=3000,
                fingerprint="fp-w01-refund",
                transaction_type="spend",  # refund: spend row, positive amount (ADR-0009)
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 1, 6),  # 2026-W02
                amount_paise=-9000,
                fingerprint="fp-w02-spend",
            ),
            # Income — excluded from both sides.
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 1, 7),  # 2026-W02
                amount_paise=80000,
                fingerprint="fp-w02-income",
                transaction_type="income",
            ),
        ]
    )
    session.commit()

    period = client.get(f"{_PERIOD_URL}?bucket=week&start=2025-12-29&end=2026-01-11")
    buckets = period.json()["buckets"]
    assert buckets == [
        {"period": "2026-W01", "total_paise": -12000},  # -15000 + 3000
        {"period": "2026-W02", "total_paise": -9000},
    ]

    listing = client.get("/api/v1/transactions?date_from=2025-12-29&date_to=2026-01-11&limit=500")
    list_total = sum(t["amount_paise"] for t in listing.json() if t["transaction_type"] == "spend")
    assert sum(b["total_paise"] for b in buckets) == list_total == -21000


# ---- validation ------------------------------------------------------------


def test_period_start_after_end_returns_422(
    client: TestClient,
    seeded_user: User,
) -> None:
    resp = client.get(f"{_PERIOD_URL}?bucket=month&start=2026-06-30&end=2026-06-01")
    assert resp.status_code == 422
    assert resp.json()["detail"] == "start must be on or before end"


@pytest.mark.parametrize(
    "query",
    [
        "bucket=day&start=2026-01-01&end=2026-01-31",  # bucket not in {week, month}
        "bucket=week&start=2026-13-01&end=2026-01-31",  # malformed start date
        "bucket=week&start=2026-01-01",  # missing end (no server default)
        "start=2026-01-01&end=2026-01-31",  # missing bucket
    ],
)
def test_period_invalid_query_returns_422(
    client: TestClient,
    seeded_user: User,
    query: str,
) -> None:
    """One framework-validation guard: the route rejects garbage with 422.

    FastAPI's Literal / required-param / date parsing isn't exhaustively
    re-tested here — that's framework behaviour, not our logic.
    """
    resp = client.get(f"{_PERIOD_URL}?{query}")
    assert resp.status_code == 422


# =============================================================================
# period-totals (income vs spend over [start, end] — the /expenses summary strip)
#
# Sibling of spend-by-period but income-aware: income is INCLUDED (its own
# figure), spend+refund net into a signed expense, transfer excluded, net is
# income+expense server-side. Board-only.
# =============================================================================

_TOTALS_URL = "/api/v1/dashboards/period-totals"


def test_period_totals_empty_window_all_zero(
    client: TestClient,
    seeded_user: User,
) -> None:
    resp = client.get(f"{_TOTALS_URL}?start=2026-05-01&end=2026-05-31")
    assert resp.status_code == 200
    assert resp.json() == {
        "start": "2026-05-01",
        "end": "2026-05-31",
        "income_paise": 0,
        "expense_paise": 0,
        "net_paise": 0,
    }


def test_income_reversal_nets_income_down(
    client: TestClient,
    axis_account: Account,
) -> None:
    """ADR-0009 rule 5: income totals are signed sums, so an income reversal (a
    negative ``income`` row, written through the ordinary POST) nets income down
    rather than being dropped or counted as positive."""
    for amount, merchant in ((100000, "ACME Payroll"), (-30000, "ACME Payroll clawback")):
        resp = client.post(
            "/api/v1/transactions",
            json={
                "date": "2026-05-10",
                "account_id": axis_account.id,
                "amount_paise": amount,
                "transaction_type": "income",
                "merchant_raw": merchant,
            },
        )
        assert resp.status_code == 201, resp.text

    body = client.get(f"{_TOTALS_URL}?start=2026-05-01&end=2026-05-31").json()
    assert body["income_paise"] == 70000
    assert body["expense_paise"] == 0
    assert body["net_paise"] == 70000


def test_period_totals_income_spend_refund_net_and_transfer_excluded(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    session: Session,
) -> None:
    """expense = Σ signed(spend, refund); income = Σ income; net = income+expense;
    transfer is excluded entirely."""
    income_cat = next(c for c in seeded_categories if c.name == "Income")
    transfer_cat = next(c for c in seeded_categories if c.name == "Transfer")
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 3),
                amount_paise=-15000,
                fingerprint="fp-spend",
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 9),
                amount_paise=5000,
                fingerprint="fp-refund",
                transaction_type="spend",  # refund: spend row, positive amount (ADR-0009)
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 13),
                amount_paise=100000,
                fingerprint="fp-income",
                transaction_type="income",
                category_id=income_cat.id,
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 20),
                amount_paise=-50000,
                fingerprint="fp-transfer",
                transaction_type="transfer",
                category_id=transfer_cat.id,
            ),
        ]
    )
    session.commit()

    resp = client.get(f"{_TOTALS_URL}?start=2026-05-01&end=2026-05-31")
    assert resp.status_code == 200
    body = resp.json()
    # expense = -15000 + 5000 = -10000 (signed); income = 100000; net = 90000.
    assert body["expense_paise"] == -10000
    assert body["income_paise"] == 100000
    assert body["net_paise"] == 90000


def test_period_totals_excludes_pending(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    session: Session,
) -> None:
    income_cat = next(c for c in seeded_categories if c.name == "Income")
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 3),
                amount_paise=-15000,
                fingerprint="fp-confirmed",
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 4),
                amount_paise=99999,
                fingerprint="fp-pending-income",
                transaction_type="income",
                category_id=income_cat.id,
                confirmed_at=None,
            ),
        ]
    )
    session.commit()

    resp = client.get(f"{_TOTALS_URL}?start=2026-05-01&end=2026-05-31")
    body = resp.json()
    assert body["expense_paise"] == -15000
    assert body["income_paise"] == 0  # pending income excluded
    assert body["net_paise"] == -15000


def test_period_totals_start_after_end_returns_422(
    client: TestClient,
    seeded_user: User,
) -> None:
    resp = client.get(f"{_TOTALS_URL}?start=2026-06-30&end=2026-06-01")
    assert resp.status_code == 422
    assert resp.json()["detail"] == "start must be on or before end"


def test_period_totals_cross_user_isolation(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """Another user's income and spend must not appear in either figure.

    7 of 9 sibling dashboard routes had an isolation test; this one did not, and
    its user scoping lives INSIDE the very select(expense_sum, income_sum) whose
    case() pair a later consolidation merges. Lose the predicate there and another
    user's salary shows up in /expenses' income strip with every other dashboards
    test still green — which is precisely what this pins.
    """
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

    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 2, 10),
                amount_paise=-15000,
                fingerprint="fp-pt-v1-spend",
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 2, 11),
                amount_paise=50000,
                transaction_type="income",
                fingerprint="fp-pt-v1-income",
            ),
            # The other user's rows: a big spend AND a big income, so a dropped
            # predicate moves BOTH figures rather than only one.
            _make_txn(
                user_id=other_user.id,
                account_id=other_account.id,
                txn_date=date(2026, 2, 12),
                amount_paise=-99999,
                fingerprint="fp-pt-other-spend",
            ),
            _make_txn(
                user_id=other_user.id,
                account_id=other_account.id,
                txn_date=date(2026, 2, 13),
                amount_paise=777777,
                transaction_type="income",
                fingerprint="fp-pt-other-income",
            ),
        ]
    )
    session.commit()

    resp = client.get(f"{_TOTALS_URL}?start=2026-02-01&end=2026-02-28")
    assert resp.status_code == 200
    body = resp.json()
    assert body["income_paise"] == 50000
    assert body["expense_paise"] == -15000
    assert body["net_paise"] == 35000


# =============================================================================
# tagging-stats (F3 auto-tag acceptance metric, PRD §Success-metrics)
#
# Denominator = board rows the import auto-tagged to a still-live category
# (auto_category_id points at a non-archived bucket); kept = subset whose final
# category_id still equals that suggestion. Rows whose frozen suggestion is a
# since-archived category are excluded from both sides (current-health semantics,
# ADR-0004). Rate is None at zero denominator ("no data" ≠ "0%").
# =============================================================================

_TAGGING_URL = "/api/v1/dashboards/tagging-stats"


def test_tagging_stats_no_auto_tagged_rows_returns_none(
    client: TestClient,
    seeded_user: User,
) -> None:
    resp = client.get(_TAGGING_URL)
    assert resp.status_code == 200
    assert resp.json() == {
        "total_auto_tagged": 0,
        "kept": 0,
        "acceptance_rate": None,
        "rules_count": 0,
        "imported_total": 0,
        "pre_tagged": 0,
        "coverage_rate": None,
    }


def test_tagging_stats_kept_changed_cleared_and_manual(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    session: Session,
) -> None:
    """3 auto-tagged board rows in the denominator; 1 kept the suggestion.

    Rows: kept (cat==auto), changed (cat!=auto), cleared (cat=NULL, auto set →
    not kept), manual (auto=NULL → excluded from denominator). Only the first
    keeps its suggestion, so kept == 1. One merchant_tag_map row drives
    rules_count.

    All 4 rows are ``source="import"`` (the "manual" row is really an import
    row with no tag-map match, not a source="manual" row — see
    test_tagging_stats_coverage_excludes_manual_rows for that case), so
    imported_total == 4 and pre_tagged mirrors total_auto_tagged == 3.
    """
    food = next(c for c in seeded_categories if c.name == "Food")
    shopping = next(c for c in seeded_categories if c.name == "Shopping")
    session.add(
        MerchantTagMap(
            user_id=axis_account.user_id,
            merchant_normalized="swiggy",
            category_id=food.id,
            hit_count=3,
        )
    )
    session.add_all(
        [
            _make_txn(  # kept
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 1),
                amount_paise=-1000,
                fingerprint="fp-kept",
                category_id=food.id,
                auto_category_id=food.id,
            ),
            _make_txn(  # changed away from the suggestion
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 2),
                amount_paise=-2000,
                fingerprint="fp-changed",
                category_id=shopping.id,
                auto_category_id=food.id,
            ),
            _make_txn(  # cleared the category entirely → not kept
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 3),
                amount_paise=-3000,
                fingerprint="fp-cleared",
                category_id=None,
                auto_category_id=food.id,
            ),
            _make_txn(  # manual (no suggestion) → excluded from denominator
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 4),
                amount_paise=-4000,
                fingerprint="fp-manual",
                category_id=food.id,
                auto_category_id=None,
            ),
        ]
    )
    session.commit()

    resp = client.get(_TAGGING_URL)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_auto_tagged"] == 3
    assert body["kept"] == 1  # only fp-kept (cat==auto)
    assert body["acceptance_rate"] == pytest.approx(1 / 3)
    assert body["rules_count"] == 1
    assert body["imported_total"] == 4
    assert body["pre_tagged"] == 3
    assert body["coverage_rate"] == pytest.approx(3 / 4)


def test_tagging_stats_linking_an_auto_tagged_row_keeps_it_kept(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    session: Session,
) -> None:
    """A link clears ``category_id`` into ``pre_link_category_id`` (ADR-0002 rule 9); the
    acceptance metric must read the saved category, or every link would count an
    accepted auto-tag as rejected (PRD §Success-metrics)."""
    food = next(c for c in seeded_categories if c.name == "Food")
    bank = Account(
        user_id=axis_account.user_id, name="HDFC", type="bank", issuer="hdfc", last4=None
    )
    session.add(bank)
    session.flush()
    tagged = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 5, 1),
        amount_paise=-1000,
        fingerprint="fp-linked-kept",
        category_id=food.id,
        auto_category_id=food.id,
    )
    partner = _make_txn(
        user_id=axis_account.user_id,
        account_id=bank.id,
        txn_date=date(2026, 5, 1),
        amount_paise=1000,
        fingerprint="fp-linked-partner",
    )
    session.add_all([tagged, partner])
    session.commit()
    before = client.get(_TAGGING_URL).json()
    assert (before["total_auto_tagged"], before["kept"]) == (1, 1)

    pair_existing_rows(session, tagged, partner)
    session.commit()

    after = client.get(_TAGGING_URL).json()
    assert (after["total_auto_tagged"], after["kept"]) == (1, 1)


def test_tagging_stats_excludes_pending_rows(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    session: Session,
) -> None:
    """A pending (confirmed_at IS NULL) auto-tagged row is not yet accepted —
    it must not count toward the acceptance denominator, nor toward the
    coverage denominator (it isn't on the board at all yet)."""
    food = next(c for c in seeded_categories if c.name == "Food")
    session.add(
        _make_txn(
            user_id=axis_account.user_id,
            account_id=axis_account.id,
            txn_date=date(2026, 5, 1),
            amount_paise=-1000,
            fingerprint="fp-pending-auto",
            category_id=food.id,
            auto_category_id=food.id,
            confirmed_at=None,
        )
    )
    session.commit()

    resp = client.get(_TAGGING_URL)
    body = resp.json()
    assert body["total_auto_tagged"] == 0
    assert body["acceptance_rate"] is None
    assert body["imported_total"] == 0
    assert body["coverage_rate"] is None


def test_tagging_stats_excludes_archived_suggestion_rows(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    session: Session,
) -> None:
    """A frozen suggestion pointing at a since-archived category leaves the metric
    (current-health semantics, ADR-0004) — in BOTH directions:

    * an unchanged row that would otherwise read "kept forever" for a dead bucket,
    * a row re-bucketed away (F4a default-to-Other) that would otherwise read as a
      spurious "not-kept" and drag the rate down.

    Only the live-suggestion kept row survives, so the rate is a clean 1/1.
    """
    food = next(c for c in seeded_categories if c.name == "Food")
    shopping = next(c for c in seeded_categories if c.name == "Shopping")
    shopping.archived_at = datetime.now(UTC)
    session.add_all(
        [
            _make_txn(  # archived suggestion, still equal → would be "kept forever"
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 1),
                amount_paise=-1000,
                fingerprint="fp-archived-kept",
                category_id=shopping.id,
                auto_category_id=shopping.id,
            ),
            _make_txn(  # archived suggestion, re-bucketed away → spurious not-kept
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 2),
                amount_paise=-2000,
                fingerprint="fp-archived-rebucketed",
                category_id=food.id,
                auto_category_id=shopping.id,
            ),
            _make_txn(  # live suggestion, kept → the only counted row
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 3),
                amount_paise=-3000,
                fingerprint="fp-live-kept",
                category_id=food.id,
                auto_category_id=food.id,
            ),
        ]
    )
    session.commit()

    resp = client.get(_TAGGING_URL)
    body = resp.json()
    assert body["total_auto_tagged"] == 1  # only fp-live-kept
    assert body["kept"] == 1
    assert body["acceptance_rate"] == pytest.approx(1.0)


def test_tagging_stats_reject_to_since_archived_stays_counted(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    session: Session,
) -> None:
    """A row auto-tagged to a *live* bucket, then changed by the user to a bucket
    that is *later* archived, is a genuine reject — it must STAY in the denominator
    as not-kept. This pins the join column: the query joins on ``auto_category_id``
    (still live), never the final ``category_id`` (archived). A mistaken join on
    ``category_id`` would drop this row and pass every other tagging-stats test.
    """
    food = next(c for c in seeded_categories if c.name == "Food")
    shopping = next(c for c in seeded_categories if c.name == "Shopping")
    shopping.archived_at = datetime.now(UTC)
    session.add(
        _make_txn(
            user_id=axis_account.user_id,
            account_id=axis_account.id,
            txn_date=date(2026, 5, 1),
            amount_paise=-1000,
            fingerprint="fp-reject-to-archived",
            category_id=shopping.id,  # user changed away, then this bucket archived
            auto_category_id=food.id,  # suggestion is still live
        )
    )
    session.commit()

    resp = client.get(_TAGGING_URL)
    body = resp.json()
    assert body["total_auto_tagged"] == 1  # counted — suggestion (Food) is live
    assert body["kept"] == 0  # final category (Shopping) != suggestion (Food)
    assert body["acceptance_rate"] == pytest.approx(0.0)


def test_tagging_stats_excludes_foreign_category_suggestion(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    session: Session,
) -> None:
    """A v1 row whose ``auto_category_id`` points at ANOTHER user's category (the
    leak the join's ``Category.user_id == user_id`` guards, ADR-0003 rule 7) must be
    excluded. Pins the cross-user predicate: dropping it would count the foreign
    suggestion. The composite same-user FK refuses such a row (rule 10), so it is
    stored with FKs off: the read must hold on its own.
    """
    other_user = User(id=uuid.UUID("00000000-0000-0000-0000-000000000099"))
    session.add(other_user)
    session.commit()
    leaky_category = Category(
        color="#2a78d6",
        user_id=other_user.id,
        name="STOLEN-PROTECTED-NAME",
        is_seeded=False,
    )
    session.add(leaky_category)
    session.commit()
    session.refresh(leaky_category)

    food = next(c for c in seeded_categories if c.name == "Food")
    with foreign_keys_off(session):
        session.add_all(
            [
                _make_txn(  # legit v1 auto-tag → the only row that should count
                    user_id=axis_account.user_id,
                    account_id=axis_account.id,
                    txn_date=date(2026, 5, 1),
                    amount_paise=-1000,
                    fingerprint="fp-v1-legit",
                    category_id=food.id,
                    auto_category_id=food.id,
                ),
                _make_txn(  # v1 row whose suggestion points at the other user's category
                    user_id=axis_account.user_id,
                    account_id=axis_account.id,
                    txn_date=date(2026, 5, 2),
                    amount_paise=-2000,
                    fingerprint="fp-v1-foreign-suggestion",
                    category_id=food.id,
                    auto_category_id=leaky_category.id,
                ),
            ]
        )
        session.commit()

    resp = client.get(_TAGGING_URL)
    body = resp.json()
    assert body["total_auto_tagged"] == 1  # foreign-suggestion row excluded
    assert body["kept"] == 1


def test_tagging_stats_coverage_excludes_manual_rows(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    session: Session,
) -> None:
    """coverage_rate's denominator (imported_total) is source == "import" board
    rows only. A manual row must not count, even though it's confirmed and has
    no auto_category_id — same exclusion source="manual" already gets
    everywhere else, just applied to the new metric.
    """
    food = next(c for c in seeded_categories if c.name == "Food")
    session.add_all(
        [
            _make_txn(  # import row with no tag-map match → counts, untagged
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 1),
                amount_paise=-1000,
                fingerprint="fp-import-untagged",
                category_id=food.id,
                auto_category_id=None,
            ),
            _make_txn(  # manual row → must not count toward imported_total
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 2),
                amount_paise=-2000,
                fingerprint="fp-manual-row",
                category_id=food.id,
                auto_category_id=None,
                source="manual",
            ),
        ]
    )
    session.commit()

    resp = client.get(_TAGGING_URL)
    body = resp.json()
    assert body["imported_total"] == 1  # only fp-import-untagged
    assert body["pre_tagged"] == 0
    assert body["coverage_rate"] == pytest.approx(0.0)
    assert body["total_auto_tagged"] == 0
    assert body["acceptance_rate"] is None


def test_tagging_stats_coverage_excludes_non_auto_taggable_types(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    session: Session,
) -> None:
    """coverage_rate's denominator counts only AUTO_TAGGABLE_TYPES rows.

    ``import_service`` sets ``auto_category_id`` for spend rows only, so an
    income row (a CC bill payment, say) can never enter the numerator. Counting
    it in the denominator made the metric structurally unable to reach 100%:
    fully-covered spend alongside one income row read 0.5, so a bank statement
    dominated by transfers could report "0% pre-tagged" while auto-tagging
    worked perfectly.
    """
    food = next(c for c in seeded_categories if c.name == "Food")
    salary = next(c for c in seeded_categories if c.kind == "income")
    session.add_all(
        [
            _make_txn(  # spend, auto-tagged and kept → numerator + denominator
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 1),
                amount_paise=-1000,
                fingerprint="fp-spend-tagged",
                category_id=food.id,
                auto_category_id=food.id,
            ),
            _make_txn(  # imported income → neither, since it can't be auto-tagged
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 2),
                amount_paise=50000,
                fingerprint="fp-income-row",
                transaction_type="income",
                category_id=salary.id,
                auto_category_id=None,
            ),
        ]
    )
    session.commit()

    body = client.get(_TAGGING_URL).json()
    assert body["imported_total"] == 1  # the income row is not a coverage miss
    assert body["pre_tagged"] == 1
    assert body["coverage_rate"] == pytest.approx(1.0)  # 0.5 before the fix


def test_tagging_stats_rules_count_excludes_zero_hit_rows(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    session: Session,
) -> None:
    """A merchant_tag_map row at hit_count == 0 is the seeded-and-never-confirmed
    marker (merchant-alias arc decision 4) — excluded from rules_count so a
    fresh user's seeded dictionary doesn't inflate this count before any
    import runs. A learned row (hit_count >= 1) still counts.
    """
    food = next(c for c in seeded_categories if c.name == "Food")
    shopping = next(c for c in seeded_categories if c.name == "Shopping")
    session.add_all(
        [
            MerchantTagMap(
                user_id=axis_account.user_id,
                merchant_normalized="seeded-merchant",
                category_id=food.id,
                hit_count=0,
            ),
            MerchantTagMap(
                user_id=axis_account.user_id,
                merchant_normalized="learned-merchant",
                category_id=shopping.id,
                hit_count=2,
            ),
        ]
    )
    session.commit()

    resp = client.get(_TAGGING_URL)
    body = resp.json()
    assert body["rules_count"] == 1


def test_tagging_stats_rules_count_skips_unknown_via_rows(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    session: Session,
) -> None:
    """ADR-0011 rule 12: a map row whose key resolves to an unknown-via canonical is not
    a rule, so it is not counted."""
    food = next(c for c in seeded_categories if c.name == "Food")
    session.add_all(
        [
            MerchantTagMap(
                user_id=axis_account.user_id,
                merchant_normalized="upi/9876543210@ybl",
                category_id=food.id,
                hit_count=2,
            ),
            MerchantTagMap(
                user_id=axis_account.user_id,
                merchant_normalized="swiggy",
                category_id=food.id,
                hit_count=1,
            ),
        ]
    )
    session.commit()
    assert client.get(_TAGGING_URL).json()["rules_count"] == 1


def test_tagging_stats_rules_count_excludes_archived_categories(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    session: Session,
) -> None:
    """ADR-0004 rule 10: ``rules_count`` counts live rules — a rule whose category
    was archived drops out, like it does from the rules list and the prefill."""
    food = next(c for c in seeded_categories if c.name == "Food")
    shopping = next(c for c in seeded_categories if c.name == "Shopping")
    session.add_all(
        [
            MerchantTagMap(
                user_id=axis_account.user_id,
                merchant_normalized="a",
                category_id=food.id,
                hit_count=2,
            ),
            MerchantTagMap(
                user_id=axis_account.user_id,
                merchant_normalized="b",
                category_id=shopping.id,
                hit_count=2,
            ),
        ]
    )
    session.commit()
    assert client.get(_TAGGING_URL).json()["rules_count"] == 2

    assert client.delete(f"/api/v1/categories/{shopping.id}").status_code == 204

    assert client.get(_TAGGING_URL).json()["rules_count"] == 1


def test_tagging_stats_rules_count_counts_pinned_seed_rows(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    session: Session,
) -> None:
    """A pinned ``hit_count = 0`` row is user-authored, not seeded (ADR-0011 rule 11),
    so it counts; an unpinned one at zero is a never-confirmed seed and does not."""
    food = next(c for c in seeded_categories if c.name == "Food")
    shopping = next(c for c in seeded_categories if c.name == "Shopping")
    session.add_all(
        [
            MerchantTagMap(
                user_id=axis_account.user_id,
                merchant_normalized="pinned-seed",
                category_id=food.id,
                hit_count=0,
                pinned=True,
            ),
            MerchantTagMap(
                user_id=axis_account.user_id,
                merchant_normalized="plain-seed",
                category_id=shopping.id,
                hit_count=0,
            ),
        ]
    )
    session.commit()

    assert client.get(_TAGGING_URL).json()["rules_count"] == 1


# =============================================================================
# overview (Financial Overview home — PRD §F8 view 1 + view 4)
#
# Per-account balances (opening_balance + board-only signed txn sum, archived
# included, all-time) + net worth + portfolio value (null-NAV → 0) + the month's
# income/expense/net (mirrors period-totals). Balances are all-time; only the
# income/expense/net figures are month-scoped.
# =============================================================================

_OVERVIEW_URL = "/api/v1/dashboards/overview"


def test_overview_empty_no_accounts(client: TestClient, seeded_user: User) -> None:
    resp = client.get(f"{_OVERVIEW_URL}?month=2026-05")
    assert resp.status_code == 200
    assert resp.json() == {
        "period": "2026-05",
        "net_worth_paise": 0,
        "portfolio_value_paise": 0,
        "fx_unavailable_count": 0,
        "income_paise": 0,
        "expense_paise": 0,
        "net_paise": 0,
        "accounts": [],
        "gross_spend_paise": 0,
        "fronted_peer_entries_paise": 0,
        "net_personal_spend_paise": 0,
        "fronted_card_usage_paise": 0,
    }


def test_overview_account_balance_net_worth_and_period(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """A confirmed CC spend surfaces as year-to-date spend and the month's signed
    expense, but does NOT move net worth — credit cards are excluded from it
    (spend channels, not liabilities)."""
    session.add(
        _make_txn(
            user_id=axis_account.user_id,
            account_id=axis_account.id,
            txn_date=date(2026, 5, 10),
            amount_paise=-15000,
            fingerprint="fp-ov-spend",
        )
    )
    session.commit()

    body = client.get(f"{_OVERVIEW_URL}?month=2026-05").json()
    assert body["accounts"] == [
        {
            "account_id": axis_account.id,
            "name": "Axis CC",
            "type": "credit_card",
            "currency": "INR",
            "balance_paise": -15000,  # opening 0 + (-15000), all-time
            "spend_ytd_paise": -15000,  # signed net spend, Jan 1 → end of May
            "gross_spend_ytd_paise": -15000,
            "refund_ytd_paise": 0,
            "cashback_ytd_paise": 0,
            "archived": False,
        }
    ]
    assert body["net_worth_paise"] == 0  # CC excluded from net worth
    assert body["portfolio_value_paise"] == 0
    assert body["expense_paise"] == -15000
    assert body["income_paise"] == 0
    assert body["net_paise"] == -15000


def test_overview_pending_txn_excluded_from_balance(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """confirmed_at IS NULL (review queue) rows don't move the balance."""
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 10),
                amount_paise=-10000,
                fingerprint="fp-ov-confirmed",
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 11),
                amount_paise=-99999,
                fingerprint="fp-ov-pending",
                confirmed_at=None,
            ),
        ]
    )
    session.commit()

    body = client.get(f"{_OVERVIEW_URL}?month=2026-05").json()
    assert body["accounts"][0]["balance_paise"] == -10000
    # CC excluded from net worth; the balance_paise check above carries the
    # pending-exclusion assertion.
    assert body["net_worth_paise"] == 0


def test_overview_opening_balance_included(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    """balance = opening_balance_paise + Σ confirmed txns."""
    bank = Account(
        user_id=seeded_user.id,
        name="HDFC Savings",
        type="bank",
        issuer="hdfc",
        opening_balance_paise=500000,
    )
    session.add(bank)
    session.commit()
    session.refresh(bank)

    session.add(
        _make_txn(
            user_id=seeded_user.id,
            account_id=bank.id,
            txn_date=date(2026, 5, 10),
            amount_paise=-20000,
            fingerprint="fp-ov-bank",
        )
    )
    session.commit()

    body = client.get(f"{_OVERVIEW_URL}?month=2026-05").json()
    assert body["accounts"][0]["balance_paise"] == 480000  # 500000 - 20000
    assert body["net_worth_paise"] == 480000


def test_overview_archived_account_with_balance_included(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    """An archived account that still holds a balance stays in the accounts list
    (flagged) and in net worth — archiving must not silently drop money."""
    archived = Account(
        user_id=seeded_user.id,
        name="Closed Wallet",
        type="cash",
        opening_balance_paise=30000,
        archived_at=datetime.now(UTC),
    )
    session.add(archived)
    session.commit()
    session.refresh(archived)

    body = client.get(f"{_OVERVIEW_URL}?month=2026-05").json()
    assert body["accounts"] == [
        {
            "account_id": archived.id,
            "name": "Closed Wallet",
            "type": "cash",
            "currency": "INR",
            "balance_paise": 30000,
            "spend_ytd_paise": None,  # non-CC rows carry no YTD-spend figure
            "gross_spend_ytd_paise": None,
            "refund_ytd_paise": None,
            "cashback_ytd_paise": None,
            "archived": True,
        }
    ]
    assert body["net_worth_paise"] == 30000


def test_overview_balance_is_all_time_but_period_is_monthly(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """An April spend is in the (all-time) balance but not in a May period
    figure — pins the balance/period scope distinction."""
    session.add(
        _make_txn(
            user_id=axis_account.user_id,
            account_id=axis_account.id,
            txn_date=date(2026, 4, 20),
            amount_paise=-7000,
            fingerprint="fp-ov-april",
        )
    )
    session.commit()

    body = client.get(f"{_OVERVIEW_URL}?month=2026-05").json()
    assert body["accounts"][0]["balance_paise"] == -7000  # all-time
    # YTD spend spans the whole year-to-date, so the April spend is in it …
    assert body["accounts"][0]["spend_ytd_paise"] == -7000
    assert body["net_worth_paise"] == 0  # CC excluded
    assert body["expense_paise"] == 0  # … but nothing in the May-only period
    assert body["net_paise"] == 0


def test_overview_net_worth_includes_portfolio(
    client: TestClient,
    axis_account: Account,
    instrument: Instrument,
    session: Session,
) -> None:
    """Portfolio current value (Σ over NAV-bearing holdings) rolls into net
    worth. The CC spend rides on spend_ytd_paise but is excluded from net worth,
    so net worth here is portfolio-only."""
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 10),
                amount_paise=-15000,
                fingerprint="fp-ov-cc",
            ),
            InvestmentTransaction(
                user_id=instrument.user_id,
                account_id=_default_inv_account_id(session, instrument.user_id),
                instrument_id=instrument.id,
                date=date(2026, 5, 1),
                transaction_type="buy",
                units=Decimal("10"),
                price_per_unit_native=Decimal("140"),
                amount_native_paise=140000,
                fees_native_paise=0,
                fx_rate_to_inr=Decimal("1"),
            ),
        ]
    )
    session.commit()

    body = client.get(f"{_OVERVIEW_URL}?month=2026-05").json()
    # 10 units × NAV 150 × 100 paise = 150000.
    assert body["portfolio_value_paise"] == 150000
    # net worth = portfolio only (150000); the CC (-15000) is excluded.
    assert body["net_worth_paise"] == 150000


def _usd_instrument_with_buy(session: Session, user_id: UUID) -> None:
    """Seed a priced USD holding: 10 units of a $150-NAV equity, one buy."""
    inst = Instrument(
        user_id=user_id,
        symbol="AAPL",
        name="Apple",
        asset_class="us_equity",
        currency="USD",
        exchange="NASDAQ",
        current_nav=Decimal("150"),
    )
    session.add(inst)
    session.flush()
    session.add(
        InvestmentTransaction(
            user_id=user_id,
            account_id=_default_inv_account_id(session, user_id),
            instrument_id=inst.id,
            date=date(2026, 5, 1),
            transaction_type="buy",
            units=Decimal("10"),
            price_per_unit_native=Decimal("100"),
            amount_native_paise=100_000,
            fees_native_paise=0,
            fx_rate_to_inr=Decimal("80"),
        )
    )
    session.commit()


def test_overview_includes_priced_usd_holding(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    """A priced USD holding converts at the newest cached rate and rolls into net
    worth — the bug was overview() calling compute_holdings without an fx rate,
    so USD holdings fell out as FX-unavailable.

    The quote date is FIXED, not ``date.today()``: the route reads ``latest_rate``, which
    has no as-of date, so nothing here may depend on what the host thinks today is. With
    the old ``rate_on(on=date.today())`` read this test was silently host-coupled — see
    ``test_overview_uses_newest_rate_even_if_dated_ahead_of_utc_today``."""
    session.add(
        FxRateQuote(
            date=date(2026, 5, 20),
            from_currency="USD",
            to_currency="INR",
            rate=Decimal("83"),
            source="seed",
        )
    )
    session.commit()
    _usd_instrument_with_buy(session, seeded_user.id)

    body = client.get(f"{_OVERVIEW_URL}?month=2026-05").json()
    # 10 units × NAV 150 × 100 paise = 150000 cents, × today's rate 83 → INR paise.
    assert body["portfolio_value_paise"] == 150_000 * 83
    assert body["net_worth_paise"] == 150_000 * 83
    assert body["fx_unavailable_count"] == 0


def test_overview_uses_newest_rate_even_if_dated_ahead_of_utc_today(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    """A cache whose only rate is dated AFTER ``clock.today()`` still values the USD holding.

    This is the 00:00–05:30 IST window, made deterministic. ``rate_on`` is
    ``date <= on`` carry-forward, so under the old ``rate_on(on=date.today())`` read this
    row was found on the native IST host (local today) and MISSED once the read moved to
    UTC — dropping the USD leg out of ``net_worth_paise`` for 5.5 hours a day while the
    Docker UTC stack reported a different number for identical data. ``latest_rate`` has no
    date predicate, so there is no window to be in.
    """
    session.add(
        FxRateQuote(
            date=clock.today() + timedelta(days=1),
            from_currency="USD",
            to_currency="INR",
            rate=Decimal("83"),
            source="seed",
        )
    )
    session.commit()
    _usd_instrument_with_buy(session, seeded_user.id)

    body = client.get(f"{_OVERVIEW_URL}?month=2026-05").json()
    assert body["portfolio_value_paise"] == 150_000 * 83
    assert body["fx_unavailable_count"] == 0


def test_overview_usd_holding_without_fx_rate_excluded(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    """No cached USD→INR rate → the priced USD holding can't roll up to INR, so
    it is excluded from net worth AND surfaced via fx_unavailable_count (the
    exclusion is honest, not silent)."""
    _usd_instrument_with_buy(session, seeded_user.id)  # no FxRateQuote seeded

    body = client.get(f"{_OVERVIEW_URL}?month=2026-05").json()
    assert body["portfolio_value_paise"] == 0
    assert body["net_worth_paise"] == 0
    assert body["fx_unavailable_count"] == 1


def test_overview_null_nav_holding_contributes_zero(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    """A holding whose instrument has no NAV counts as ₹0 in portfolio value
    (never its cost) — same rule as /holdings."""
    no_nav = Instrument(
        user_id=seeded_user.id,
        symbol="INF000NONAV1",
        name="Unpriced Fund",
        asset_class="indian_mf",
        currency="INR",
        exchange="MFCentral",
        current_nav=None,
    )
    session.add(no_nav)
    session.commit()
    session.refresh(no_nav)

    session.add(
        InvestmentTransaction(
            user_id=seeded_user.id,
            account_id=_default_inv_account_id(session, seeded_user.id),
            instrument_id=no_nav.id,
            date=date(2026, 5, 1),
            transaction_type="buy",
            units=Decimal("10"),
            price_per_unit_native=Decimal("100"),
            amount_native_paise=100000,
            fees_native_paise=0,
            fx_rate_to_inr=Decimal("1"),
        )
    )
    session.commit()

    body = client.get(f"{_OVERVIEW_URL}?month=2026-05").json()
    assert body["portfolio_value_paise"] == 0
    assert body["net_worth_paise"] == 0


def test_overview_income_expense_match_period_totals(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    session: Session,
) -> None:
    """The overview's month figures equal the trusted period-totals endpoint
    over the same window (transfer excluded from both)."""
    income_cat = next(c for c in seeded_categories if c.name == "Income")
    transfer_cat = next(c for c in seeded_categories if c.name == "Transfer")
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 3),
                amount_paise=-15000,
                fingerprint="fp-ov-spend",
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 9),
                amount_paise=5000,
                fingerprint="fp-ov-refund",
                transaction_type="spend",  # refund: spend row, positive amount (ADR-0009)
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 13),
                amount_paise=100000,
                fingerprint="fp-ov-income",
                transaction_type="income",
                category_id=income_cat.id,
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 20),
                amount_paise=-50000,
                fingerprint="fp-ov-transfer",
                transaction_type="transfer",
                category_id=transfer_cat.id,
            ),
        ]
    )
    session.commit()

    overview = client.get(f"{_OVERVIEW_URL}?month=2026-05").json()
    totals = client.get(f"{_TOTALS_URL}?start=2026-05-01&end=2026-05-31").json()
    assert overview["income_paise"] == totals["income_paise"] == 100000
    assert overview["expense_paise"] == totals["expense_paise"] == -10000
    assert overview["net_paise"] == totals["net_paise"] == 90000


def test_overview_credit_card_excluded_from_net_worth(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """A credit card is a spend channel, not a liability: it's excluded from net
    worth entirely (bill payments aren't recorded, so its balance is accumulated
    spend, not real debt) — whether the balance is net-negative or net-positive.
    A sibling bank account pins that real assets still count. Also asserts that
    spend_ytd_paise counts only spend/refund (the income/credit is excluded).
    """
    bank = Account(
        user_id=axis_account.user_id,
        name="HDFC Savings",
        type="bank",
        issuer="hdfc",
        opening_balance_paise=100000,
    )
    session.add(bank)
    session.commit()

    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 3),
                amount_paise=-20000,
                fingerprint="fp-cc-spend",
            ),
            # A bill-payment credit larger than the spend → the CC goes
            # net-positive (the bug's trigger: it used to read as an asset).
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 5),
                amount_paise=65000,
                fingerprint="fp-cc-credit",
                transaction_type="income",
            ),
        ]
    )
    session.commit()

    body = client.get(f"{_OVERVIEW_URL}?month=2026-05").json()
    cc_row = next(a for a in body["accounts"] if a["type"] == "credit_card")
    bank_row = next(a for a in body["accounts"] if a["type"] == "bank")
    # The CC's stored balance is net-positive and still surfaces as-is …
    assert cc_row["balance_paise"] == 45000  # -20000 + 65000
    # … but YTD spend counts only spend/refund, so the +65000 income is NOT in it.
    assert cc_row["spend_ytd_paise"] == -20000
    assert bank_row["balance_paise"] == 100000
    assert bank_row["spend_ytd_paise"] is None  # non-CC → no YTD-spend figure
    # The CC contributes 0 to net worth — excluded entirely. Net worth = bank asset.
    assert body["net_worth_paise"] == 100000


def test_overview_investment_account_excluded_from_net_worth(
    client: TestClient,
    instrument: Instrument,
    session: Session,
) -> None:
    """An investment account is a placeholder — its opening balance is out of net
    worth, so itemising the same money as holdings can't double-count it (B#11).

    This is the *legacy-row* case, and the only one that matters: ``AccountCreate``
    now rejects a non-zero investment opening balance, but ``opening_balance_paise``
    is locked on PATCH and ``DELETE`` is a soft archive that net worth deliberately
    still counts, so a row created before that rule has no in-app correction path.
    The exclusion is what makes it harmless. The account still surfaces in
    ``accounts[]`` with its raw signed balance — the panel's job is unchanged.
    """
    user_id = instrument.user_id
    zerodha = Account(
        user_id=user_id,
        name="Zerodha",
        type="investment",
        issuer=None,
        last4=None,
        # The doc's scenario: ₹30,00,000 recorded as an un-itemised portfolio …
        opening_balance_paise=3_000_000_00,
    )
    bank = Account(
        user_id=user_id,
        name="HDFC Savings",
        type="bank",
        issuer="hdfc",
        opening_balance_paise=100000,
    )
    session.add_all([zerodha, bank])
    # … and then itemised as the same ₹30,00,000 of holdings. Units chosen so the
    # rollup is exact (20000 × NAV 150 × 100), keeping the assertion independent
    # of the rounding rule.
    session.add(
        InvestmentTransaction(
            user_id=user_id,
            account_id=_default_inv_account_id(session, user_id),
            instrument_id=instrument.id,
            date=date(2026, 5, 1),
            transaction_type="buy",
            units=Decimal("20000"),
            price_per_unit_native=Decimal("150"),
            amount_native_paise=3_000_000_00,
            fees_native_paise=0,
            fx_rate_to_inr=Decimal("1"),
        )
    )
    session.commit()

    body = client.get(f"{_OVERVIEW_URL}?month=2026-05").json()
    inv_row = next(a for a in body["accounts"] if a["type"] == "investment")
    # The panel still reports the raw signed balance (same contract as a CC's).
    assert inv_row["balance_paise"] == 3_000_000_00
    assert inv_row["spend_ytd_paise"] is None  # non-CC → no YTD-spend figure
    assert body["portfolio_value_paise"] == 3_000_000_00
    # Net worth = bank + portfolio. The investment account's ₹30L is NOT added on
    # top of the holdings that represent the same money — that sum (₹60,01,000)
    # is the defect.
    assert body["net_worth_paise"] == 100000 + 3_000_000_00


def test_overview_cc_spend_ytd_is_signed_net_within_year(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """spend_ytd_paise is the signed net Σ(spend, refund) over the calendar
    year-to-date (from 1 January, PRD §F8): a refund nets against spends, and a
    prior-year spend — December of the previous year — is excluded from the YTD
    figure while still counting in the all-time balance."""
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 2, 10),
                amount_paise=-30000,
                fingerprint="fp-ytd-feb",
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 4, 5),
                amount_paise=-20000,
                fingerprint="fp-ytd-apr",
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 4, 15),
                amount_paise=8000,
                fingerprint="fp-ytd-refund",
                transaction_type="spend",  # refund: spend row, positive amount (ADR-0009)
            ),
            # Prior year: in the all-time balance, but NOT in the 2026 YTD figure.
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2025, 12, 20),
                amount_paise=-50000,
                fingerprint="fp-ytd-prioryear",
            ),
        ]
    )
    session.commit()

    row = client.get(f"{_OVERVIEW_URL}?month=2026-05").json()["accounts"][0]
    # 2026 YTD [2026-01-01, 2026-05-31]: -30000 - 20000 + 8000 = -42000 (refund
    # nets; the Dec 2025 spend belongs to 2025).
    assert row["spend_ytd_paise"] == -42000
    # All-time balance carries every row: -30000 - 20000 + 8000 - 50000 = -92000.
    assert row["balance_paise"] == -92000


def test_ytd_starts_on_1_january(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """PRD §F8: on the spend side, "year" and every YTD figure mean the calendar
    year, 1 January – 31 December (only tax stays on the financial year). The
    window derives strictly from the ?month= / ?year= param, never the server
    clock, and each boundary is inclusive. ``?year=2026`` is echoed verbatim."""
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2025, 1, 1),  # first day of 2025
                amount_paise=-5000,
                fingerprint="fp-cy25-first",
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2025, 12, 31),  # last day of 2025
                amount_paise=-11000,
                fingerprint="fp-cy25-last",
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 1, 1),  # first day of 2026
                amount_paise=-22000,
                fingerprint="fp-cy26-first",
            ),
        ]
    )
    session.commit()

    jan = client.get(f"{_OVERVIEW_URL}?month=2026-01").json()["accounts"][0]
    dec = client.get(f"{_OVERVIEW_URL}?month=2025-12").json()["accounts"][0]
    apr = client.get(f"{_OVERVIEW_URL}?month=2026-04").json()["accounts"][0]
    # 2026 YTD [2026-01-01, 2026-01-31] catches only the 1 January spend.
    assert jan["spend_ytd_paise"] == -22000
    # 2025 YTD [2025-01-01, 2025-12-31] catches both of that year's ends.
    assert dec["spend_ytd_paise"] == -16000
    # April is NOT a new year: 2026 YTD [2026-01-01, 2026-04-30] still holds 1 January.
    assert apr["spend_ytd_paise"] == -22000

    y26 = client.get(f"{_OVERVIEW_URL}?year=2026").json()
    assert y26["period"] == "2026"
    assert y26["expense_paise"] == -22000  # 2026-01-01 .. 2026-12-31
    assert y26["accounts"][0]["spend_ytd_paise"] == -22000
    y25 = client.get(f"{_OVERVIEW_URL}?year=2025").json()
    assert y25["expense_paise"] == -16000  # 2025-01-01 .. 2025-12-31
    by_cat = client.get("/api/v1/dashboards/spend-by-category?year=2026").json()
    assert sum(r["total_paise"] for r in by_cat["rows"]) == -22000
    # A year's previous period is the previous calendar year: view 2's delta for
    # ?year=2026 asks spend-by-category for ?year=2025 (lib/period.ts previousPeriod).
    prev = client.get("/api/v1/dashboards/spend-by-category?year=2025").json()
    assert prev["period"] == "2025"
    assert sum(r["total_paise"] for r in prev["rows"]) == -16000


@pytest.mark.parametrize(
    ("instant", "year"),
    [
        # 23:30 IST on 31 December 2026: still 2026.
        (datetime(2026, 12, 31, 18, 0, tzinfo=UTC), 2026),
        # 01:00 IST on 1 January 2027: the business date (ADR-0001 rule 5c) is in
        # 2027, though the UTC date is still 31 December 2026.
        (datetime(2026, 12, 31, 19, 30, tzinfo=UTC), 2027),
    ],
    ids=["2330-ist-31-dec", "0100-ist-1-jan"],
)
def test_available_years_offers_the_current_year_of_the_business_date(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    instant: datetime,
    year: int,
) -> None:
    """With no data, the only year offered is the calendar year of today's IST date."""
    monkeypatch.setattr(clock, "utcnow", lambda: instant)

    resp = client.get("/api/v1/dashboards/available-years")
    assert resp.status_code == 200
    assert resp.json()["years"] == [year]


def test_overview_spend_ytd_is_per_credit_card(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """Two credit cards get independent spend_ytd_paise — the account_id-keyed
    map must not collapse or cross-assign totals."""
    icici = Account(
        user_id=axis_account.user_id,
        name="ICICI CC",
        type="credit_card",
        issuer="icici",
        last4="5678",
    )
    session.add(icici)
    session.commit()
    session.refresh(icici)

    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 3, 1),
                amount_paise=-13000,
                fingerprint="fp-two-axis",
            ),
            _make_txn(
                user_id=icici.user_id,
                account_id=icici.id,
                txn_date=date(2026, 3, 2),
                amount_paise=-77000,
                fingerprint="fp-two-icici-spend",
            ),
            _make_txn(
                user_id=icici.user_id,
                account_id=icici.id,
                txn_date=date(2026, 3, 8),
                amount_paise=7000,
                fingerprint="fp-two-icici-refund",
                transaction_type="spend",  # refund: spend row, positive amount (ADR-0009)
            ),
        ]
    )
    session.commit()

    # March 2026: the 2026 YTD these rows are in.
    body = client.get(f"{_OVERVIEW_URL}?month=2026-03").json()
    by_name = {a["name"]: a for a in body["accounts"]}
    assert by_name["Axis CC"]["spend_ytd_paise"] == -13000
    assert by_name["ICICI CC"]["spend_ytd_paise"] == -70000  # -77000 + 7000
    # Both accounts are credit cards → net worth is zero (both excluded).
    assert body["net_worth_paise"] == 0


def test_overview_spend_ytd_null_for_non_credit_card_types(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """Only credit_card rows carry spend_ytd_paise; bank / cash rows are None
    (not 0), pinning "not applicable" apart from a genuine zero spend."""
    session.add_all(
        [
            Account(
                user_id=axis_account.user_id,
                name="HDFC Savings",
                type="bank",
                issuer="hdfc",
                opening_balance_paise=100000,
            ),
            Account(
                user_id=axis_account.user_id,
                name="Wallet",
                type="cash",
                opening_balance_paise=5000,
            ),
        ]
    )
    session.commit()

    accounts = client.get(f"{_OVERVIEW_URL}?month=2026-05").json()["accounts"]
    by_type = {a["type"]: a for a in accounts}
    assert by_type["credit_card"]["spend_ytd_paise"] == 0  # CC, no txns → 0
    assert by_type["bank"]["spend_ytd_paise"] is None
    assert by_type["cash"]["spend_ytd_paise"] is None


@pytest.mark.parametrize("month", ["2026-13", "2026-1", "abc", ""])
def test_overview_invalid_month_returns_422(
    client: TestClient,
    seeded_user: User,
    month: str,
) -> None:
    resp = client.get(f"{_OVERVIEW_URL}?month={month}")
    assert resp.status_code == 422


# =============================================================================
# cashflow-by-period (income vs spend + net, series form — the /spending
# "am I solvent" chart, PRD §F8 view 3)
#
# The series generalization of period-totals: income INCLUDED (its own figure),
# spend+refund net into a signed expense, transfer excluded, net = income +
# expense server-side, per bucket. Reuses spend_by_period's _bucket_of /
# _iter_periods / window-filter / zero-fill, so the headline assertions are the
# income/expense split, the signed-expense contract (never clamped), and the
# negative-net deficit case (the whole reason the chart exists).
# =============================================================================

_CASHFLOW_URL = "/api/v1/dashboards/cashflow-by-period"


def test_cashflow_empty_window_zero_filled(
    client: TestClient,
    seeded_user: User,
) -> None:
    """No activity → every month is {income:0, expense:0, net:0}, envelope echoed."""
    resp = client.get(f"{_CASHFLOW_URL}?bucket=month&start=2026-01-01&end=2026-02-28")
    assert resp.status_code == 200
    assert resp.json() == {
        "bucket": "month",
        "start": "2026-01-01",
        "end": "2026-02-28",
        "buckets": [
            {"period": "2026-01", "income_paise": 0, "expense_paise": 0, "net_paise": 0},
            {"period": "2026-02", "income_paise": 0, "expense_paise": 0, "net_paise": 0},
        ],
    }


def test_cashflow_month_income_spend_refund_net(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    session: Session,
) -> None:
    """One month: income = Σincome; expense = signed Σ(spend, refund) (refund
    nets); net = income + expense."""
    income_cat = next(c for c in seeded_categories if c.name == "Income")
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 3),
                amount_paise=-15000,
                fingerprint="fp-spend",
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 9),
                amount_paise=5000,
                fingerprint="fp-refund",
                transaction_type="spend",  # refund: spend row, positive amount (ADR-0009)
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 13),
                amount_paise=100000,
                fingerprint="fp-income",
                transaction_type="income",
                category_id=income_cat.id,
            ),
        ]
    )
    session.commit()

    resp = client.get(f"{_CASHFLOW_URL}?bucket=month&start=2026-05-01&end=2026-05-31")
    assert resp.status_code == 200
    # expense = -15000 + 5000 = -10000 (signed); income = 100000; net = 90000.
    assert resp.json()["buckets"] == [
        {
            "period": "2026-05",
            "income_paise": 100000,
            "expense_paise": -10000,
            "net_paise": 90000,
        }
    ]


def test_cashflow_deficit_month_net_negative(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    session: Session,
) -> None:
    """Headline requirement: when spend magnitude exceeds income, net_paise is
    strictly negative in the payload (the 2025-10 Diwali deficit, in miniature)."""
    income_cat = next(c for c in seeded_categories if c.name == "Income")
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2025, 10, 5),
                amount_paise=50000,
                fingerprint="fp-salary",
                transaction_type="income",
                category_id=income_cat.id,
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2025, 10, 20),
                amount_paise=-60000,
                fingerprint="fp-diwali",
            ),
        ]
    )
    session.commit()

    resp = client.get(f"{_CASHFLOW_URL}?bucket=month&start=2025-10-01&end=2025-10-31")
    bucket = resp.json()["buckets"][0]
    assert bucket["income_paise"] == 50000
    assert bucket["expense_paise"] == -60000
    assert bucket["net_paise"] == -10000
    assert bucket["net_paise"] < 0  # the deficit is the whole point


def test_cashflow_refund_dominant_bucket_expense_positive_not_clamped(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """Signed-expense contract: a bucket where refunds outweigh spend surfaces a
    POSITIVE expense_paise — the server does not clamp to 0 (clamping would break
    net = income + expense). net stays exact."""
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 3),
                amount_paise=-5000,
                fingerprint="fp-spend",
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 9),
                amount_paise=8000,
                fingerprint="fp-big-refund",
                transaction_type="spend",  # refund: spend row, positive amount (ADR-0009)
            ),
        ]
    )
    session.commit()

    resp = client.get(f"{_CASHFLOW_URL}?bucket=month&start=2026-05-01&end=2026-05-31")
    bucket = resp.json()["buckets"][0]
    # expense = -5000 + 8000 = +3000 (NOT clamped to 0); income 0; net = +3000.
    assert bucket["expense_paise"] == 3000
    assert bucket["income_paise"] == 0
    assert bucket["net_paise"] == 3000


def test_cashflow_excludes_transfer(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    session: Session,
) -> None:
    transfer_cat = next(c for c in seeded_categories if c.name == "Transfer")
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 3),
                amount_paise=-15000,
                fingerprint="fp-spend",
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 20),
                amount_paise=-50000,
                fingerprint="fp-transfer",
                transaction_type="transfer",
                category_id=transfer_cat.id,
            ),
        ]
    )
    session.commit()

    resp = client.get(f"{_CASHFLOW_URL}?bucket=month&start=2026-05-01&end=2026-05-31")
    # Only the -15000 spend; the transfer is neither income nor expense.
    assert resp.json()["buckets"] == [
        {"period": "2026-05", "income_paise": 0, "expense_paise": -15000, "net_paise": -15000}
    ]


def test_cashflow_excludes_pending(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    session: Session,
) -> None:
    """confirmed_at IS NULL (review queue) rows are off the dashboard."""
    income_cat = next(c for c in seeded_categories if c.name == "Income")
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 3),
                amount_paise=-15000,
                fingerprint="fp-confirmed",
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 4),
                amount_paise=99999,
                fingerprint="fp-pending-income",
                transaction_type="income",
                category_id=income_cat.id,
                confirmed_at=None,
            ),
        ]
    )
    session.commit()

    resp = client.get(f"{_CASHFLOW_URL}?bucket=month&start=2026-05-01&end=2026-05-31")
    assert resp.json()["buckets"] == [
        {"period": "2026-05", "income_paise": 0, "expense_paise": -15000, "net_paise": -15000}
    ]


def test_cashflow_cross_user_isolation(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
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

    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 3),
                amount_paise=-15000,
                fingerprint="fp-v1",
            ),
            _make_txn(
                user_id=other_user.id,
                account_id=other_account.id,
                txn_date=date(2026, 5, 4),
                amount_paise=99999,
                fingerprint="fp-other-user",
                transaction_type="income",
            ),
        ]
    )
    session.commit()

    resp = client.get(f"{_CASHFLOW_URL}?bucket=month&start=2026-05-01&end=2026-05-31")
    # Only v1's spend; the other user's income is absent.
    assert resp.json()["buckets"] == [
        {"period": "2026-05", "income_paise": 0, "expense_paise": -15000, "net_paise": -15000}
    ]


def test_cashflow_month_zero_fill_gap(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """A month with no in-window activity is a {0,0,0} bucket, not a gap."""
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 1, 15),
                amount_paise=-10000,
                fingerprint="fp-jan",
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 3, 15),
                amount_paise=-30000,
                fingerprint="fp-mar",
            ),
        ]
    )
    session.commit()

    resp = client.get(f"{_CASHFLOW_URL}?bucket=month&start=2026-01-01&end=2026-03-31")
    assert resp.json()["buckets"] == [
        {"period": "2026-01", "income_paise": 0, "expense_paise": -10000, "net_paise": -10000},
        {"period": "2026-02", "income_paise": 0, "expense_paise": 0, "net_paise": 0},
        {"period": "2026-03", "income_paise": 0, "expense_paise": -30000, "net_paise": -30000},
    ]


def test_cashflow_week_bucket_zero_fill(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    session: Session,
) -> None:
    """Week-grain zero-fill (per-route wiring): income in 2026-W01, spend in
    2026-W03, W02 empty. Window Mon 2025-12-29 .. Sun 2026-01-18 = W01..W03."""
    income_cat = next(c for c in seeded_categories if c.name == "Income")
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2025, 12, 30),  # 2026-W01
                amount_paise=40000,
                fingerprint="fp-w01-income",
                transaction_type="income",
                category_id=income_cat.id,
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 1, 13),  # 2026-W03
                amount_paise=-30000,
                fingerprint="fp-w03-spend",
            ),
        ]
    )
    session.commit()

    resp = client.get(f"{_CASHFLOW_URL}?bucket=week&start=2025-12-29&end=2026-01-18")
    assert resp.json()["buckets"] == [
        {"period": "2026-W01", "income_paise": 40000, "expense_paise": 0, "net_paise": 40000},
        {"period": "2026-W02", "income_paise": 0, "expense_paise": 0, "net_paise": 0},
        {"period": "2026-W03", "income_paise": 0, "expense_paise": -30000, "net_paise": -30000},
    ]


def test_cashflow_start_after_end_returns_422(
    client: TestClient,
    seeded_user: User,
) -> None:
    resp = client.get(f"{_CASHFLOW_URL}?bucket=month&start=2026-06-30&end=2026-06-01")
    assert resp.status_code == 422
    assert resp.json()["detail"] == "start must be on or before end"


def test_cashflow_reconciles_with_period_totals_and_spend_by_period(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    session: Session,
) -> None:
    """Ties the new route to the two trusted aggregates over a month-aligned
    window: Σ cashflow.income == period-totals.income, and Σ cashflow.expense ==
    Σ spend-by-period bucket totals. Transfer excluded from all three."""
    income_cat = next(c for c in seeded_categories if c.name == "Income")
    transfer_cat = next(c for c in seeded_categories if c.name == "Transfer")
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 3),
                amount_paise=-15000,
                fingerprint="fp-food",
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 9),
                amount_paise=3000,
                fingerprint="fp-refund",
                transaction_type="spend",  # refund: spend row, positive amount (ADR-0009)
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 13),
                amount_paise=80000,
                fingerprint="fp-income",
                transaction_type="income",
                category_id=income_cat.id,
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 20),
                amount_paise=-50000,
                fingerprint="fp-transfer",
                transaction_type="transfer",
                category_id=transfer_cat.id,
            ),
        ]
    )
    session.commit()

    cashflow = client.get(f"{_CASHFLOW_URL}?bucket=month&start=2026-05-01&end=2026-05-31").json()
    cf_income = sum(b["income_paise"] for b in cashflow["buckets"])
    cf_expense = sum(b["expense_paise"] for b in cashflow["buckets"])
    cf_net = sum(b["net_paise"] for b in cashflow["buckets"])

    totals = client.get(f"{_TOTALS_URL}?start=2026-05-01&end=2026-05-31").json()
    period = client.get(f"{_PERIOD_URL}?bucket=month&start=2026-05-01&end=2026-05-31").json()
    period_expense = sum(b["total_paise"] for b in period["buckets"])

    assert cf_income == totals["income_paise"] == 80000
    assert cf_expense == period_expense == totals["expense_paise"] == -12000  # -15000 + 3000
    assert cf_net == totals["net_paise"] == 68000


# =============================================================================
# top-merchants (PRD §F8 view 3 — "where is the money actually going?")
#
# Month-scoped GROUP BY merchant_normalized: signed-sum per merchant, ordered
# most-negative first, capped at `limit`, no-merchant bucket excluded. Shared
# semantics with spend-by-category (type filter, confirmed-only, cross-user,
# signed sum nets refunds) are re-asserted so a change to only one endpoint is
# caught. Headline behaviours: empty-bucket exclusion, MAX label over raw
# variants, "top N of M" (total_merchants / truncated), net-credit sorts last.
#
# No `bucket` param → no week path; the per-route week-bucket assertion rule
# (which applies to the [start,end] period routes) doesn't apply here.
# =============================================================================

_TOP_MERCHANTS_URL = "/api/v1/dashboards/top-merchants"


def test_top_merchants_empty_month_returns_empty(
    client: TestClient,
    seeded_user: User,
) -> None:
    resp = client.get(f"{_TOP_MERCHANTS_URL}?month=2026-05")
    assert resp.status_code == 200
    assert resp.json() == {
        "period": "2026-05",
        "rows": [],
        "total_merchants": 0,
        "truncated": False,
        "label_id": None,
    }


def test_top_merchants_single_merchant(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    session.add(
        _make_txn(
            user_id=axis_account.user_id,
            account_id=axis_account.id,
            txn_date=date(2026, 5, 10),
            amount_paise=-15000,
            fingerprint="fp-swiggy",
            merchant_raw="Swiggy",
        )
    )
    session.commit()

    resp = client.get(f"{_TOP_MERCHANTS_URL}?month=2026-05")
    assert resp.status_code == 200
    body = resp.json()
    assert body["period"] == "2026-05"
    assert body["total_merchants"] == 1
    assert body["truncated"] is False
    assert body["rows"] == [
        {
            "merchant_canonical": normalize_merchant("Swiggy"),
            "merchant_label": "Swiggy",
            "total_paise": -15000,
        }
    ]


def test_top_merchants_ranked_most_negative_first_with_tiebreak(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """Biggest spender first; equal totals break by merchant_normalized ascending."""
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 3),
                amount_paise=-50000,
                fingerprint="fp-amazon",
                merchant_raw="Amazon",
            ),
            # Two merchants tied at -10000: "Big Basket" < "Swiggy" (normalized asc).
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 5),
                amount_paise=-10000,
                fingerprint="fp-swiggy",
                merchant_raw="Swiggy",
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 7),
                amount_paise=-10000,
                fingerprint="fp-bigbasket",
                merchant_raw="Big Basket",
            ),
        ]
    )
    session.commit()

    resp = client.get(f"{_TOP_MERCHANTS_URL}?month=2026-05")
    rows = resp.json()["rows"]
    assert [r["merchant_canonical"] for r in rows] == [
        normalize_merchant("Amazon"),
        normalize_merchant("Big Basket"),
        normalize_merchant("Swiggy"),
    ]
    assert [r["total_paise"] for r in rows] == [-50000, -10000, -10000]


def test_top_merchants_groups_by_canonical(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """ADR-0011 rule 8: descriptors folded by the user's aliases are one bar, keyed on the
    canonical. A second user's alias must not leak in."""
    uid = axis_account.user_id
    other = User(id=uuid.uuid4())
    session.add(other)
    session.flush()
    session.add_all(
        [
            MerchantAlias(user_id=uid, pattern="swiggy", canonical="swiggy"),
            MerchantAlias(user_id=uid, pattern="bigbasket", canonical="big basket"),
            MerchantAlias(user_id=other.id, pattern="zomato", canonical="swiggy"),
        ]
    )
    for n, (raw, paise) in enumerate(
        [
            ("UPI/SWIGGY/1111@YBL", -10000),
            ("SWIGGY*BLR*2222", -20000),
            ("Swiggy", -5000),
            ("BIGBASKET ONLINE", -7000),
            ("Zomato", -1000),
        ]
    ):
        session.add(
            _make_txn(
                user_id=uid,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 3 + n),
                amount_paise=paise,
                fingerprint=f"fp-canon-{n}",
                merchant_raw=raw,
            )
        )
    session.commit()

    body = client.get(f"{_TOP_MERCHANTS_URL}?month=2026-05").json()
    assert body["rows"] == [
        {"merchant_canonical": "swiggy", "merchant_label": "Swiggy", "total_paise": -35000},
        {"merchant_canonical": "big basket", "merchant_label": "big basket", "total_paise": -7000},
        {"merchant_canonical": "zomato", "merchant_label": "Zomato", "total_paise": -1000},
    ]
    assert body["total_merchants"] == 3
    capped = client.get(f"{_TOP_MERCHANTS_URL}?month=2026-05&limit=1").json()
    assert capped["truncated"] is True
    assert capped["total_merchants"] == 3


def test_top_merchants_raw_variants_group_under_one_normalized_key(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """Different raw strings normalizing to the same key collapse to one group;
    the summed total is signed and the label is a representative raw (MAX)."""
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 3),
                amount_paise=-20000,
                fingerprint="fp-swiggy-1",
                merchant_raw="SWIGGY",
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 4),
                amount_paise=-10000,
                fingerprint="fp-swiggy-2",
                merchant_raw="Swiggy",
            ),
        ]
    )
    session.commit()

    resp = client.get(f"{_TOP_MERCHANTS_URL}?month=2026-05")
    body = resp.json()
    assert body["total_merchants"] == 1
    assert len(body["rows"]) == 1
    row = body["rows"][0]
    assert row["merchant_canonical"] == normalize_merchant("Swiggy")
    assert row["total_paise"] == -30000
    # Label is a deterministic pick of one of the raw variants (MAX); don't
    # over-pin which one (it's collation-dependent), just that it's a real raw.
    assert row["merchant_label"] in {"SWIGGY", "Swiggy"}


def test_top_merchants_empty_merchant_bucket_filtered(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """A no-merchant row (merchant_raw="" → merchant_normalized="") is excluded
    from both `rows` and `total_merchants`, even when it's the biggest spend."""
    session.add_all(
        [
            # Biggest spend, but no merchant → must not appear.
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 3),
                amount_paise=-99999,
                fingerprint="fp-no-merchant",
                merchant_raw="",
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 5),
                amount_paise=-5000,
                fingerprint="fp-uber",
                merchant_raw="Uber",
            ),
        ]
    )
    session.commit()

    resp = client.get(f"{_TOP_MERCHANTS_URL}?month=2026-05")
    body = resp.json()
    assert body["total_merchants"] == 1
    assert [r["merchant_canonical"] for r in body["rows"]] == [normalize_merchant("Uber")]
    assert -99999 not in [r["total_paise"] for r in body["rows"]]


def test_top_merchants_truncation_and_count_rows_invariants(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """`limit` caps `rows`; `total_merchants` is the full pre-LIMIT distinct count;
    `truncated` = total_merchants > limit. Pins both count/rows invariants."""
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 3),
                amount_paise=-30000,
                fingerprint="fp-amazon",
                merchant_raw="Amazon",
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 4),
                amount_paise=-20000,
                fingerprint="fp-swiggy",
                merchant_raw="Swiggy",
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 5),
                amount_paise=-10000,
                fingerprint="fp-uber",
                merchant_raw="Uber",
            ),
        ]
    )
    session.commit()

    # Truncated: limit below the distinct count.
    trunc = client.get(f"{_TOP_MERCHANTS_URL}?month=2026-05&limit=2").json()
    assert trunc["total_merchants"] == 3
    assert trunc["truncated"] is True
    assert len(trunc["rows"]) == 2  # == limit
    # The two most-negative, in order.
    assert [r["merchant_canonical"] for r in trunc["rows"]] == [
        normalize_merchant("Amazon"),
        normalize_merchant("Swiggy"),
    ]

    # Not truncated: limit >= distinct count → all rows shown.
    full = client.get(f"{_TOP_MERCHANTS_URL}?month=2026-05&limit=8").json()
    assert full["total_merchants"] == 3
    assert full["truncated"] is False
    assert len(full["rows"]) == full["total_merchants"] == 3


def test_top_merchants_net_credit_merchant_sorts_last(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """A refund-dominant merchant has a positive net total and sorts after every
    spender (ascending signed order) — the frontend renders it apart, not as a bar."""
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 3),
                amount_paise=-10000,
                fingerprint="fp-amazon-spend",
                merchant_raw="Amazon",
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 5),
                amount_paise=5000,
                fingerprint="fp-flipkart-refund",
                transaction_type="spend",  # refund: spend row, positive amount (ADR-0009)
                merchant_raw="Flipkart",
            ),
        ]
    )
    session.commit()

    resp = client.get(f"{_TOP_MERCHANTS_URL}?month=2026-05")
    rows = resp.json()["rows"]
    assert [r["merchant_canonical"] for r in rows] == [
        normalize_merchant("Amazon"),
        normalize_merchant("Flipkart"),
    ]
    assert [r["total_paise"] for r in rows] == [-10000, 5000]


def test_top_merchants_all_net_credit_month(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """A month with only refunds → every row is positive (no spend bar). Pins the
    'position doesn't imply sign' contract the frontend relies on."""
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 3),
                amount_paise=3000,
                fingerprint="fp-amazon-refund",
                transaction_type="spend",  # refund: spend row, positive amount (ADR-0009)
                merchant_raw="Amazon",
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 5),
                amount_paise=5000,
                fingerprint="fp-flipkart-refund",
                transaction_type="spend",  # refund: spend row, positive amount (ADR-0009)
                merchant_raw="Flipkart",
            ),
        ]
    )
    session.commit()

    resp = client.get(f"{_TOP_MERCHANTS_URL}?month=2026-05")
    body = resp.json()
    assert body["total_merchants"] == 2
    assert body["truncated"] is False
    # Ascending signed order: +3000 before +5000.
    assert [r["total_paise"] for r in body["rows"]] == [3000, 5000]
    assert all(r["total_paise"] > 0 for r in body["rows"])


def test_top_merchants_excludes_income_transfer_and_pending(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    session: Session,
) -> None:
    """income + transfer excluded by type; pending (confirmed_at IS NULL) off the
    board. Only the confirmed spend merchant survives."""
    income_cat = next(c for c in seeded_categories if c.name == "Income")
    transfer_cat = next(c for c in seeded_categories if c.name == "Transfer")
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 3),
                amount_paise=-10000,
                fingerprint="fp-spend",
                merchant_raw="Amazon",
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 4),
                amount_paise=100000,
                fingerprint="fp-income",
                transaction_type="income",
                merchant_raw="Employer",
                category_id=income_cat.id,
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 5),
                amount_paise=-50000,
                fingerprint="fp-transfer",
                transaction_type="transfer",
                merchant_raw="Self",
                category_id=transfer_cat.id,
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 6),
                amount_paise=-99999,
                fingerprint="fp-pending",
                merchant_raw="Croma",
                confirmed_at=None,
            ),
        ]
    )
    session.commit()

    resp = client.get(f"{_TOP_MERCHANTS_URL}?month=2026-05")
    body = resp.json()
    assert body["total_merchants"] == 1
    assert [r["merchant_canonical"] for r in body["rows"]] == [normalize_merchant("Amazon")]
    assert body["rows"][0]["total_paise"] == -10000


def test_top_merchants_cross_user_isolation(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
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

    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 3),
                amount_paise=-10000,
                fingerprint="fp-v1",
                merchant_raw="Amazon",
            ),
            _make_txn(
                user_id=other_user.id,
                account_id=other_account.id,
                txn_date=date(2026, 5, 4),
                amount_paise=-99999,
                fingerprint="fp-other-user",
                merchant_raw="Amazon",
            ),
        ]
    )
    session.commit()

    resp = client.get(f"{_TOP_MERCHANTS_URL}?month=2026-05")
    body = resp.json()
    # Only v1's -10000 Amazon; the other user's -99999 is absent (not summed in).
    assert body["total_merchants"] == 1
    assert body["rows"] == [
        {
            "merchant_canonical": normalize_merchant("Amazon"),
            "merchant_label": "Amazon",
            "total_paise": -10000,
        }
    ]


def test_top_merchants_reconciles_with_spend_by_period(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """Over a month with **only merchant-bearing** spend rows (no empty bucket, no
    refunds), Σ(all merchant totals) == the month's spend-by-period total. Pure
    spend keeps the truncated magnitude check clean: Σ|shown| ≤ Σ|month spend|."""
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 3),
                amount_paise=-50000,
                fingerprint="fp-amazon",
                merchant_raw="Amazon",
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 5),
                amount_paise=-10000,
                fingerprint="fp-swiggy",
                merchant_raw="Swiggy",
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 7),
                amount_paise=-8000,
                fingerprint="fp-uber",
                merchant_raw="Uber",
            ),
        ]
    )
    session.commit()

    period = client.get(f"{_PERIOD_URL}?bucket=month&start=2026-05-01&end=2026-05-31")
    period_total = sum(b["total_paise"] for b in period.json()["buckets"])

    # Full: limit >= distinct merchants → signed identity holds exactly.
    full = client.get(f"{_TOP_MERCHANTS_URL}?month=2026-05&limit=50").json()
    assert full["truncated"] is False
    assert sum(r["total_paise"] for r in full["rows"]) == period_total == -68000

    # Truncated to the single biggest spender: shown magnitude ≤ month spend
    # magnitude (pure spend, so no net-credit can invert this).
    trunc = client.get(f"{_TOP_MERCHANTS_URL}?month=2026-05&limit=1").json()
    assert trunc["truncated"] is True
    shown_magnitude = sum(-r["total_paise"] for r in trunc["rows"])
    assert shown_magnitude == 50000
    assert shown_magnitude <= -period_total  # 50000 <= 68000


@pytest.mark.parametrize("limit", [0, 51, -1])
def test_top_merchants_invalid_limit_returns_422(
    client: TestClient,
    seeded_user: User,
    limit: int,
) -> None:
    """`limit` is bounded ge=1, le=50 (house guardrail); out-of-range → 422."""
    resp = client.get(f"{_TOP_MERCHANTS_URL}?month=2026-05&limit={limit}")
    assert resp.status_code == 422


@pytest.mark.parametrize(
    "month",
    ["2026-13", "2026-1", "2026-00", "2026/05", "abc", ""],
)
def test_top_merchants_invalid_month_returns_422_without_echoing_input(
    client: TestClient,
    seeded_user: User,
    month: str,
) -> None:
    resp = client.get(f"{_TOP_MERCHANTS_URL}?month={month}")
    assert resp.status_code == 422
    body = resp.json()
    if isinstance(body.get("detail"), str):
        assert body["detail"] == "month must match YYYY-MM"
    assert month == "" or month not in resp.text


def test_top_merchants_missing_month_returns_422(
    client: TestClient,
    seeded_user: User,
) -> None:
    resp = client.get(_TOP_MERCHANTS_URL)
    assert resp.status_code == 422


# =============================================================================
# F3a tag cross-filter (?label_id=) — tag-analysis arc Phase A
#
# Optional label filter added to spend-by-category, top-merchants,
# spend-by-period, and category-trend. It's an EXISTS subquery
# (Transaction.labels.any(Label.id == label_id)), copied from the
# transactions.py list route — so a txn either matches the tag or doesn't (no
# join-row duplication, no double-counting), and every response echoes the
# active `label_id` (None when unfiltered), mirroring the month/start/end echo.
#
# Each test asserts (a) the filter isolates to tagged txns, (b) the unfiltered
# path is unchanged (regression), and (c) the echoed label_id. Labels apply to
# spending txns only, so these all use spend/refund rows.
# =============================================================================

_SBC_URL = "/api/v1/dashboards/spend-by-category"


def _tag_txns(
    session: Session,
    *,
    user_id: UUID,
    name: str,
    txns: list[Transaction],
) -> Label:
    """Create label ``name`` for ``user_id`` and attach it to each already-
    persisted txn in ``txns``; return the label.

    Mirrors the Label + TransactionLabel construction in test_transactions.py.
    Caller must have committed ``txns`` first (the join rows need their ids).
    """
    label = Label(user_id=user_id, name=name)
    session.add(label)
    session.flush()
    for txn in txns:
        session.add(TransactionLabel(transaction_id=txn.id, label_id=label.id, user_id=user_id))
    session.commit()
    return label


def test_spend_by_category_label_filter_scopes_to_tagged(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    session: Session,
) -> None:
    """?label_id= narrows the breakdown to the tagged txn's category; the
    unfiltered call still sees both categories."""
    food = next(c for c in seeded_categories if c.name == "Food")
    shopping = next(c for c in seeded_categories if c.name == "Shopping")
    tagged = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 5, 10),
        amount_paise=-15000,
        fingerprint="fp-food",
        category_id=food.id,
    )
    plain = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 5, 11),
        amount_paise=-50000,
        fingerprint="fp-shop",
        category_id=shopping.id,
    )
    session.add_all([tagged, plain])
    session.commit()
    travel = _tag_txns(session, user_id=axis_account.user_id, name="travel", txns=[tagged])

    # Unfiltered: both categories, no echoed filter (regression guard).
    unfiltered = client.get(f"{_SBC_URL}?month=2026-05").json()
    assert unfiltered["label_id"] is None
    assert {r["category_id"] for r in unfiltered["rows"]} == {food.id, shopping.id}

    # Filtered: only the tagged txn's category, echoed label_id.
    body = client.get(f"{_SBC_URL}?month=2026-05&label_id={travel.id}").json()
    assert body["label_id"] == travel.id
    assert body["rows"] == [
        {
            "category_id": food.id,
            "category_name": "Food",
            "parent_id": None,
            "parent_name": None,
            "display_color": "#2a78d6",
            "parent_display_color": None,
            "total_paise": -15000,
        },
    ]


def test_spend_by_category_multi_tagged_txn_not_double_counted(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    session: Session,
) -> None:
    """A txn carrying two labels contributes its amount ONCE under a filter —
    guards the EXISTS subquery against a regression to an inner JOIN (which
    would multiply the category sum by the number of matching links)."""
    food = next(c for c in seeded_categories if c.name == "Food")
    txn = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 5, 10),
        amount_paise=-15000,
        fingerprint="fp-food",
        category_id=food.id,
    )
    session.add(txn)
    session.commit()
    # Two labels on the one txn.
    travel = _tag_txns(session, user_id=axis_account.user_id, name="travel", txns=[txn])
    _tag_txns(session, user_id=axis_account.user_id, name="work", txns=[txn])

    body = client.get(f"{_SBC_URL}?month=2026-05&label_id={travel.id}").json()
    assert body["rows"] == [
        {
            "category_id": food.id,
            "category_name": "Food",
            "parent_id": None,
            "parent_name": None,
            "display_color": "#2a78d6",
            "parent_display_color": None,
            "total_paise": -15000,
        },
    ]


def test_spend_by_period_label_filter_scopes_bucket_totals(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """The month bucket sums only the tagged txn under the filter; unfiltered
    sums both."""
    tagged = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 5, 10),
        amount_paise=-15000,
        fingerprint="fp-a",
    )
    plain = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 5, 20),
        amount_paise=-50000,
        fingerprint="fp-b",
    )
    session.add_all([tagged, plain])
    session.commit()
    travel = _tag_txns(session, user_id=axis_account.user_id, name="travel", txns=[tagged])

    url = f"{_PERIOD_URL}?bucket=month&start=2026-05-01&end=2026-05-31"
    unfiltered = client.get(url).json()
    assert unfiltered["label_id"] is None
    assert unfiltered["buckets"] == [{"period": "2026-05", "total_paise": -65000}]

    body = client.get(f"{url}&label_id={travel.id}").json()
    assert body["label_id"] == travel.id
    assert body["buckets"] == [{"period": "2026-05", "total_paise": -15000}]


def test_top_merchants_label_filter_scopes_rows_and_total_merchants(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """The filter scopes both the ranked rows AND total_merchants, so the
    "top N of M" caption stays honest (the count query gets the same clause)."""
    tagged = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 5, 10),
        amount_paise=-15000,
        fingerprint="fp-swiggy",
        merchant_raw="Swiggy",
    )
    plain = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 5, 12),
        amount_paise=-50000,
        fingerprint="fp-amazon",
        merchant_raw="Amazon",
    )
    session.add_all([tagged, plain])
    session.commit()
    travel = _tag_txns(session, user_id=axis_account.user_id, name="travel", txns=[tagged])

    unfiltered = client.get(f"{_TOP_MERCHANTS_URL}?month=2026-05").json()
    assert unfiltered["total_merchants"] == 2

    body = client.get(f"{_TOP_MERCHANTS_URL}?month=2026-05&label_id={travel.id}").json()
    assert body["label_id"] == travel.id
    assert body["total_merchants"] == 1
    assert body["truncated"] is False
    assert [r["merchant_canonical"] for r in body["rows"]] == [normalize_merchant("Swiggy")]


@pytest.mark.parametrize("bad", ["0", "-1"])
def test_label_filter_rejects_non_positive_id(
    client: TestClient,
    seeded_user: User,
    bad: str,
) -> None:
    """label_id has Query(gt=0), so 0 / negative is a 422 before the query runs."""
    resp = client.get(f"{_SBC_URL}?month=2026-05&label_id={bad}")
    assert resp.status_code == 422


def test_label_filter_unknown_label_returns_empty(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    session: Session,
) -> None:
    """A label_id matching none of the user's txns (unknown / another user's)
    yields an empty breakdown, not an error — and still echoes the id."""
    food = next(c for c in seeded_categories if c.name == "Food")
    session.add(
        _make_txn(
            user_id=axis_account.user_id,
            account_id=axis_account.id,
            txn_date=date(2026, 5, 10),
            amount_paise=-15000,
            fingerprint="fp-food",
            category_id=food.id,
        )
    )
    session.commit()

    body = client.get(f"{_SBC_URL}?month=2026-05&label_id=999999").json()
    assert body["label_id"] == 999999
    assert body["rows"] == []


# =============================================================================
# GET /dashboards/spend-by-tag — spend-by-tag breakdown + coverage (arc Phase B)
#
# The tag analog of spend-by-category, but the per-tag rows come from a JOIN +
# GROUP BY over transaction_labels (arc decision #7's group-by shape), which
# INTENTIONALLY double-counts a multi-tagged txn across its tags — so Σ(rows)
# legitimately overshoots the month total. Coverage therefore can't sum the rows:
# `total_spend_paise` is a separate un-joined Σ, the untagged bucket is the
# NOT-EXISTS complement, and `tagged_paise = total − untagged` counts each tagged
# txn once. `coverage_rate` is the signed ratio when it lands in [0, 1], else None
# (no spend, or refund-skew) — the signed building blocks are never clamped.
#
# These reuse `_make_txn` / `_tag_txns` and assert (a) per-tag signed sums,
# (b) the intended double-count in rows vs the once-count in coverage, (c) the
# bottom-pinned untagged bucket, and (d) the money-math parity (type/board/sign)
# every dashboard route shares. Labels apply to spending txns only.
# =============================================================================

_SBT_URL = "/api/v1/dashboards/spend-by-tag"


def test_spend_by_tag_empty_month_all_zero(
    client: TestClient,
    seeded_user: User,
) -> None:
    """No txns → no rows, zero totals, and coverage None (no denominator)."""
    body = client.get(f"{_SBT_URL}?month=2026-05").json()
    assert body == {
        "period": "2026-05",
        "rows": [],
        "total_spend_paise": 0,
        "tagged_paise": 0,
        "coverage_rate": None,
    }


def test_spend_by_tag_single_tag_plus_untagged_bucket(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """One tagged spend + one untagged spend: a tag row, then the untagged bucket
    row (label_id=None) pinned last; coverage = tagged / total, hand-computed; the
    signed partition identity holds."""
    tagged = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 5, 10),
        amount_paise=-15000,
        fingerprint="fp-tagged",
    )
    untagged = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 5, 11),
        amount_paise=-5000,
        fingerprint="fp-untagged",
    )
    session.add_all([tagged, untagged])
    session.commit()
    travel = _tag_txns(session, user_id=axis_account.user_id, name="travel", txns=[tagged])

    body = client.get(f"{_SBT_URL}?month=2026-05").json()
    assert body["period"] == "2026-05"
    assert body["rows"] == [
        {"label_id": travel.id, "label_name": "travel", "total_paise": -15000},
        {"label_id": None, "label_name": None, "total_paise": -5000},
    ]
    assert body["total_spend_paise"] == -20000
    assert body["tagged_paise"] == -15000
    assert body["coverage_rate"] == 0.75
    # Signed partition identity: total == tagged + untagged bucket row.
    untagged_row = body["rows"][-1]
    assert body["total_spend_paise"] == body["tagged_paise"] + untagged_row["total_paise"]


def test_spend_by_tag_multi_tagged_double_counts_in_rows_but_once_in_coverage(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """THE definitional Phase B assertion (mirror-opposite of Phase A's
    ``…_not_double_counted``): a txn carrying two tags appears under BOTH tag rows
    with its FULL amount (the group-by double-count is intended, arc decision #7),
    so Σ(rows) overshoots the month total — but coverage counts the txn ONCE
    (``tagged_paise`` is the single amount, never the doubled sum)."""
    txn = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 5, 10),
        amount_paise=-15000,
        fingerprint="fp-multi",
    )
    session.add(txn)
    session.commit()
    travel = _tag_txns(session, user_id=axis_account.user_id, name="travel", txns=[txn])
    work = _tag_txns(session, user_id=axis_account.user_id, name="work", txns=[txn])

    body = client.get(f"{_SBT_URL}?month=2026-05").json()
    # Both tags carry the full amount — the intended double-count.
    assert body["rows"] == [
        {"label_id": travel.id, "label_name": "travel", "total_paise": -15000},
        {"label_id": work.id, "label_name": "work", "total_paise": -15000},
    ]
    # Σ(rows) overshoots — correct and never "fixed".
    assert sum(r["total_paise"] for r in body["rows"]) == -30000
    # Coverage counts the txn once: tagged == the single amount, not -30000.
    assert body["total_spend_paise"] == -15000
    assert body["tagged_paise"] == -15000
    assert body["coverage_rate"] == 1.0


def test_spend_by_tag_refund_nets_within_a_tag(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """A spend and a refund carrying the same tag net into one signed row
    (PRD §F4a rule 3), exactly like spend-by-category."""
    spend = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 5, 10),
        amount_paise=-15000,
        fingerprint="fp-spend",
    )
    refund = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 5, 12),
        amount_paise=5000,
        fingerprint="fp-refund",
        transaction_type="spend",  # refund: spend row, positive amount (ADR-0009)
    )
    session.add_all([spend, refund])
    session.commit()
    travel = _tag_txns(session, user_id=axis_account.user_id, name="travel", txns=[spend, refund])

    body = client.get(f"{_SBT_URL}?month=2026-05").json()
    assert body["rows"] == [
        {"label_id": travel.id, "label_name": "travel", "total_paise": -10000},
    ]
    assert body["total_spend_paise"] == -10000
    assert body["coverage_rate"] == 1.0


def test_spend_by_tag_net_credit_tag_surfaces_positive_after_spends(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """A tag whose refunds outweigh its in-window spend surfaces with a POSITIVE
    total and sorts after every net-spend tag (most-negative-first order)."""
    food_spend = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 5, 10),
        amount_paise=-10000,
        fingerprint="fp-food",
    )
    travel_spend = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 5, 11),
        amount_paise=-5000,
        fingerprint="fp-tspend",
    )
    travel_refund = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 5, 12),
        amount_paise=8000,
        fingerprint="fp-trefund",
        transaction_type="spend",  # refund: spend row, positive amount (ADR-0009)
    )
    session.add_all([food_spend, travel_spend, travel_refund])
    session.commit()
    foodtag = _tag_txns(session, user_id=axis_account.user_id, name="food", txns=[food_spend])
    travel = _tag_txns(
        session,
        user_id=axis_account.user_id,
        name="travel",
        txns=[travel_spend, travel_refund],
    )

    body = client.get(f"{_SBT_URL}?month=2026-05").json()
    # food (-10000) is the biggest spender → first; travel nets to +3000 → last.
    assert body["rows"] == [
        {"label_id": foodtag.id, "label_name": "food", "total_paise": -10000},
        {"label_id": travel.id, "label_name": "travel", "total_paise": 3000},
    ]
    assert body["total_spend_paise"] == -7000


def test_spend_by_tag_untagged_bucket_pinned_last_regardless_of_magnitude(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """The untagged bucket stays last even when it is the largest magnitude —
    arc decision #4 (bottom-pinned regardless of size), mirroring the
    uncategorized bucket."""
    small_tagged = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 5, 10),
        amount_paise=-1000,
        fingerprint="fp-small",
    )
    big_untagged = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 5, 11),
        amount_paise=-50000,
        fingerprint="fp-big",
    )
    session.add_all([small_tagged, big_untagged])
    session.commit()
    travel = _tag_txns(session, user_id=axis_account.user_id, name="travel", txns=[small_tagged])

    body = client.get(f"{_SBT_URL}?month=2026-05").json()
    # Despite -50000 << -1000, the untagged bucket is LAST, not sorted to the top.
    assert [r["label_id"] for r in body["rows"]] == [travel.id, None]
    assert body["rows"][-1] == {"label_id": None, "label_name": None, "total_paise": -50000}
    # Coverage reflects the low tagged share: 1000 / 51000.
    assert body["tagged_paise"] == -1000
    assert body["total_spend_paise"] == -51000
    assert body["coverage_rate"] == 1000 / 51000


def test_spend_by_tag_coverage_none_when_refund_skew_exceeds_one(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """Refund-skew edge: untagged rows net to a CREDIT while the month is net-spend,
    so the signed ratio > 1 → coverage_rate is None, but the signed building blocks
    stay honest and are NEVER clamped (arc decision #6)."""
    tagged_spend = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 5, 10),
        amount_paise=-100000,
        fingerprint="fp-tspend",
    )
    untagged_refund = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 5, 12),
        amount_paise=20000,
        fingerprint="fp-urefund",
        transaction_type="spend",  # refund: spend row, positive amount (ADR-0009)
    )
    session.add_all([tagged_spend, untagged_refund])
    session.commit()
    _tag_txns(session, user_id=axis_account.user_id, name="travel", txns=[tagged_spend])

    body = client.get(f"{_SBT_URL}?month=2026-05").json()
    # tagged (-100000) / total (-80000) = 1.25 → outside [0, 1] → None.
    assert body["coverage_rate"] is None
    # …but the raw signed figures are honest, not clamped.
    assert body["total_spend_paise"] == -80000
    assert body["tagged_paise"] == -100000
    # The untagged credit surfaces as a positive bottom-pinned bucket row.
    assert body["rows"][-1] == {"label_id": None, "label_name": None, "total_paise": 20000}


def test_spend_by_tag_coverage_rate_is_positive_zero_when_all_untagged(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """An all-untagged month yields ``tagged_paise = 0`` over a NEGATIVE denominator,
    i.e. ``-0.0`` — which serialises as ``-0.0`` and renders as "-0%".

    ``coverage_rate == 0.0`` is NOT sufficient here: ``-0.0 == 0.0`` in IEEE-754, so
    that assertion passes both before and after the fix. ``copysign`` inspects the
    sign bit, which is the only thing that changed.
    """
    session.add(
        _make_txn(
            user_id=axis_account.user_id,
            account_id=axis_account.id,
            txn_date=date(2026, 5, 10),
            amount_paise=-25075,
            fingerprint="fp-all-untagged",
        )
    )
    session.commit()

    body = client.get(f"{_SBT_URL}?month=2026-05").json()
    assert body["total_spend_paise"] == -25075
    assert body["tagged_paise"] == 0
    assert body["coverage_rate"] == 0.0
    assert math.copysign(1.0, body["coverage_rate"]) > 0


def test_spend_by_tag_excludes_income_and_transfer(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """A tag linked to income / transfer rows counts only the spend — the
    ``transaction_type == "spend"`` type filter applies to the grouped query too."""
    spend = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 5, 10),
        amount_paise=-10000,
        fingerprint="fp-spend",
    )
    income = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 5, 11),
        amount_paise=50000,
        fingerprint="fp-income",
        transaction_type="income",
    )
    transfer = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 5, 12),
        amount_paise=-30000,
        fingerprint="fp-transfer",
        transaction_type="transfer",
    )
    session.add_all([spend, income, transfer])
    session.commit()
    travel = _tag_txns(
        session, user_id=axis_account.user_id, name="travel", txns=[spend, income, transfer]
    )

    body = client.get(f"{_SBT_URL}?month=2026-05").json()
    assert body["rows"] == [
        {"label_id": travel.id, "label_name": "travel", "total_paise": -10000},
    ]
    assert body["total_spend_paise"] == -10000


def test_spend_by_tag_excludes_pending_rows(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """Pending rows (``confirmed_at IS NULL``) are off the board — excluded from
    both the grouped rows and the coverage scalars (``confirmed_only``)."""
    confirmed = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 5, 10),
        amount_paise=-10000,
        fingerprint="fp-confirmed",
    )
    pending = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 5, 11),
        amount_paise=-5000,
        fingerprint="fp-pending",
        confirmed_at=None,
    )
    session.add_all([confirmed, pending])
    session.commit()
    travel = _tag_txns(
        session, user_id=axis_account.user_id, name="travel", txns=[confirmed, pending]
    )

    body = client.get(f"{_SBT_URL}?month=2026-05").json()
    assert body["rows"] == [
        {"label_id": travel.id, "label_name": "travel", "total_paise": -10000},
    ]
    assert body["total_spend_paise"] == -10000


def test_spend_by_tag_cross_user_isolation(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """Another user's tagged spend never appears in v1's breakdown or totals.

    The foreign-label-name-leak that spend-by-category guards is *structurally*
    unrepresentable here: ``transaction_labels``'s composite same-user FKs
    (ADR-0002) forbid a row linking v1's txn to another user's label, so only the
    other-user-rows prong applies. The ``Label.user_id == user_id`` predicate on
    the grouped JOIN is the belt-and-suspenders backstop regardless.
    """
    other_user = User(id=uuid.UUID("00000000-0000-0000-0000-0000000000b2"))
    session.add(other_user)
    session.commit()
    other_account = Account(
        user_id=other_user.id,
        name="Other CC",
        type="credit_card",
        issuer="axis",
        last4="9998",
    )
    session.add(other_account)
    session.commit()
    session.refresh(other_account)

    mine = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 5, 10),
        amount_paise=-12000,
        fingerprint="fp-mine",
    )
    theirs = _make_txn(
        user_id=other_user.id,
        account_id=other_account.id,
        txn_date=date(2026, 5, 10),
        amount_paise=-99999,
        fingerprint="fp-theirs",
    )
    session.add_all([mine, theirs])
    session.commit()
    my_tag = _tag_txns(session, user_id=axis_account.user_id, name="mine", txns=[mine])
    # Another user's label with the SAME name, on their own txn.
    _tag_txns(session, user_id=other_user.id, name="mine", txns=[theirs])

    body = client.get(f"{_SBT_URL}?month=2026-05").json()
    assert body["rows"] == [
        {"label_id": my_tag.id, "label_name": "mine", "total_paise": -12000},
    ]
    assert body["total_spend_paise"] == -12000
    assert -99999 not in [r["total_paise"] for r in body["rows"]]


@pytest.mark.parametrize("month", ["2026-13", "2026-1", "abc", ""])
def test_spend_by_tag_invalid_month_returns_422_without_echo(
    client: TestClient,
    seeded_user: User,
    month: str,
) -> None:
    """Invalid ``?month=`` is a 422 whose detail never echoes the rejected value
    (input-echo discipline, shared with spend-by-category)."""
    resp = client.get(f"{_SBT_URL}?month={month}")
    assert resp.status_code == 422
    assert month not in resp.text or month == ""


# =============================================================================
# GET /dashboards/spend-by-tag-by-period — tag trend over time (arc Phase C)
#
# The tag×time generalization of spend-by-tag: the group-by-tag INNER JOIN shape
# (arc decision #7, which INTENTIONALLY double-counts a multi-tagged txn across
# its tags) + the clipped-window Python `_bucket_of`/`_iter_periods` bucketing
# from spend-by-period. Two things differ from category-trend, both
# from tags being many:many:
#   1. The untagged residual is EXCLUDED (INNER JOIN drops zero-label txns) — no
#      null-id line, unlike the category route's LEFT JOIN uncategorized bucket.
#   2. NO cross-tag reconciliation identity: cells double-count + untagged is
#      dropped, so Σ(bucket cells) ≠ that bucket's spend-by-period total (the
#      category route HAS that identity). The only valid reconciliation is
#      per-tag — a tag's cells over the window sum to its spend-by-tag grouped
#      total for the same window. Cells are signed and never clamped.
# =============================================================================

_SBTBP_URL = "/api/v1/dashboards/spend-by-tag-by-period"


def test_spend_by_tag_by_period_empty_window_zero_filled_no_tags(
    client: TestClient,
    seeded_user: User,
) -> None:
    """No tagged activity → tags=[] and each period is a dense (empty) zero-fill;
    the envelope echoes bucket/start/end."""
    body = client.get(f"{_SBTBP_URL}?bucket=month&start=2026-04-01&end=2026-06-30").json()
    assert body == {
        "bucket": "month",
        "start": "2026-04-01",
        "end": "2026-06-30",
        "tags": [],
        # No tags → each bucket's `totals` is empty (dense over an empty tag set).
        "buckets": [
            {"period": "2026-04", "totals": []},
            {"period": "2026-05", "totals": []},
            {"period": "2026-06", "totals": []},
        ],
    }


def test_spend_by_tag_by_period_per_tag_line_reconciles_to_grouped_sum(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """THE valid reconciliation (arc Phase C): a tag's cells across the window sum
    to that tag's spend-by-tag grouped total for the same window — cross-checked
    against the single-month spend-by-tag endpoint."""
    may = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 5, 10),
        amount_paise=-15000,
        fingerprint="fp-may",
    )
    jun = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 6, 4),
        amount_paise=-25000,
        fingerprint="fp-jun",
    )
    session.add_all([may, jun])
    session.commit()
    travel = _tag_txns(session, user_id=axis_account.user_id, name="travel", txns=[may, jun])

    body = client.get(f"{_SBTBP_URL}?bucket=month&start=2026-05-01&end=2026-06-30").json()
    assert body["tags"] == [{"label_id": travel.id, "label_name": "travel"}]
    assert body["buckets"] == [
        {"period": "2026-05", "totals": [{"label_id": travel.id, "total_paise": -15000}]},
        {"period": "2026-06", "totals": [{"label_id": travel.id, "total_paise": -25000}]},
    ]
    # The tag's cells over the window sum to -40000...
    line_sum = sum(
        cell["total_paise"]
        for b in body["buckets"]
        for cell in b["totals"]
        if cell["label_id"] == travel.id
    )
    assert line_sum == -40000
    # ...and that equals Σ over the per-month spend-by-tag grouped totals.
    may_row = next(
        r
        for r in client.get(f"{_SBT_URL}?month=2026-05").json()["rows"]
        if r["label_id"] == travel.id
    )
    jun_row = next(
        r
        for r in client.get(f"{_SBT_URL}?month=2026-06").json()["rows"]
        if r["label_id"] == travel.id
    )
    assert may_row["total_paise"] + jun_row["total_paise"] == line_sum


def test_spend_by_tag_by_period_multi_tag_double_counts_and_no_total_identity(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """A txn carrying two tags appears in BOTH lines at full amount (the intended
    group-by double-count, arc decision #7). The NEGATIVE identity: Σ of a
    bucket's cells does NOT equal that bucket's spend-by-period total — no
    cross-tag total identity exists here (unlike category-trend)."""
    txn = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 5, 10),
        amount_paise=-15000,
        fingerprint="fp-multi",
    )
    session.add(txn)
    session.commit()
    travel = _tag_txns(session, user_id=axis_account.user_id, name="travel", txns=[txn])
    work = _tag_txns(session, user_id=axis_account.user_id, name="work", txns=[txn])

    url = "?bucket=month&start=2026-05-01&end=2026-05-31"
    body = client.get(f"{_SBTBP_URL}{url}").json()
    # Both tags carry the full amount in the single May bucket.
    may_cells = body["buckets"][0]["totals"]
    assert {c["label_id"]: c["total_paise"] for c in may_cells} == {
        travel.id: -15000,
        work.id: -15000,
    }
    # Σ(cells) overshoots — correct and never clamped.
    assert sum(c["total_paise"] for c in may_cells) == -30000
    # NEGATIVE identity: the true single-txn month total is -15000, so Σ(cells)
    # deliberately does NOT reconcile to the spend-by-period bucket total.
    period_total = client.get(f"{_PERIOD_URL}{url}").json()["buckets"][0]["total_paise"]
    assert period_total == -15000
    assert sum(c["total_paise"] for c in may_cells) != period_total


def test_spend_by_tag_by_period_untagged_excluded(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """An untagged txn contributes to NO line — there is no null-id "untagged"
    row (INNER JOIN drops zero-label txns), unlike the category route's
    uncategorized bucket."""
    tagged = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 5, 10),
        amount_paise=-15000,
        fingerprint="fp-tagged",
    )
    untagged = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 5, 11),
        amount_paise=-99999,
        fingerprint="fp-untagged",
    )
    session.add_all([tagged, untagged])
    session.commit()
    travel = _tag_txns(session, user_id=axis_account.user_id, name="travel", txns=[tagged])

    body = client.get(f"{_SBTBP_URL}?bucket=month&start=2026-05-01&end=2026-05-31").json()
    assert body["tags"] == [{"label_id": travel.id, "label_name": "travel"}]
    # No null-id line; the untagged -99999 never appears.
    assert None not in [t["label_id"] for t in body["tags"]]
    assert body["buckets"] == [
        {"period": "2026-05", "totals": [{"label_id": travel.id, "total_paise": -15000}]},
    ]


def test_spend_by_tag_by_period_zero_fill_dense_grid(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """Dense grid: two tags, each active in a different month over a 3-month
    window → every (period, tag) cell present, zero-filled where inactive, and a
    fully-empty middle month still lists a 0 cell per tag."""
    may = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 4, 5),
        amount_paise=-10000,
        fingerprint="fp-apr",
    )
    jul = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 6, 5),
        amount_paise=-20000,
        fingerprint="fp-jun",
    )
    session.add_all([may, jul])
    session.commit()
    a = _tag_txns(session, user_id=axis_account.user_id, name="alpha", txns=[may])
    b = _tag_txns(session, user_id=axis_account.user_id, name="beta", txns=[jul])

    body = client.get(f"{_SBTBP_URL}?bucket=month&start=2026-04-01&end=2026-06-30").json()
    # beta (-20000) is the bigger grand spender → first; alpha second.
    assert body["tags"] == [
        {"label_id": b.id, "label_name": "beta"},
        {"label_id": a.id, "label_name": "alpha"},
    ]
    # Every bucket carries a cell per tag in `tags` order, zero-filled.
    assert body["buckets"] == [
        {
            "period": "2026-04",
            "totals": [
                {"label_id": b.id, "total_paise": 0},
                {"label_id": a.id, "total_paise": -10000},
            ],
        },
        {
            "period": "2026-05",  # empty middle month — still dense, all zero
            "totals": [
                {"label_id": b.id, "total_paise": 0},
                {"label_id": a.id, "total_paise": 0},
            ],
        },
        {
            "period": "2026-06",
            "totals": [
                {"label_id": b.id, "total_paise": -20000},
                {"label_id": a.id, "total_paise": 0},
            ],
        },
    ]


def test_spend_by_tag_by_period_net_credit_cell_surfaces_positive(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """A tag whose refunds outweigh spend in a bucket surfaces a POSITIVE cell,
    never clamped server-side (the frontend line dips below y=0)."""
    spend = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 5, 5),
        amount_paise=-5000,
        fingerprint="fp-spend",
    )
    refund = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 5, 20),
        amount_paise=8000,
        fingerprint="fp-refund",
        transaction_type="spend",  # refund: spend row, positive amount (ADR-0009)
    )
    session.add_all([spend, refund])
    session.commit()
    travel = _tag_txns(session, user_id=axis_account.user_id, name="travel", txns=[spend, refund])

    body = client.get(f"{_SBTBP_URL}?bucket=month&start=2026-05-01&end=2026-05-31").json()
    assert body["buckets"] == [
        {"period": "2026-05", "totals": [{"label_id": travel.id, "total_paise": 3000}]},
    ]


def test_spend_by_tag_by_period_week_grain_zero_fill_boundary(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """Week grain (parity with spend-by-period's week test): three consecutive ISO
    weeks, tagged spend in W01 and W03, none in W02 → W02 zero-filled. Asserts the
    ISO-year boundary label too (2025-12-29 is 2026-W01)."""
    w01 = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2025, 12, 29),  # Monday, ISO 2026-W01
        amount_paise=-10000,
        fingerprint="fp-w01",
    )
    w03 = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 1, 12),  # Monday, ISO 2026-W03
        amount_paise=-30000,
        fingerprint="fp-w03",
    )
    session.add_all([w01, w03])
    session.commit()
    travel = _tag_txns(session, user_id=axis_account.user_id, name="travel", txns=[w01, w03])

    body = client.get(f"{_SBTBP_URL}?bucket=week&start=2025-12-29&end=2026-01-18").json()
    assert body["tags"] == [{"label_id": travel.id, "label_name": "travel"}]
    assert body["buckets"] == [
        {"period": "2026-W01", "totals": [{"label_id": travel.id, "total_paise": -10000}]},
        {"period": "2026-W02", "totals": [{"label_id": travel.id, "total_paise": 0}]},
        {"period": "2026-W03", "totals": [{"label_id": travel.id, "total_paise": -30000}]},
    ]


def test_spend_by_tag_by_period_start_after_end_returns_422(
    client: TestClient,
    seeded_user: User,
) -> None:
    """start > end is a 422 whose detail never echoes the rejected values
    (input-echo discipline, shared with the sibling period routes)."""
    resp = client.get(f"{_SBTBP_URL}?bucket=month&start=2026-06-30&end=2026-05-01")
    assert resp.status_code == 422
    assert "2026-06-30" not in resp.text and "2026-05-01" not in resp.text


def test_spend_by_tag_by_period_user_isolation(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """Another user's identically-named tag on their own txn never leaks — the
    INNER JOIN's Label.user_id guard + the Transaction.user_id filter scope both
    the tag set and the cells to the current user."""
    other_user = User(id=uuid.UUID("00000000-0000-0000-0000-0000000000c3"))
    session.add(other_user)
    session.flush()
    other_account = Account(
        user_id=other_user.id,
        name="Other CC",
        type="credit_card",
        issuer="axis",
        last4="9997",
    )
    session.add(other_account)
    session.commit()
    session.refresh(other_account)

    mine = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 5, 10),
        amount_paise=-12000,
        fingerprint="fp-mine",
    )
    theirs = _make_txn(
        user_id=other_user.id,
        account_id=other_account.id,
        txn_date=date(2026, 5, 10),
        amount_paise=-99999,
        fingerprint="fp-theirs",
    )
    session.add_all([mine, theirs])
    session.commit()
    my_tag = _tag_txns(session, user_id=axis_account.user_id, name="mine", txns=[mine])
    _tag_txns(session, user_id=other_user.id, name="mine", txns=[theirs])

    body = client.get(f"{_SBTBP_URL}?bucket=month&start=2026-05-01&end=2026-05-31").json()
    assert body["tags"] == [{"label_id": my_tag.id, "label_name": "mine"}]
    assert body["buckets"] == [
        {"period": "2026-05", "totals": [{"label_id": my_tag.id, "total_paise": -12000}]},
    ]
    assert -99999 not in [c["total_paise"] for b in body["buckets"] for c in b["totals"]]


def test_spend_by_tag_by_period_excludes_income_and_transfer(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """The ``transaction_type == "spend"`` type filter applies to the trend query too.

    Its single-month twin pins this; the by-period version did not, and the filter
    it depends on sits in the same prologue a later consolidation rewrites. Note
    the tag is attached to all three rows, so a lost filter shows up as a moved
    number rather than a missing tag.
    """
    spend = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 5, 10),
        amount_paise=-10000,
        fingerprint="fp-sbtbp-spend",
    )
    income = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 5, 11),
        amount_paise=50000,
        transaction_type="income",
        fingerprint="fp-sbtbp-income",
    )
    transfer = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 5, 12),
        amount_paise=-30000,
        transaction_type="transfer",
        fingerprint="fp-sbtbp-transfer",
    )
    session.add_all([spend, income, transfer])
    session.commit()
    travel = _tag_txns(
        session, user_id=axis_account.user_id, name="travel", txns=[spend, income, transfer]
    )

    body = client.get(f"{_SBTBP_URL}?bucket=month&start=2026-05-01&end=2026-05-31").json()
    assert body["buckets"] == [
        {"period": "2026-05", "totals": [{"label_id": travel.id, "total_paise": -10000}]},
    ]


def test_spend_by_tag_by_period_excludes_pending_rows(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """Pending rows (``confirmed_at IS NULL``) are off the board here too.

    ``_make_txn`` defaults ``confirmed_at`` to a real timestamp, so every other
    test in this block is blind to ``confirmed_only`` being dropped — this is the
    only row of data in the section that can see it.
    """
    confirmed = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 5, 10),
        amount_paise=-10000,
        fingerprint="fp-sbtbp-confirmed",
    )
    pending = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 5, 11),
        amount_paise=-77000,
        confirmed_at=None,
        fingerprint="fp-sbtbp-pending",
    )
    session.add_all([confirmed, pending])
    session.commit()
    travel = _tag_txns(
        session, user_id=axis_account.user_id, name="travel", txns=[confirmed, pending]
    )

    body = client.get(f"{_SBTBP_URL}?bucket=month&start=2026-05-01&end=2026-05-31").json()
    assert body["buckets"] == [
        {"period": "2026-05", "totals": [{"label_id": travel.id, "total_paise": -10000}]},
    ]


def test_retyping_a_miscategorised_income_to_spend_reduces_the_months_expense(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """ADR-0007 §Verification 2, amended by ADR-0009 — the correctness claim
    the whole ADR rests on, now expressed in the post-collapse vocabulary.

    Every F8 aggregate filters on ``transaction_type == "spend"`` and routes
    ``income`` to a separate bucket — the SIGN, not the type, is what makes a
    positive spend row a refund, but the *type* is still what decides which
    bucket a row's amount lands in. So a merchant refund the parser could only
    classify as ``income`` — ``_REFUND_RE`` can never separate a refund from a
    cashback credit — never enters the expense sum, and displayed spend is
    inflated by the credit's full magnitude (§F4a note 3).

    Re-typing it to ``spend`` is the fix (its positive amount is what makes it
    a refund), and this asserts the money actually moves: the credit leaves
    the income bucket and nets against the month's spend.
    """
    session.add(
        _make_txn(
            user_id=axis_account.user_id,
            account_id=axis_account.id,
            txn_date=date(2026, 5, 10),
            amount_paise=-100000,
            fingerprint="fp-refund-spend",
        )
    )
    credit = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 5, 12),
        amount_paise=40000,
        fingerprint="fp-refund-credit",
        transaction_type="income",
    )
    session.add(credit)
    session.commit()
    session.refresh(credit)

    before = client.get(f"{_OVERVIEW_URL}?month=2026-05").json()
    assert before["expense_paise"] == -100000  # the credit is invisible to spend
    assert before["income_paise"] == 40000

    resp = client.patch(f"/api/v1/transactions/{credit.id}", json={"transaction_type": "spend"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["amount_paise"] == 40000  # unchanged — the sign was already a refund's

    after = client.get(f"{_OVERVIEW_URL}?month=2026-05").json()
    # Spend drops by exactly the refund magnitude; the income bucket empties.
    assert after["expense_paise"] == -60000
    assert after["income_paise"] == 0
    assert after["net_paise"] == -60000


def test_available_years(
    client: TestClient,
    session: Session,
    axis_account: Account,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Years are calendar years (PRD §F8): a 15 March 2024 row offers 2024, through
    the current year, with no gap."""
    monkeypatch.setattr(clock, "utcnow", lambda: datetime(2026, 11, 15, 6, 0, tzinfo=UTC))
    session.add(
        _make_txn(
            user_id=axis_account.user_id,
            account_id=axis_account.id,
            txn_date=date(2024, 3, 15),
            amount_paise=-5000,
            fingerprint="fp-2024",
        )
    )
    session.commit()
    resp = client.get("/api/v1/dashboards/available-years")
    assert resp.status_code == 200
    assert resp.json()["years"] == [2026, 2025, 2024]


def test_year_parameter_dashboards(
    client: TestClient,
    session: Session,
    axis_account: Account,
) -> None:
    session.add(
        _make_txn(
            user_id=axis_account.user_id,
            account_id=axis_account.id,
            txn_date=date(2025, 6, 10),
            amount_paise=-12000,
            fingerprint="fp-2025-txn",
        )
    )
    session.commit()

    r1 = client.get("/api/v1/dashboards/spend-by-category?year=2025")
    assert r1.status_code == 200
    data1 = r1.json()
    assert data1["period"] == "2025"
    assert len(data1["rows"]) >= 1

    r2 = client.get("/api/v1/dashboards/spend-by-tag?year=2025")
    assert r2.status_code == 200
    data2 = r2.json()
    assert data2["period"] == "2025"

    r3 = client.get("/api/v1/dashboards/top-merchants?year=2025")
    assert r3.status_code == 200
    data3 = r3.json()
    assert data3["period"] == "2025"


def test_available_years_reaches_a_future_dated_row(
    client: TestClient,
    session: Session,
    axis_account: Account,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A future-dated row (a backfilled import, or a clock-skewed manual entry)
    must still be reachable — the fix for the old `max(current_year,
    current_year)` tautology, which was a no-op and could silently return `[]`
    when every row was outside the current year. Years are calendar years."""
    monkeypatch.setattr(clock, "utcnow", lambda: datetime(2026, 11, 15, 6, 0, tzinfo=UTC))
    session.add(
        _make_txn(
            user_id=axis_account.user_id,
            account_id=axis_account.id,
            txn_date=date(2030, 6, 15),
            amount_paise=-5000,
            fingerprint="fp-future",
        )
    )
    session.commit()
    resp = client.get("/api/v1/dashboards/available-years")
    assert resp.status_code == 200
    years = resp.json()["years"]
    assert 2030 in years
    assert 2026 in years  # the current year is always offered, even mid-range
    assert years == sorted(years, reverse=True)


def test_year_query_param_out_of_range_rejected(client: TestClient) -> None:
    """``year=0000`` is shape-valid against ``^\\d{4}$`` but out of any
    plausible range — ``date(0, 1, 1)`` raises ``ValueError``, which must
    surface as a controlled 422, not an uncaught 500."""
    resp = client.get("/api/v1/dashboards/spend-by-category?year=0000")
    assert resp.status_code == 422
    assert "0000" not in resp.text  # input-echo discipline


def test_overview_on_a_year_period_covers_the_whole_year(
    client: TestClient,
    session: Session,
    axis_account: Account,
) -> None:
    """``overview`` accepts a ``year`` period, not just ``month`` — the fix for
    the frontend's old ``YYYY-12`` fake-a-year workaround, under which
    ``income_paise`` was December-only. A February income row must count."""
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2025, 2, 10),
                amount_paise=100000,
                fingerprint="fp-feb-income",
                transaction_type="income",
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2025, 11, 5),
                amount_paise=-30000,
                fingerprint="fp-nov-spend",
            ),
        ]
    )
    session.commit()

    resp = client.get(f"{_OVERVIEW_URL}?year=2025")
    assert resp.status_code == 200
    body = resp.json()
    assert body["period"] == "2025"
    assert body["income_paise"] == 100000
    assert body["expense_paise"] == -30000


def test_overview_cashback_ytd_is_spend_side(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """ADR-0013 rule 4: the card YTD block splits positive spend by category. ``cashback``
    is positive spend in the spend "Cashback" or a child of it (one hop, ADR-0012 rule 6);
    ``refund`` is every other positive spend, uncategorized included; income never counts.
    ``spend_ytd = gross + refund + cashback``."""
    uid = axis_account.user_id
    food = Category(color="#2a78d6", user_id=uid, name="Food", kind="spend")
    cashback = Category(color="#2a78d6", user_id=uid, name=CASHBACK_CATEGORY_NAME, kind="spend")
    salary = Category(color="#2a78d6", user_id=uid, name="Salary", kind="income")
    session.add_all([food, cashback, salary])
    session.flush()
    rewards = Category(user_id=uid, name="Card Rewards", kind="spend", parent_id=cashback.id)
    session.add(rewards)
    session.flush()

    rows = (
        (-20000, "spend", None),
        (3000, "spend", food.id),
        (500, "spend", cashback.id),
        (100, "spend", rewards.id),
        (1000, "income", salary.id),
        (200, "spend", None),
    )
    session.add_all(
        [
            _make_txn(
                user_id=uid,
                account_id=axis_account.id,
                txn_date=date(2026, 4, 10 + i),
                amount_paise=amount,
                fingerprint=f"fp-cb-ytd-{i}",
                transaction_type=txn_type,
                category_id=category_id,
            )
            for i, (amount, txn_type, category_id) in enumerate(rows)
        ]
    )
    session.commit()

    row = client.get(f"{_OVERVIEW_URL}?month=2026-05").json()["accounts"][0]
    assert row["gross_spend_ytd_paise"] == -20000
    assert row["refund_ytd_paise"] == 3200
    assert row["cashback_ytd_paise"] == 600
    assert row["spend_ytd_paise"] == -16200


# =============================================================================
# Category trend (PRD §F8 view 6) — GET /dashboards/category-trend?months=12
#
# A trailing window of whole calendar months ending in the IST business month,
# signed spend per parent category with a subcategory drilldown. Archived
# children roll into their parent (ADR-0012 rule 6); Σ parents for a month is
# that month's spend-by-period total.
# =============================================================================

_CATEGORY_TREND_URL = "/api/v1/dashboards/category-trend"
_TREND_NOW = datetime(2026, 5, 10, 6, 0, tzinfo=UTC)


class _TrendWorld:
    """The invented category family used by the category-trend tests."""

    def __init__(self, household: Category, supplies: Category, repairs: Category, trips: Category):
        self.household = household
        self.supplies = supplies
        self.repairs = repairs
        self.trips = trips


def _seed_trend_world(session: Session, account: Account) -> _TrendWorld:
    uid = account.user_id
    household = Category(user_id=uid, name="Household", color="#2a78d6")
    session.add(household)
    session.flush()
    supplies = Category(user_id=uid, name="Supplies", parent_id=household.id)
    repairs = Category(
        user_id=uid,
        name="Repairs",
        parent_id=household.id,
        archived_at=datetime(2026, 1, 1),
    )
    trips = Category(color="#d95926", user_id=uid, name="Trips")
    session.add_all([supplies, repairs, trips])
    session.flush()

    def txn(
        n: int,
        d: date,
        amount: int,
        cat: Category | None,
        *,
        transaction_type: str = "spend",
        confirmed: bool = True,
    ) -> Transaction:
        return _make_txn(
            user_id=uid,
            account_id=account.id,
            txn_date=d,
            amount_paise=amount,
            fingerprint=f"fp-ct-{n}",
            transaction_type=transaction_type,
            category_id=cat.id if cat else None,
            confirmed_at=_DEFAULT_CONFIRMED if confirmed else None,
        )

    session.add_all(
        [
            txn(1, date(2025, 6, 1), -10000, supplies),  # first day of the window
            txn(2, date(2026, 3, 5), -8000, supplies),
            txn(3, date(2026, 3, 20), 3000, supplies),  # refund
            txn(4, date(2026, 3, 10), -2000, household),  # direct row
            txn(5, date(2025, 9, 1), -7000, repairs),  # archived child
            txn(6, date(2026, 4, 1), -4000, trips),
            txn(7, date(2026, 5, 31), -1500, None),  # last day of the window
            # Noise that must be excluded.
            txn(8, date(2025, 5, 31), -99900, supplies),
            txn(9, date(2026, 6, 1), -99900, supplies),
            txn(10, date(2026, 3, 15), 50000, supplies, transaction_type="income"),
            txn(11, date(2026, 3, 16), -50000, supplies, transaction_type="transfer"),
            txn(12, date(2026, 3, 17), -50000, supplies, confirmed=False),
        ]
    )
    session.commit()
    return _TrendWorld(household, supplies, repairs, trips)


def test_category_trend_matches_monthly_by_category(
    client: TestClient,
    axis_account: Account,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each parent's monthly series equals the sum of its family's rows in the
    month's spend-by-category view, to the paisa (PRD §Verification 5)."""
    monkeypatch.setattr(clock, "utcnow", lambda: _TREND_NOW)
    world = _seed_trend_world(session, axis_account)

    resp = client.get(f"{_CATEGORY_TREND_URL}?months=12")
    assert resp.status_code == 200
    body = resp.json()
    periods = body["periods"]
    assert periods[0] == "2025-06"
    assert periods[-1] == "2026-05"
    assert len(periods) == 12
    assert body["label_id"] is None

    cats = session.query(Category).filter(Category.user_id == axis_account.user_id).all()
    family: dict[int | None, set[int | None]] = {None: {None}}
    for c in cats:
        if c.parent_id is None:
            family[c.id] = {c.id}
    for c in cats:
        if c.parent_id is not None:
            family[c.parent_id].add(c.id)

    parents = {p["parent_id"]: p for p in body["parents"]}
    for i, month in enumerate(periods):
        rows = client.get(f"/api/v1/dashboards/spend-by-category?month={month}").json()["rows"]
        by_cat = {r["category_id"]: r["total_paise"] for r in rows}
        month_total = 0
        for pid, p in parents.items():
            expected = sum(by_cat.get(cid, 0) for cid in family[pid])
            assert p["totals_paise"][i] == expected, (month, pid)
            month_total += p["totals_paise"][i]
        assert month_total == sum(by_cat.values())

    household = parents[world.household.id]
    assert household["totals_paise"][periods.index("2026-03")] == -7000  # refund lowers March
    assert household["totals_paise"][periods.index("2025-09")] == -7000  # archived child stays in
    assert parents[world.trips.id]["totals_paise"][periods.index("2026-04")] == -4000
    assert body["parents"][-1]["parent_id"] is None
    assert body["parents"][-1]["subcategories"] == []


def test_category_trend_drilldown_sums_to_parent_and_keeps_archived_children(
    client: TestClient,
    axis_account: Account,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(clock, "utcnow", lambda: _TREND_NOW)
    world = _seed_trend_world(session, axis_account)

    body = client.get(f"{_CATEGORY_TREND_URL}?months=12").json()
    household = next(p for p in body["parents"] if p["parent_id"] == world.household.id)
    subs = household["subcategories"]
    assert [s["category_id"] for s in subs] == [
        world.supplies.id,
        world.repairs.id,
        world.household.id,
    ]
    assert [s["archived"] for s in subs] == [False, True, False]
    assert [s["is_direct"] for s in subs] == [False, False, True]
    assert subs[2]["category_name"] == "Household"
    assert subs[2]["totals_paise"][body["periods"].index("2026-03")] == -2000
    for i in range(len(body["periods"])):
        assert sum(s["totals_paise"][i] for s in subs) == household["totals_paise"][i]


def test_category_trend_colours_its_series_on_the_server(
    client: TestClient,
    axis_account: Account,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inheriting children rotate the palette after the parent's slot by position (archived
    siblings keep theirs); the direct row and the parent keep the parent's own colour."""
    monkeypatch.setattr(clock, "utcnow", lambda: _TREND_NOW)
    world = _seed_trend_world(session, axis_account)

    body = client.get(f"{_CATEGORY_TREND_URL}?months=12").json()
    household = next(p for p in body["parents"] if p["parent_id"] == world.household.id)
    assert household["color"] == "#2a78d6"
    assert [s.get("color") for s in household["subcategories"]] == [
        "#008300",
        "#d55181",
        "#2a78d6",
    ]
    trips = next(p for p in body["parents"] if p["parent_id"] == world.trips.id)
    assert trips["color"] == "#d95926"
    uncategorised = next(p for p in body["parents"] if p["parent_id"] is None)
    assert uncategorised["color"] is None


def test_spend_by_category_rows_carry_their_display_colors(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    world = _seed_trend_world(session, axis_account)

    rows = client.get(f"{_SBC_URL}?year=2025").json()["rows"]
    by_id = {r["category_id"]: r for r in rows}
    for cat in (world.supplies, world.repairs):
        assert by_id[cat.id].get("display_color") == "#2a78d6"
        assert by_id[cat.id].get("parent_display_color") == "#2a78d6"

    by_id = {r["category_id"]: r for r in client.get(f"{_SBC_URL}?year=2026").json()["rows"]}
    household = by_id[world.household.id]
    assert household.get("display_color") == "#2a78d6"
    assert household["parent_display_color"] is None
    trips = by_id[world.trips.id]
    assert trips.get("display_color") == "#d95926"
    assert trips["parent_display_color"] is None
    assert by_id[None]["display_color"] is None
    assert by_id[None]["parent_display_color"] is None


@pytest.mark.parametrize(
    ("instant", "first", "last"),
    [
        (datetime(2026, 3, 31, 18, 0, tzinfo=UTC), "2025-04", "2026-03"),
        (datetime(2026, 3, 31, 19, 30, tzinfo=UTC), "2025-05", "2026-04"),
    ],
    ids=["2330-ist-31-mar", "0100-ist-1-apr"],
)
def test_category_trend_window_ends_in_the_business_month(
    client: TestClient,
    seeded_user: User,
    monkeypatch: pytest.MonkeyPatch,
    instant: datetime,
    first: str,
    last: str,
) -> None:
    monkeypatch.setattr(clock, "utcnow", lambda: instant)
    periods = client.get(f"{_CATEGORY_TREND_URL}?months=12").json()["periods"]
    assert (periods[0], periods[-1], len(periods)) == (first, last, 12)
    assert len(client.get(f"{_CATEGORY_TREND_URL}?months=3").json()["periods"]) == 3


@pytest.mark.parametrize("months", ["0", "61", "x"])
def test_category_trend_months_out_of_range_422(
    client: TestClient, seeded_user: User, months: str
) -> None:
    assert client.get(f"{_CATEGORY_TREND_URL}?months={months}").status_code == 422


def test_category_trend_excludes_other_users(
    client: TestClient,
    axis_account: Account,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(clock, "utcnow", lambda: _TREND_NOW)
    other = User(id=uuid.UUID("00000000-0000-0000-0000-000000000099"))
    session.add(other)
    session.commit()
    other_account = Account(
        user_id=other.id, name="Other CC", type="credit_card", issuer="axis", last4="9999"
    )
    other_cat = Category(color="#2a78d6", user_id=other.id, name="STOLEN-PROTECTED-NAME")
    session.add_all([other_account, other_cat])
    session.commit()
    session.add(
        _make_txn(
            user_id=other.id,
            account_id=other_account.id,
            txn_date=date(2026, 5, 2),
            amount_paise=-77700,
            fingerprint="fp-ct-other",
            category_id=other_cat.id,
        )
    )
    session.commit()

    resp = client.get(_CATEGORY_TREND_URL)
    assert resp.status_code == 200
    assert resp.json()["parents"] == []
    assert "STOLEN-PROTECTED-NAME" not in resp.text


def test_category_trend_label_filter_scopes_series(
    client: TestClient,
    axis_account: Account,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(clock, "utcnow", lambda: _TREND_NOW)
    world = _seed_trend_world(session, axis_account)
    tagged = session.query(Transaction).filter(Transaction.fingerprint == "fp-ct-6").one()
    label = _tag_txns(session, user_id=axis_account.user_id, name="trip", txns=[tagged])

    body = client.get(f"{_CATEGORY_TREND_URL}?label_id={label.id}").json()
    assert body["label_id"] == label.id
    assert [p["parent_id"] for p in body["parents"]] == [world.trips.id]
    assert sum(body["parents"][0]["totals_paise"]) == -4000


def test_fronted_card_usage_calculation(
    client: TestClient,
    session: Session,
    seeded_user: User,
    axis_account: Account,
) -> None:
    """ADR-0017 Rule 7: Monthly Fronted Card Usage and Net Personal Spend Isolation.

    - fronted_card_usage_paise: monthly sum of credit card debits linked to peer_entries
      (direction = 'you_gave').
    - net_personal_spend_paise = gross_spend_paise - fronted_peer_entries_paise.
    - transactions flagged as unpaired 'transfer' remain excluded from personal spend.
    - bank debit peer entries do not count towards card usage.
    - unconfirmed/pending rows are excluded.
    - tenant isolation: other users' rows do not leak.
    """
    uid = seeded_user.id
    bank_account = Account(user_id=uid, name="HDFC Bank", type="bank")
    session.add(bank_account)
    session.flush()

    contact = PeerContact(user_id=uid, name="Bob Friend")
    session.add(contact)
    session.flush()

    # 1. Confirmed Credit Card Spend: ₹5,000 debit (-500,000 paise)
    card_spend = _make_txn(
        user_id=uid,
        account_id=axis_account.id,
        txn_date=date(2026, 3, 5),
        amount_paise=-500000,
        fingerprint="fp-card-spend-1",
        transaction_type="spend",
    )
    # 2. Confirmed Bank Spend: ₹2,000 debit (-200,000 paise)
    bank_spend = _make_txn(
        user_id=uid,
        account_id=bank_account.id,
        txn_date=date(2026, 3, 8),
        amount_paise=-200000,
        fingerprint="fp-bank-spend-1",
        transaction_type="spend",
    )
    # 3. Confirmed Credit Card 100% fronted transfer: ₹3,000 debit (-300,000 paise)
    card_transfer = _make_txn(
        user_id=uid,
        account_id=axis_account.id,
        txn_date=date(2026, 3, 12),
        amount_paise=-300000,
        fingerprint="fp-card-transfer-1",
        transaction_type="transfer",
    )
    session.add_all([card_spend, bank_spend, card_transfer])
    session.flush()

    # 4. Peer entries:
    # - Split ₹2,000 of card spend for Bob
    pe_card_spend = PeerEntry(
        user_id=uid,
        contact_id=contact.id,
        transaction_id=card_spend.id,
        date=date(2026, 3, 5),
        amount_paise=200000,
        direction="you_gave",
    )
    # - Split ₹1,000 of bank spend for Bob
    pe_bank_spend = PeerEntry(
        user_id=uid,
        contact_id=contact.id,
        transaction_id=bank_spend.id,
        date=date(2026, 3, 8),
        amount_paise=100000,
        direction="you_gave",
    )
    # - 100% fronted ₹3,000 on card transfer for Bob
    pe_card_transfer = PeerEntry(
        user_id=uid,
        contact_id=contact.id,
        transaction_id=card_transfer.id,
        date=date(2026, 3, 12),
        amount_paise=300000,
        direction="you_gave",
    )
    session.add_all([pe_card_spend, pe_bank_spend, pe_card_transfer])

    # 5. Pending (unconfirmed) transaction with peer entry: must NOT count
    pending_txn = _make_txn(
        user_id=uid,
        account_id=axis_account.id,
        txn_date=date(2026, 3, 20),
        amount_paise=-100000,
        fingerprint="fp-pending-card",
        transaction_type="spend",
        confirmed_at=None,
    )
    session.add(pending_txn)
    session.flush()
    pe_pending = PeerEntry(
        user_id=uid,
        contact_id=contact.id,
        transaction_id=pending_txn.id,
        date=date(2026, 3, 20),
        amount_paise=100000,
        direction="you_gave",
    )
    session.add(pe_pending)

    # 6. Another user's card transaction + peer entry: must NOT leak into active user's stats
    other_user = User(id=uuid.UUID("00000000-0000-0000-0000-000000000088"))
    session.add(other_user)
    session.flush()
    other_card = Account(
        user_id=other_user.id, name="Other CC", type="credit_card", issuer="axis", last4="8888"
    )
    session.add(other_card)
    session.flush()
    other_contact = PeerContact(user_id=other_user.id, name="Other Contact")
    session.add(other_contact)
    session.flush()
    other_txn = _make_txn(
        user_id=other_user.id,
        account_id=other_card.id,
        txn_date=date(2026, 3, 15),
        amount_paise=-999000,
        fingerprint="fp-other-card",
        transaction_type="spend",
    )
    session.add(other_txn)
    session.flush()
    other_pe = PeerEntry(
        user_id=other_user.id,
        contact_id=other_contact.id,
        transaction_id=other_txn.id,
        date=date(2026, 3, 15),
        amount_paise=999000,
        direction="you_gave",
    )
    session.add(other_pe)

    session.commit()

    # Query March 2026 overview
    resp = client.get(f"{_OVERVIEW_URL}?month=2026-03")
    assert resp.status_code == 200
    data = resp.json()

    # fronted_card_usage_paise:
    # Card spend debit (200,000) + Card transfer debit (300,000) = 500,000 paise (₹5,000).
    # Bank spend debit (100,000) is on a bank account, so not in card usage.
    # Pending txn (100,000) is excluded because confirmed_at is None.
    # Other user (999,000) is excluded by user_id isolation.
    assert data["fronted_card_usage_paise"] == 500000

    # gross_spend_paise:
    # Card spend (500,000) + Bank spend (200,000) = 700,000 paise (₹7,000).
    # Card transfer (300,000) is excluded because transaction_type == 'transfer'.
    # Pending is excluded.
    assert data["gross_spend_paise"] == 700000

    # fronted_peer_entries_paise:
    # Fronted card spend (200,000) + Fronted bank spend (100,000) = 300,000 paise.
    # Fronted transfer (300,000) is excluded because transaction is transfer, not spend.
    assert data["fronted_peer_entries_paise"] == 300000

    # net_personal_spend_paise:
    # gross_spend_paise (700,000) - fronted_peer_entries_paise (300,000) = 400,000 paise (₹4,000).
    assert data["net_personal_spend_paise"] == 400000
