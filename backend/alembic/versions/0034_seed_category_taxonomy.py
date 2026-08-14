"""seed two-level category taxonomy

Revision ID: 0034_seed_category_taxonomy
Revises: 0033_add_category_parent_id
Create Date: 2026-08-14

Backfills the 2-level category taxonomy for every EXISTING user:
1. Creates parent categories for spend and income.
2. Reparents existing legacy categories under their respective parents.
3. Inserts new pure-English subcategories under their parents.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0034_seed_category_taxonomy"
down_revision: str | Sequence[str] | None = "0033_add_category_parent_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_users = sa.table(
    "users",
    sa.column("id", sa.Uuid()),
)

_categories = sa.table(
    "categories",
    sa.column("id", sa.Integer()),
    sa.column("user_id", sa.Uuid()),
    sa.column("name", sa.String()),
    sa.column("kind", sa.String()),
    sa.column("is_seeded", sa.Boolean()),
    sa.column("color", sa.String()),
    sa.column("parent_id", sa.Integer()),
    sa.column("archived_at", sa.DateTime()),
)

# downgrade()-only: the three FKs into categories.id besides categories.parent_id
# itself (app/models/transaction.py, app/models/merchant_tag_map.py).
_transactions = sa.table(
    "transactions",
    sa.column("id", sa.Integer()),
    sa.column("category_id", sa.Integer()),
    sa.column("auto_category_id", sa.Integer()),
)

_merchant_tag_map = sa.table(
    "merchant_tag_map",
    sa.column("id", sa.Integer()),
    sa.column("category_id", sa.Integer()),
)

_SPEND_PARENTS: tuple[tuple[str, str], ...] = (
    ("Food & Dining", "#d95926"),
    ("Household & Living", "#6c5cd6"),
    ("Bills & Utilities", "#0e97c4"),
    ("Commute & Transportation", "#2a78d6"),
    ("Shopping & Lifestyle", "#d55181"),
    ("Family & Social", "#b246c0"),
    ("Savings & Investments", "#008300"),
    ("Loans & Settlements", "#c23b6b"),
)

_INCOME_PARENTS: tuple[tuple[str, str], ...] = (("Income", "#008300"),)

_LEGACY_SPEND_REPARENT: dict[str, str] = {
    "Food": "Food & Dining",
    "Groceries": "Food & Dining",
    "Rent": "Household & Living",
    "Utilities": "Bills & Utilities",
    "Transport": "Commute & Transportation",
    "Travel": "Commute & Transportation",
    "Shopping": "Shopping & Lifestyle",
    "Entertainment": "Shopping & Lifestyle",
    "Health": "Shopping & Lifestyle",
    "Subscriptions": "Shopping & Lifestyle",
    "Investment": "Savings & Investments",
    "EMI": "Loans & Settlements",
}

_LEGACY_INCOME_REPARENT: dict[str, str] = {
    "Salary": "Income",
    "Freelancing": "Income",
    "Cashback": "Income",
    "Other": "Income",
}

# The legacy flat default hex each reparented child carried, frozen from migrations
# 0012 (seed) / 0018 (recolor) — the values a pre-hierarchy user's row would have gotten.
# Used ONLY by step 2 below to detect "this row still carries the seed's original colour"
# before nulling it — a migration must not import provisioning.py, hence the frozen copy.
_LEGACY_SPEND_DEFAULT_COLOR: dict[str, str] = {
    "Food": "#d95926",
    "Groceries": "#6f9e15",
    "Transport": "#2a78d6",
    "Rent": "#6c5cd6",
    "Utilities": "#0e97c4",
    "Shopping": "#d55181",
    "Entertainment": "#b246c0",
    "Health": "#e34948",
    "Travel": "#0e9488",
    "Subscriptions": "#1baf7a",
    "EMI": "#c23b6b",
    "Investment": "#008300",
}
_LEGACY_INCOME_DEFAULT_COLOR: dict[str, str] = {
    "Salary": "#008300",
    "Freelancing": "#2a78d6",
    "Cashback": "#c98500",
    "Other": "#94a3b8",
}

_NEW_SPEND_SUBCATEGORIES: dict[str, tuple[str, ...]] = {
    "Food & Dining": (
        "Online Food Delivery",
        "Restaurants & Cafes",
        "Quick Bites & Snacks",
        "Coffee & Tea",
    ),
    "Household & Living": (
        "Rent & Maintenance",
        "Instant Grocery Delivery",
        "Household Help & Domestic Staff",
        "Home Improvements & Repairs",
        "Furniture & Appliances",
    ),
    "Bills & Utilities": (
        "Mobile & Broadband",
        "Electricity",
        "Cooking Gas & LPG",
        "Cable & Satellite TV",
        "Water & Municipal Taxes",
    ),
    "Commute & Transportation": (
        "Fuel & Petrol",
        "Metro & Public Transit",
        "Ride-Hailing & Taxis",
        "Highway Tolls & Parking",
        "Vehicle Service & Repairs",
    ),
    "Shopping & Lifestyle": (
        "Clothing & Apparel",
        "Electronics & Gadgets",
        "Personal Care & Grooming",
        "Footwear & Accessories",
        "Digital Subscriptions & Streaming",
    ),
    "Family & Social": (
        "Gifts & Celebrations",
        "Family Support & Transfers",
        "Education & Tuition",
        "Charity & Donations",
    ),
    "Savings & Investments": (
        "Mutual Funds & SIPs",
        "Stocks & Securities",
        "Precious Metals & Gold",
        "Health & Life Insurance",
        "Fixed Deposits & Savings",
    ),
    "Loans & Settlements": (
        "Credit Card Payments",
        "Loan EMIs & Repayments",
        "Home & Personal Loans",
        "Shared Expenses & Splits",
    ),
}

_NEW_INCOME_SUBCATEGORIES: dict[str, tuple[str, ...]] = {
    "Income": (
        "Investment Returns",
        "Rental Income",
    ),
}


def upgrade() -> None:
    bind = op.get_bind()
    user_ids = [row.id for row in bind.execute(sa.select(_users.c.id)).all()]

    for user_id in user_ids:
        # 1. Insert parent categories if not already present
        existing_cats = bind.execute(
            sa.select(_categories.c.id, _categories.c.name, _categories.c.kind).where(
                _categories.c.user_id == user_id,
                _categories.c.archived_at.is_(None),
            )
        ).all()
        existing_spend = {row.name: row.id for row in existing_cats if row.kind == "spend"}
        existing_income = {row.name: row.id for row in existing_cats if row.kind == "income"}

        for name, color in _SPEND_PARENTS:
            if name not in existing_spend:
                bind.execute(
                    _categories.insert().values(
                        user_id=user_id,
                        name=name,
                        kind="spend",
                        is_seeded=True,
                        color=color,
                        parent_id=None,
                    )
                )

        for name, color in _INCOME_PARENTS:
            if name not in existing_income:
                bind.execute(
                    _categories.insert().values(
                        user_id=user_id,
                        name=name,
                        kind="income",
                        is_seeded=True,
                        color=color,
                        parent_id=None,
                    )
                )

        # Refresh map of parent names -> parent IDs, carrying color/is_seeded too — step 2
        # below needs both to decide whether a reparented child's colour should be nulled.
        refreshed_cats = bind.execute(
            sa.select(
                _categories.c.id,
                _categories.c.name,
                _categories.c.kind,
                _categories.c.color,
                _categories.c.is_seeded,
            ).where(
                _categories.c.user_id == user_id,
                _categories.c.archived_at.is_(None),
            )
        ).all()
        spend_map = {row.name: row.id for row in refreshed_cats if row.kind == "spend"}
        income_map = {row.name: row.id for row in refreshed_cats if row.kind == "income"}
        spend_by_name = {row.name: row for row in refreshed_cats if row.kind == "spend"}
        income_by_name = {row.name: row for row in refreshed_cats if row.kind == "income"}

        # 2. Reparent legacy categories.
        #
        # Decision #5 (ADR-0012 / PRD §F5): a seeded subcategory's colour is NULL so it
        # inherits the parent's hue, and every *seeded* subcategory provision_default_categories
        # creates is born that way. A legacy flat category becomes a subcategory right here, so
        # parity requires nulling its colour too — but ONLY when it still carries its ORIGINAL
        # seed colour (the frozen 0012/0018 hex above) AND is_seeded is True. A user who
        # re-coloured a legacy default (e.g. picked their own shade for "Groceries") keeps it —
        # this migration reparents, it never overwrites a user's choice. Do not "simplify" this
        # to an unconditional NULL; that would silently discard a user's customisation.
        for child_name, parent_name in _LEGACY_SPEND_REPARENT.items():
            child_id = spend_map.get(child_name)
            parent_id = spend_map.get(parent_name)
            if child_id is not None and parent_id is not None:
                child_row = spend_by_name[child_name]
                values: dict[str, object] = {"parent_id": parent_id}
                legacy_hex = _LEGACY_SPEND_DEFAULT_COLOR.get(child_name)
                still_default = legacy_hex is not None and child_row.color == legacy_hex
                if child_row.is_seeded and still_default:
                    values["color"] = None
                bind.execute(
                    _categories.update().where(_categories.c.id == child_id).values(**values)
                )

        for child_name, parent_name in _LEGACY_INCOME_REPARENT.items():
            child_id = income_map.get(child_name)
            parent_id = income_map.get(parent_name)
            if child_id is not None and parent_id is not None:
                child_row = income_by_name[child_name]
                values = {"parent_id": parent_id}
                legacy_hex = _LEGACY_INCOME_DEFAULT_COLOR.get(child_name)
                still_default = legacy_hex is not None and child_row.color == legacy_hex
                if child_row.is_seeded and still_default:
                    values["color"] = None
                bind.execute(
                    _categories.update().where(_categories.c.id == child_id).values(**values)
                )

        # 3. Insert new subcategories
        for parent_name, subcategories in _NEW_SPEND_SUBCATEGORIES.items():
            parent_id = spend_map.get(parent_name)
            if parent_id is None:
                continue
            for sub_name in subcategories:
                if sub_name not in spend_map:
                    bind.execute(
                        _categories.insert().values(
                            user_id=user_id,
                            name=sub_name,
                            kind="spend",
                            is_seeded=True,
                            color=None,
                            parent_id=parent_id,
                        )
                    )

        for parent_name, subcategories in _NEW_INCOME_SUBCATEGORIES.items():
            parent_id = income_map.get(parent_name)
            if parent_id is None:
                continue
            for sub_name in subcategories:
                if sub_name not in income_map:
                    bind.execute(
                        _categories.insert().values(
                            user_id=user_id,
                            name=sub_name,
                            kind="income",
                            is_seeded=True,
                            color=None,
                            parent_id=parent_id,
                        )
                    )


def downgrade() -> None:
    bind = op.get_bind()

    def _is_referenced(category_id: int) -> bool:
        """True if a transaction or a merchant rule still points at this category.

        Deleting a referenced row under FK enforcement would raise; the caller's
        job is to archive instead."""
        txn_hit = bind.execute(
            sa.select(sa.literal(1))
            .where(
                sa.or_(
                    _transactions.c.category_id == category_id,
                    _transactions.c.auto_category_id == category_id,
                )
            )
            .limit(1)
        ).first()
        if txn_hit is not None:
            return True
        tag_hit = bind.execute(
            sa.select(sa.literal(1)).where(_merchant_tag_map.c.category_id == category_id).limit(1)
        ).first()
        return tag_hit is not None

    def _has_children(category_id: int) -> bool:
        """True if any category row still points at this one as parent.

        Hard-deleting it anyway would let ``categories.parent_id``'s
        ``ondelete="CASCADE"`` (migration 0033) take that child down with it —
        seeded-and-kept-for-a-reference or genuinely user-authored alike."""
        return (
            bind.execute(
                sa.select(sa.literal(1)).where(_categories.c.parent_id == category_id).limit(1)
            ).first()
            is not None
        )

    # 1. Clear parent_id ONLY on what this migration itself set it on: the legacy
    #    categories it reparented, and the new subcategories it inserted already
    #    parented. `is_seeded` alone is NOT an exclusive marker for "this migration
    #    owns it" — a user can rename or reparent a seeded row and it stays
    #    is_seeded=True (the same lesson ADR-0011's seed dictionary learned the hard
    #    way: a migration DELETE keyed on a supposed seed marker destroyed user
    #    rules). Scope by is_seeded AND the frozen name set this migration owns, so
    #    a user-authored hierarchy — even one hanging off a seeded parent — is
    #    never touched here.
    spend_reparented_names: set[str] = set(_LEGACY_SPEND_REPARENT)
    for subcats in _NEW_SPEND_SUBCATEGORIES.values():
        spend_reparented_names.update(subcats)
    income_reparented_names: set[str] = set(_LEGACY_INCOME_REPARENT)
    for subcats in _NEW_INCOME_SUBCATEGORIES.values():
        income_reparented_names.update(subcats)

    bind.execute(
        _categories.update()
        .where(
            _categories.c.is_seeded.is_(True),
            _categories.c.kind == "spend",
            _categories.c.name.in_(spend_reparented_names),
        )
        .values(parent_id=None)
    )
    bind.execute(
        _categories.update()
        .where(
            _categories.c.is_seeded.is_(True),
            _categories.c.kind == "income",
            _categories.c.name.in_(income_reparented_names),
        )
        .values(parent_id=None)
    )

    # 2. Remove the parents + new subcategories this migration created — but never a
    #    row a transaction or a merchant rule still points at (would leave a dangling
    #    FK), and never a parent that still has a remaining child post-step-1 (would
    #    cascade-delete it via categories.parent_id's ondelete="CASCADE" — step 1
    #    already detached every child this migration owns, so a remaining child here
    #    can only be user-authored). Soft-archive instead: a downgrade that can't
    #    fully undo itself without destroying user data leaves an archived row
    #    behind, not a crater.
    now = datetime.now(UTC).replace(tzinfo=None)  # naive UTC — ADR-0001 rule 5

    spend_new_names: set[str] = {name for name, _ in _SPEND_PARENTS}
    for subcats in _NEW_SPEND_SUBCATEGORIES.values():
        spend_new_names.update(subcats)

    income_new_names: set[str] = {name for name, _ in _INCOME_PARENTS}
    for subcats in _NEW_INCOME_SUBCATEGORIES.values():
        income_new_names.update(subcats)

    for kind, names in (("spend", spend_new_names), ("income", income_new_names)):
        candidate_ids = [
            row.id
            for row in bind.execute(
                sa.select(_categories.c.id).where(
                    _categories.c.is_seeded.is_(True),
                    _categories.c.kind == kind,
                    _categories.c.name.in_(names),
                )
            ).all()
        ]
        for category_id in candidate_ids:
            if _is_referenced(category_id) or _has_children(category_id):
                bind.execute(
                    _categories.update()
                    .where(_categories.c.id == category_id, _categories.c.archived_at.is_(None))
                    .values(archived_at=now)
                )
            else:
                bind.execute(_categories.delete().where(_categories.c.id == category_id))
