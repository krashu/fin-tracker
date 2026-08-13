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

_INCOME_PARENTS: tuple[tuple[str, str], ...] = (
    ("Income", "#008300"),
)

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

        # Refresh map of parent names -> parent IDs
        refreshed_cats = bind.execute(
            sa.select(_categories.c.id, _categories.c.name, _categories.c.kind).where(
                _categories.c.user_id == user_id,
                _categories.c.archived_at.is_(None),
            )
        ).all()
        spend_map = {row.name: row.id for row in refreshed_cats if row.kind == "spend"}
        income_map = {row.name: row.id for row in refreshed_cats if row.kind == "income"}

        # 2. Reparent legacy categories
        for child_name, parent_name in _LEGACY_SPEND_REPARENT.items():
            child_id = spend_map.get(child_name)
            parent_id = spend_map.get(parent_name)
            if child_id is not None and parent_id is not None:
                bind.execute(
                    _categories.update()
                    .where(_categories.c.id == child_id)
                    .values(parent_id=parent_id)
                )

        for child_name, parent_name in _LEGACY_INCOME_REPARENT.items():
            child_id = income_map.get(child_name)
            parent_id = income_map.get(parent_name)
            if child_id is not None and parent_id is not None:
                bind.execute(
                    _categories.update()
                    .where(_categories.c.id == child_id)
                    .values(parent_id=parent_id)
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
    # 1. Clear parent_id
    bind.execute(_categories.update().values(parent_id=None))

    # 2. Delete parent categories and new subcategories created in this migration
    spend_new_names: set[str] = {name for name, _ in _SPEND_PARENTS}
    for subcats in _NEW_SPEND_SUBCATEGORIES.values():
        spend_new_names.update(subcats)

    income_new_names: set[str] = {name for name, _ in _INCOME_PARENTS}
    for subcats in _NEW_INCOME_SUBCATEGORIES.values():
        income_new_names.update(subcats)

    bind.execute(
        _categories.delete().where(
            _categories.c.is_seeded.is_(True),
            _categories.c.kind == "spend",
            _categories.c.name.in_(spend_new_names),
        )
    )
    bind.execute(
        _categories.delete().where(
            _categories.c.is_seeded.is_(True),
            _categories.c.kind == "income",
            _categories.c.name.in_(income_new_names),
        )
    )
