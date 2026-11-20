"""streamline category taxonomy to lean core set

Revision ID: 0049_streamline_category_taxonomy
Revises: 0048_peer_ledger
Create Date: 2026-11-20

Consolidates the 64-category bloat into a lean, intuitive starter taxonomy (PRD §F5, ADR-0012):
1. Archives colliding obsolete categories (subcategory "Shopping", root "Bills & Utilities").
2. Renames remaining roots ("Household & Living" -> "Housing & Utilities",
   "Commute & Transportation" -> "Transport & Commute", "Shopping & Lifestyle" -> "Shopping", etc.).
3. Creates new roots ("Healthcare", "Travel & Vacations") if absent.
4. Creates new subcategories (including "Bills & Utilities" under "Housing & Utilities").
5. Reassigns existing transactions from retired categories to streamlined equivalents.
6. Archives remaining obsolete seeded categories.
7. Synchronizes merchant aliases and merchant tag rules.

Downgrade reverses all renames, un-archives retired categories, and cleans up added categories.
Constants are frozen copies (migrations are snapshots). Timestamps are naive UTC (ADR-0001 rule 5).
Every update/delete filters user_id (ADR-0003 tenant isolation).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0049_streamline_category_taxonomy"
down_revision: str | Sequence[str] | None = "0048_peer_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_users = sa.table("users", sa.column("id", sa.Uuid()))

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

_transactions = sa.table(
    "transactions",
    sa.column("id", sa.Integer()),
    sa.column("user_id", sa.Uuid()),
    sa.column("category_id", sa.Integer()),
    sa.column("auto_category_id", sa.Integer()),
    sa.column("pre_link_category_id", sa.Integer()),
)

_merchant_tag_map = sa.table(
    "merchant_tag_map",
    sa.column("id", sa.Integer()),
    sa.column("user_id", sa.Uuid()),
    sa.column("merchant_normalized", sa.String()),
    sa.column("category_id", sa.Integer()),
    sa.column("hit_count", sa.Integer()),
    sa.column("pinned", sa.Boolean()),
)

_merchant_alias = sa.table(
    "merchant_alias",
    sa.column("id", sa.Integer()),
    sa.column("user_id", sa.Uuid()),
    sa.column("pattern", sa.String()),
    sa.column("canonical", sa.String()),
    sa.column("is_seeded", sa.Boolean()),
)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# Roots to rename: old_name -> (new_name, new_color, old_color)
_ROOT_RENAMES: dict[str, tuple[str, str, str]] = {
    "Household & Living": ("Housing & Utilities", "#6c5cd6", "#6c5cd6"),
    "Commute & Transportation": ("Transport & Commute", "#2a78d6", "#2a78d6"),
    "Shopping & Lifestyle": ("Shopping", "#d55181", "#d55181"),
    "Family & Social": ("Entertainment & Subscriptions", "#b246c0", "#b246c0"),
    "Savings & Investments": ("Investments & Savings", "#008300", "#008300"),
    "Loans & Settlements": ("Loans & EMIs", "#c23b6b", "#c23b6b"),
}

# New roots to create if absent: (name, color)
_NEW_ROOTS: tuple[tuple[str, str], ...] = (
    ("Healthcare", "#e34948"),
    ("Travel & Vacations", "#0e97c4"),
)

# New subcategories to ensure exist under parent: (parent_name, subcategory_name)
_NEW_SUBCATEGORIES: tuple[tuple[str, str], ...] = (
    ("Food & Dining", "Dining Out & Delivery"),
    ("Transport & Commute", "Daily Commute"),
    ("Transport & Commute", "Vehicle Service & Fuel"),
    ("Shopping", "Clothing & Personal"),
    ("Shopping", "Electronics & Home"),
    ("Healthcare", "Pharmacy & Medicines"),
    ("Healthcare", "Doctor & Diagnostics"),
    ("Entertainment & Subscriptions", "Streaming & Subscriptions"),
    ("Entertainment & Subscriptions", "Events & Outings"),
    ("Loans & EMIs", "Loan EMIs"),
    ("Travel & Vacations", "Flights & Trains"),
    ("Travel & Vacations", "Hotels & Stays"),
    ("Housing & Utilities", "Bills & Utilities"),
)

# Mapping of obsolete category names to their target replacement category names
_REASSIGN_MAP: dict[str, str] = {
    # Food
    "Food": "Dining Out & Delivery",
    "Quick Bites & Snacks": "Dining Out & Delivery",
    "Restaurants & Cafes": "Dining Out & Delivery",
    "Coffee & Tea": "Dining Out & Delivery",
    "Online Food Delivery": "Dining Out & Delivery",
    "Instant Grocery Delivery": "Groceries",
    # Housing & Utilities (Utilities mapped to new subcategory "Bills & Utilities")
    "Rent": "Rent & Maintenance",
    "Bills & Utilities": "Bills & Utilities",
    "Utilities": "Bills & Utilities",
    "Electricity": "Bills & Utilities",
    "Cooking Gas & LPG": "Bills & Utilities",
    "Water & Municipal Taxes": "Bills & Utilities",
    "Mobile & Broadband": "Bills & Utilities",
    "Cable & Satellite TV": "Bills & Utilities",
    "Household Help & Domestic Staff": "Rent & Maintenance",
    "Home Improvements & Repairs": "Electronics & Home",
    "Furniture & Appliances": "Electronics & Home",
    # Transport
    "Transport": "Daily Commute",
    "Travel": "Flights & Trains",
    "Fuel & Petrol": "Vehicle Service & Fuel",
    "Metro & Public Transit": "Daily Commute",
    "Ride-Hailing & Taxis": "Daily Commute",
    "Highway Tolls & Parking": "Daily Commute",
    "Vehicle Service & Repairs": "Vehicle Service & Fuel",
    # Shopping
    "Shopping": "Clothing & Personal",
    "Clothing & Apparel": "Clothing & Personal",
    "Footwear & Accessories": "Clothing & Personal",
    "Personal Care & Grooming": "Clothing & Personal",
    "Electronics & Gadgets": "Electronics & Home",
    # Health
    "Health": "Pharmacy & Medicines",
    "Health & Life Insurance": "Doctor & Diagnostics",
    # Subscriptions & Entertainment
    "Subscriptions": "Streaming & Subscriptions",
    "Digital Subscriptions & Streaming": "Streaming & Subscriptions",
    "Entertainment": "Events & Outings",
    "Gifts & Celebrations": "Events & Outings",
    "Family Support & Transfers": "Events & Outings",
    "Education & Tuition": "Events & Outings",
    "Charity & Donations": "Events & Outings",
    # Investments
    "Investment": "Mutual Funds & SIPs",
    "Precious Metals & Gold": "Stocks & Securities",
    "Fixed Deposits & Savings": "Mutual Funds & SIPs",
    # Loans
    "EMI": "Loan EMIs",
    "Loan EMIs & Repayments": "Loan EMIs",
    "Home & Personal Loans": "Loan EMIs",
    "Shared Expenses & Splits": "Loan EMIs",
}

# The dictionary seed mapping after streamlining (pattern, canonical, category_name)
_NEW_MERCHANT_SEED: tuple[tuple[str, str, str], ...] = (
    ("swiggy", "swiggy", "Dining Out & Delivery"),
    ("zomato", "zomato", "Dining Out & Delivery"),
    ("eatsure", "eatsure", "Dining Out & Delivery"),
    ("box8", "box8", "Dining Out & Delivery"),
    ("faasos", "faasos", "Dining Out & Delivery"),
    ("dominos", "dominos", "Dining Out & Delivery"),
    ("pizza hut", "pizza hut", "Dining Out & Delivery"),
    ("mcdonalds", "mcdonalds", "Dining Out & Delivery"),
    ("kfc", "kfc", "Dining Out & Delivery"),
    ("burger king", "burger king", "Dining Out & Delivery"),
    ("subway", "subway", "Dining Out & Delivery"),
    ("starbucks", "starbucks", "Dining Out & Delivery"),
    ("barista", "barista", "Dining Out & Delivery"),
    ("chaayos", "chaayos", "Dining Out & Delivery"),
    ("bigbasket", "big basket", "Groceries"),
    ("big basket", "big basket", "Groceries"),
    ("swiggy instamart", "swiggy instamart", "Groceries"),
    ("dmart", "dmart", "Groceries"),
    ("jiomart", "jiomart", "Groceries"),
    ("natures basket", "natures basket", "Groceries"),
    ("reliance fresh", "reliance fresh", "Groceries"),
    ("milkbasket", "milkbasket", "Groceries"),
    ("licious", "licious", "Groceries"),
    ("country delight", "country delight", "Groceries"),
    ("uber", "uber", "Daily Commute"),
    ("uber eats", "uber eats", "Dining Out & Delivery"),
    ("ola", "ola", "Daily Commute"),
    ("rapido", "rapido", "Daily Commute"),
    ("namma yatri", "namma yatri", "Daily Commute"),
    ("yulu", "yulu", "Daily Commute"),
    ("netflix", "netflix", "Streaming & Subscriptions"),
    ("spotify", "spotify", "Streaming & Subscriptions"),
    ("hotstar", "hotstar", "Streaming & Subscriptions"),
    ("disney hotstar", "hotstar", "Streaming & Subscriptions"),
    ("sonyliv", "sonyliv", "Streaming & Subscriptions"),
    ("zee5", "zee5", "Streaming & Subscriptions"),
    ("jiocinema", "jiocinema", "Streaming & Subscriptions"),
    ("youtube premium", "youtube premium", "Streaming & Subscriptions"),
    ("amazon prime", "amazon prime", "Streaming & Subscriptions"),
    ("amazon prime video", "amazon prime video", "Streaming & Subscriptions"),
    ("wynk music", "wynk music", "Streaming & Subscriptions"),
    ("audible", "audible", "Streaming & Subscriptions"),
    ("kindle unlimited", "kindle unlimited", "Streaming & Subscriptions"),
    ("icloud", "icloud", "Streaming & Subscriptions"),
    ("office 365", "office 365", "Streaming & Subscriptions"),
    ("google one", "google one", "Streaming & Subscriptions"),
    ("linkedin premium", "linkedin premium", "Streaming & Subscriptions"),
    ("makemytrip", "makemytrip", "Flights & Trains"),
    ("irctc", "irctc", "Flights & Trains"),
    ("goibibo", "goibibo", "Flights & Trains"),
    ("cleartrip", "cleartrip", "Flights & Trains"),
    ("redbus", "redbus", "Flights & Trains"),
    ("air india", "air india", "Flights & Trains"),
    ("spicejet", "spicejet", "Flights & Trains"),
    ("oyo", "oyo", "Hotels & Stays"),
    ("airbnb", "airbnb", "Hotels & Stays"),
    ("trivago", "trivago", "Hotels & Stays"),
    ("agoda", "agoda", "Hotels & Stays"),
    ("ixigo", "ixigo", "Flights & Trains"),
    ("abhibus", "abhibus", "Flights & Trains"),
    ("apollo pharmacy", "apollo pharmacy", "Pharmacy & Medicines"),
    ("pharmeasy", "pharmeasy", "Pharmacy & Medicines"),
    ("1mg", "1mg", "Pharmacy & Medicines"),
    ("netmeds", "netmeds", "Pharmacy & Medicines"),
    ("practo", "practo", "Doctor & Diagnostics"),
    ("cult fit", "cult fit", "Doctor & Diagnostics"),
    ("healthkart", "healthkart", "Pharmacy & Medicines"),
    ("lal pathlabs", "lal pathlabs", "Doctor & Diagnostics"),
    ("metropolis healthcare", "metropolis healthcare", "Doctor & Diagnostics"),
    ("thyrocare", "thyrocare", "Doctor & Diagnostics"),
    ("fortis healthcare", "fortis healthcare", "Doctor & Diagnostics"),
    ("max healthcare", "max healthcare", "Doctor & Diagnostics"),
    ("manipal hospitals", "manipal hospitals", "Doctor & Diagnostics"),
    ("apollo hospitals", "apollo hospitals", "Doctor & Diagnostics"),
    ("medlife", "medlife", "Pharmacy & Medicines"),
    ("flipkart", "flipkart", "Shopping"),
    ("myntra", "myntra", "Clothing & Personal"),
    ("ajio", "ajio", "Clothing & Personal"),
    ("nykaa", "nykaa", "Clothing & Personal"),
    ("croma", "croma", "Electronics & Home"),
    ("tata cliq", "tata cliq", "Shopping"),
    ("meesho", "meesho", "Shopping"),
    ("lenskart", "lenskart", "Clothing & Personal"),
    ("decathlon", "decathlon", "Clothing & Personal"),
    ("ikea", "ikea", "Electronics & Home"),
    ("pepperfry", "pepperfry", "Electronics & Home"),
    ("urban company", "urban company", "Rent & Maintenance"),
    ("airtel", "airtel", "Bills & Utilities"),
    ("bsnl", "bsnl", "Bills & Utilities"),
    ("tata play", "tata play", "Bills & Utilities"),
    ("act fibernet", "act fibernet", "Bills & Utilities"),
    ("jiofiber", "jiofiber", "Bills & Utilities"),
    ("vodafone idea", "vodafone idea", "Bills & Utilities"),
    ("bookmyshow", "bookmyshow", "Events & Outings"),
    ("inox", "inox", "Events & Outings"),
    ("cinepolis", "cinepolis", "Events & Outings"),
    ("pvr", "pvr", "Events & Outings"),
)

# Frozen duplicate of 0035 dictionary seed for reversible downgrade
_PRE_0049_MERCHANT_SEED: tuple[tuple[str, str, str], ...] = (
    ("swiggy", "swiggy", "Online Food Delivery"),
    ("zomato", "zomato", "Online Food Delivery"),
    ("eatsure", "eatsure", "Online Food Delivery"),
    ("box8", "box8", "Online Food Delivery"),
    ("faasos", "faasos", "Online Food Delivery"),
    ("dominos", "dominos", "Online Food Delivery"),
    ("pizza hut", "pizza hut", "Online Food Delivery"),
    ("mcdonalds", "mcdonalds", "Quick Bites & Snacks"),
    ("kfc", "kfc", "Quick Bites & Snacks"),
    ("burger king", "burger king", "Quick Bites & Snacks"),
    ("subway", "subway", "Quick Bites & Snacks"),
    ("starbucks", "starbucks", "Coffee & Tea"),
    ("barista", "barista", "Coffee & Tea"),
    ("chaayos", "chaayos", "Coffee & Tea"),
    ("bigbasket", "big basket", "Groceries"),
    ("big basket", "big basket", "Groceries"),
    ("swiggy instamart", "swiggy instamart", "Instant Grocery Delivery"),
    ("dmart", "dmart", "Groceries"),
    ("jiomart", "jiomart", "Groceries"),
    ("natures basket", "natures basket", "Groceries"),
    ("reliance fresh", "reliance fresh", "Groceries"),
    ("milkbasket", "milkbasket", "Instant Grocery Delivery"),
    ("licious", "licious", "Groceries"),
    ("country delight", "country delight", "Groceries"),
    ("uber", "uber", "Ride-Hailing & Taxis"),
    ("uber eats", "uber eats", "Online Food Delivery"),
    ("ola", "ola", "Ride-Hailing & Taxis"),
    ("rapido", "rapido", "Ride-Hailing & Taxis"),
    ("namma yatri", "namma yatri", "Ride-Hailing & Taxis"),
    ("yulu", "yulu", "Ride-Hailing & Taxis"),
    ("netflix", "netflix", "Digital Subscriptions & Streaming"),
    ("spotify", "spotify", "Digital Subscriptions & Streaming"),
    ("hotstar", "hotstar", "Digital Subscriptions & Streaming"),
    ("disney hotstar", "hotstar", "Digital Subscriptions & Streaming"),
    ("sonyliv", "sonyliv", "Digital Subscriptions & Streaming"),
    ("zee5", "zee5", "Digital Subscriptions & Streaming"),
    ("jiocinema", "jiocinema", "Digital Subscriptions & Streaming"),
    ("youtube premium", "youtube premium", "Digital Subscriptions & Streaming"),
    ("amazon prime", "amazon prime", "Digital Subscriptions & Streaming"),
    ("amazon prime video", "amazon prime video", "Digital Subscriptions & Streaming"),
    ("wynk music", "wynk music", "Digital Subscriptions & Streaming"),
    ("audible", "audible", "Digital Subscriptions & Streaming"),
    ("kindle unlimited", "kindle unlimited", "Digital Subscriptions & Streaming"),
    ("icloud", "icloud", "Digital Subscriptions & Streaming"),
    ("office 365", "office 365", "Digital Subscriptions & Streaming"),
    ("google one", "google one", "Digital Subscriptions & Streaming"),
    ("linkedin premium", "linkedin premium", "Digital Subscriptions & Streaming"),
    ("makemytrip", "makemytrip", "Travel"),
    ("irctc", "irctc", "Travel"),
    ("goibibo", "goibibo", "Travel"),
    ("cleartrip", "cleartrip", "Travel"),
    ("redbus", "redbus", "Travel"),
    ("air india", "air india", "Travel"),
    ("spicejet", "spicejet", "Travel"),
    ("oyo", "oyo", "Travel"),
    ("airbnb", "airbnb", "Travel"),
    ("trivago", "trivago", "Travel"),
    ("agoda", "agoda", "Travel"),
    ("ixigo", "ixigo", "Travel"),
    ("abhibus", "abhibus", "Travel"),
    ("apollo pharmacy", "apollo pharmacy", "Health"),
    ("pharmeasy", "pharmeasy", "Health"),
    ("1mg", "1mg", "Health"),
    ("netmeds", "netmeds", "Health"),
    ("practo", "practo", "Health"),
    ("cult fit", "cult fit", "Health"),
    ("healthkart", "healthkart", "Health"),
    ("lal pathlabs", "lal pathlabs", "Health"),
    ("metropolis healthcare", "metropolis healthcare", "Health"),
    ("thyrocare", "thyrocare", "Health"),
    ("fortis healthcare", "fortis healthcare", "Health"),
    ("max healthcare", "max healthcare", "Health"),
    ("manipal hospitals", "manipal hospitals", "Health"),
    ("apollo hospitals", "apollo hospitals", "Health"),
    ("medlife", "medlife", "Health"),
    ("flipkart", "flipkart", "Shopping"),
    ("myntra", "myntra", "Clothing & Apparel"),
    ("ajio", "ajio", "Clothing & Apparel"),
    ("nykaa", "nykaa", "Personal Care & Grooming"),
    ("croma", "croma", "Electronics & Gadgets"),
    ("tata cliq", "tata cliq", "Shopping"),
    ("meesho", "meesho", "Shopping"),
    ("lenskart", "lenskart", "Footwear & Accessories"),
    ("decathlon", "decathlon", "Clothing & Apparel"),
    ("ikea", "ikea", "Furniture & Appliances"),
    ("pepperfry", "pepperfry", "Furniture & Appliances"),
    ("urban company", "urban company", "Household Help & Domestic Staff"),
    ("airtel", "airtel", "Mobile & Broadband"),
    ("bsnl", "bsnl", "Mobile & Broadband"),
    ("tata play", "tata play", "Cable & Satellite TV"),
    ("act fibernet", "act fibernet", "Mobile & Broadband"),
    ("jiofiber", "jiofiber", "Mobile & Broadband"),
    ("vodafone idea", "vodafone idea", "Mobile & Broadband"),
    ("bookmyshow", "bookmyshow", "Entertainment"),
    ("inox", "inox", "Entertainment"),
    ("cinepolis", "cinepolis", "Events & Outings"),
    ("pvr", "pvr", "Entertainment"),
)


def _is_category_referenced(bind: sa.Connection, user_id: object, category_id: int) -> bool:
    """Check if category is referenced by transactions, merchant tags, or child categories."""
    probes = (
        sa.select(sa.literal(1)).where(
            _transactions.c.user_id == user_id,
            sa.or_(
                _transactions.c.category_id == category_id,
                _transactions.c.auto_category_id == category_id,
                _transactions.c.pre_link_category_id == category_id,
            ),
        ),
        sa.select(sa.literal(1)).where(
            _merchant_tag_map.c.user_id == user_id,
            _merchant_tag_map.c.category_id == category_id,
        ),
        sa.select(sa.literal(1)).where(
            _categories.c.user_id == user_id,
            _categories.c.parent_id == category_id,
            _categories.c.archived_at.is_(None),
        ),
    )
    return any(bind.execute(probe.limit(1)).first() is not None for probe in probes)


def upgrade() -> None:
    bind = op.get_bind()
    now = _now()
    user_ids = [row.id for row in bind.execute(sa.select(_users.c.id)).all()]

    for user_id in user_ids:
        # Step 1: Archive colliding categories before renames / creations
        # - Obsolete subcategory "Shopping" (collides with root rename "Shopping")
        # - Old root "Bills & Utilities" (collides with new subcategory "Bills & Utilities")
        bind.execute(
            _categories.update()
            .where(
                _categories.c.user_id == user_id,
                _categories.c.name == "Shopping",
                _categories.c.parent_id.is_not(None),
                _categories.c.is_seeded.is_(True),
                _categories.c.archived_at.is_(None),
            )
            .values(archived_at=now)
        )
        bind.execute(
            _categories.update()
            .where(
                _categories.c.user_id == user_id,
                _categories.c.name == "Bills & Utilities",
                _categories.c.parent_id.is_(None),
                _categories.c.is_seeded.is_(True),
                _categories.c.archived_at.is_(None),
            )
            .values(archived_at=now)
        )

        # Step 2: Rename roots
        for old_name, (new_name, color, _) in _ROOT_RENAMES.items():
            bind.execute(
                _categories.update()
                .where(
                    _categories.c.user_id == user_id,
                    _categories.c.name == old_name,
                    _categories.c.parent_id.is_(None),
                    _categories.c.archived_at.is_(None),
                )
                .values(name=new_name, color=color)
            )

        # Step 3: Create new roots if absent
        for root_name, color in _NEW_ROOTS:
            existing = bind.execute(
                sa.select(_categories.c.id).where(
                    _categories.c.user_id == user_id,
                    _categories.c.name == root_name,
                    _categories.c.parent_id.is_(None),
                    _categories.c.archived_at.is_(None),
                )
            ).scalar_one_or_none()
            if existing is None:
                bind.execute(
                    _categories.insert().values(
                        user_id=user_id,
                        name=root_name,
                        kind="spend",
                        is_seeded=True,
                        color=color,
                        parent_id=None,
                        archived_at=None,
                    )
                )

        # Step 4: Create new subcategories under active parents
        parents = {
            row.name: row.id
            for row in bind.execute(
                sa.select(_categories.c.name, _categories.c.id).where(
                    _categories.c.user_id == user_id,
                    _categories.c.parent_id.is_(None),
                    _categories.c.archived_at.is_(None),
                )
            ).all()
        }
        for parent_name, sub_name in _NEW_SUBCATEGORIES:
            parent_id = parents.get(parent_name)
            if parent_id is None:
                continue
            existing_sub = bind.execute(
                sa.select(_categories.c.id).where(
                    _categories.c.user_id == user_id,
                    _categories.c.name == sub_name,
                    _categories.c.parent_id == parent_id,
                    _categories.c.archived_at.is_(None),
                )
            ).scalar_one_or_none()
            if existing_sub is None:
                bind.execute(
                    _categories.insert().values(
                        user_id=user_id,
                        name=sub_name,
                        kind="spend",
                        is_seeded=True,
                        color=None,
                        parent_id=parent_id,
                        archived_at=None,
                    )
                )

        # Step 5: Reassign transactions from obsolete categories to active streamlined categories
        active_cats = {
            row.name: row.id
            for row in bind.execute(
                sa.select(_categories.c.name, _categories.c.id).where(
                    _categories.c.user_id == user_id,
                    _categories.c.kind == "spend",
                    _categories.c.archived_at.is_(None),
                )
            ).all()
        }
        all_cat_ids_by_name: dict[str, list[int]] = {}
        for row in bind.execute(
            sa.select(_categories.c.name, _categories.c.id).where(
                _categories.c.user_id == user_id,
                _categories.c.kind == "spend",
            )
        ).all():
            all_cat_ids_by_name.setdefault(row.name, []).append(row.id)

        for old_name, target_name in _REASSIGN_MAP.items():
            target_id = active_cats.get(target_name)
            if target_id is None:
                continue

            old_ids = [cid for cid in all_cat_ids_by_name.get(old_name, []) if cid != target_id]
            if not old_ids:
                continue

            bind.execute(
                _transactions.update()
                .where(
                    _transactions.c.user_id == user_id,
                    _transactions.c.category_id.in_(old_ids),
                )
                .values(category_id=target_id)
            )
            bind.execute(
                _transactions.update()
                .where(
                    _transactions.c.user_id == user_id,
                    _transactions.c.auto_category_id.in_(old_ids),
                )
                .values(auto_category_id=target_id)
            )
            bind.execute(
                _transactions.update()
                .where(
                    _transactions.c.user_id == user_id,
                    _transactions.c.pre_link_category_id.in_(old_ids),
                )
                .values(pre_link_category_id=target_id)
            )

        # Step 6: Archive all obsolete seeded categories
        for old_name in _REASSIGN_MAP:
            # Skip newly created active subcategories that share names with obsolete roots/subs
            bind.execute(
                _categories.update()
                .where(
                    _categories.c.user_id == user_id,
                    _categories.c.name == old_name,
                    _categories.c.is_seeded.is_(True),
                    _categories.c.archived_at.is_(None),
                    # Do not archive newly active subcategory "Bills & Utilities" or root "Shopping"
                    sa.not_(
                        sa.or_(
                            sa.and_(
                                _categories.c.name == "Bills & Utilities",
                                _categories.c.parent_id.is_not(None),
                            ),
                            sa.and_(
                                _categories.c.name == "Shopping",
                                _categories.c.parent_id.is_(None),
                            ),
                        )
                    ),
                )
                .values(archived_at=now)
            )

        # Step 7: Synchronize merchant_alias and merchant_tag_map
        active_cats = {
            row.name: row.id
            for row in bind.execute(
                sa.select(_categories.c.name, _categories.c.id).where(
                    _categories.c.user_id == user_id,
                    _categories.c.kind == "spend",
                    _categories.c.archived_at.is_(None),
                )
            ).all()
        }

        # 7a: Update existing tag map seed rows (hit_count == 0, not pinned) to
        # point to new category
        existing_tag_rows = bind.execute(
            sa.select(
                _merchant_tag_map.c.id,
                _merchant_tag_map.c.merchant_normalized,
                _merchant_tag_map.c.category_id,
                _merchant_tag_map.c.hit_count,
                _merchant_tag_map.c.pinned,
            ).where(_merchant_tag_map.c.user_id == user_id)
        ).all()
        rows_by_merchant: dict[str, list[object]] = {}
        for r in existing_tag_rows:
            rows_by_merchant.setdefault(r.merchant_normalized, []).append(r)

        for _, canonical, target_name in _NEW_MERCHANT_SEED:
            target_cat_id = active_cats.get(target_name)
            if target_cat_id is None:
                continue

            merchant_rows = rows_by_merchant.get(canonical, [])
            target_row = next((r for r in merchant_rows if r.category_id == target_cat_id), None)
            seed_row = next((r for r in merchant_rows if r.hit_count == 0 and not r.pinned), None)

            if target_row is not None:
                if seed_row is not None and seed_row.id != target_row.id:
                    bind.execute(
                        _merchant_tag_map.delete().where(
                            _merchant_tag_map.c.user_id == user_id,
                            _merchant_tag_map.c.id == seed_row.id,
                        )
                    )
            elif seed_row is not None:
                bind.execute(
                    _merchant_tag_map.update()
                    .where(
                        _merchant_tag_map.c.user_id == user_id,
                        _merchant_tag_map.c.id == seed_row.id,
                    )
                    .values(category_id=target_cat_id)
                )

        # 7b: Insert missing merchant_alias and merchant_tag_map rows for newly introduced merchants
        existing_patterns = set(
            bind.execute(
                sa.select(_merchant_alias.c.pattern).where(_merchant_alias.c.user_id == user_id)
            )
            .scalars()
            .all()
        )
        existing_map_keys = {
            (r.merchant_normalized, r.category_id)
            for r in bind.execute(
                sa.select(
                    _merchant_tag_map.c.merchant_normalized,
                    _merchant_tag_map.c.category_id,
                ).where(_merchant_tag_map.c.user_id == user_id)
            ).all()
        }

        for pattern, canonical, target_name in _NEW_MERCHANT_SEED:
            target_cat_id = active_cats.get(target_name)
            if target_cat_id is None:
                continue

            if pattern not in existing_patterns:
                bind.execute(
                    _merchant_alias.insert().values(
                        user_id=user_id,
                        pattern=pattern,
                        canonical=canonical,
                        is_seeded=True,
                    )
                )
                existing_patterns.add(pattern)

            map_key = (canonical, target_cat_id)
            if canonical not in rows_by_merchant and map_key not in existing_map_keys:
                bind.execute(
                    _merchant_tag_map.insert().values(
                        user_id=user_id,
                        merchant_normalized=canonical,
                        category_id=target_cat_id,
                        hit_count=0,
                        pinned=False,
                    )
                )
                existing_map_keys.add(map_key)
                rows_by_merchant.setdefault(canonical, [])


def downgrade() -> None:
    bind = op.get_bind()
    now = _now()
    user_ids = [row.id for row in bind.execute(sa.select(_users.c.id)).all()]

    new_sub_names = {sub_name for _, sub_name in _NEW_SUBCATEGORIES}
    new_root_names = {root_name for root_name, _ in _NEW_ROOTS}

    for user_id in user_ids:
        # Step 1: Revert root renames and restored colors
        for old_name, (new_name, _, old_color) in _ROOT_RENAMES.items():
            bind.execute(
                _categories.update()
                .where(
                    _categories.c.user_id == user_id,
                    _categories.c.name == new_name,
                    _categories.c.parent_id.is_(None),
                    _categories.c.archived_at.is_(None),
                )
                .values(name=old_name, color=old_color)
            )

        # Step 2: Un-archive retired categories (most recently archived seeded row per name)
        for old_name in _REASSIGN_MAP:
            active_exists = bind.execute(
                sa.select(_categories.c.id).where(
                    _categories.c.user_id == user_id,
                    _categories.c.name == old_name,
                    _categories.c.kind == "spend",
                    _categories.c.archived_at.is_(None),
                )
            ).scalar_one_or_none()
            if active_exists is not None:
                continue

            latest_archived_id = bind.execute(
                sa.select(_categories.c.id)
                .where(
                    _categories.c.user_id == user_id,
                    _categories.c.name == old_name,
                    _categories.c.kind == "spend",
                    _categories.c.is_seeded.is_(True),
                    _categories.c.archived_at.is_not(None),
                )
                .order_by(_categories.c.archived_at.desc(), _categories.c.id.desc())
                .limit(1)
            ).scalar_one_or_none()
            if latest_archived_id is not None:
                bind.execute(
                    _categories.update()
                    .where(
                        _categories.c.user_id == user_id,
                        _categories.c.id == latest_archived_id,
                    )
                    .values(archived_at=None)
                )

        # Step 3: Revert merchant tag map seed rows to pre-0049 (0035) dictionary
        # If the user has no active 0035 categories (e.g. a user created after 0032/0035 who
        # was only seeded in 0049), delete all unpinned seed rows since they were inserted by 0049.
        active_cats_0035 = {
            row.name: row.id
            for row in bind.execute(
                sa.select(_categories.c.name, _categories.c.id).where(
                    _categories.c.user_id == user_id,
                    _categories.c.kind == "spend",
                    _categories.c.archived_at.is_(None),
                )
            ).all()
        }
        legacy_flat_cats = {
            row.name
            for row in bind.execute(
                sa.select(_categories.c.name).where(
                    _categories.c.user_id == user_id,
                    _categories.c.kind == "spend",
                    _categories.c.parent_id.is_(None),
                    _categories.c.name.in_({"Food", "Transport", "Shopping", "Health"}),
                )
            ).all()
        }
        if not legacy_flat_cats:
            # User was not seeded in 0032/0035; delete all unpinned seed rows inserted by 0049
            bind.execute(
                _merchant_tag_map.delete().where(
                    _merchant_tag_map.c.user_id == user_id,
                    _merchant_tag_map.c.hit_count == 0,
                    _merchant_tag_map.c.pinned.is_(False),
                )
            )
            bind.execute(
                _merchant_alias.delete().where(
                    _merchant_alias.c.user_id == user_id,
                    _merchant_alias.c.is_seeded.is_(True),
                )
            )
        else:
            tag_rows = bind.execute(
                sa.select(
                    _merchant_tag_map.c.id,
                    _merchant_tag_map.c.merchant_normalized,
                    _merchant_tag_map.c.category_id,
                    _merchant_tag_map.c.hit_count,
                    _merchant_tag_map.c.pinned,
                ).where(_merchant_tag_map.c.user_id == user_id)
            ).all()
            tag_rows_by_merchant: dict[str, list[object]] = {}
            for r in tag_rows:
                tag_rows_by_merchant.setdefault(r.merchant_normalized, []).append(r)

            for _, canonical, pre49_cat_name in _PRE_0049_MERCHANT_SEED:
                target_cat_id = active_cats_0035.get(pre49_cat_name)
                if target_cat_id is None:
                    continue

                merchant_rows = tag_rows_by_merchant.get(canonical, [])
                target_row = next(
                    (r for r in merchant_rows if r.category_id == target_cat_id), None
                )
                seed_row = next(
                    (r for r in merchant_rows if r.hit_count == 0 and not r.pinned), None
                )

                if target_row is not None:
                    if seed_row is not None and seed_row.id != target_row.id:
                        bind.execute(
                            _merchant_tag_map.delete().where(
                                _merchant_tag_map.c.user_id == user_id,
                                _merchant_tag_map.c.id == seed_row.id,
                            )
                        )
                elif seed_row is not None:
                    bind.execute(
                        _merchant_tag_map.update()
                        .where(
                            _merchant_tag_map.c.user_id == user_id,
                            _merchant_tag_map.c.id == seed_row.id,
                        )
                        .values(category_id=target_cat_id)
                    )

        # Step 4: Clean up any remaining merchant tag map rows pointing to new categories
        new_cat_rows = bind.execute(
            sa.select(_categories.c.id, _categories.c.name).where(
                _categories.c.user_id == user_id,
                _categories.c.is_seeded.is_(True),
                sa.or_(
                    sa.and_(
                        _categories.c.parent_id.is_not(None),
                        _categories.c.name.in_(new_sub_names),
                    ),
                    sa.and_(
                        _categories.c.parent_id.is_(None),
                        _categories.c.name.in_(new_root_names),
                    ),
                ),
            )
        ).all()
        new_cat_ids = [r.id for r in new_cat_rows]

        if new_cat_ids:
            bind.execute(
                _merchant_tag_map.delete().where(
                    _merchant_tag_map.c.user_id == user_id,
                    _merchant_tag_map.c.category_id.in_(new_cat_ids),
                    _merchant_tag_map.c.hit_count == 0,
                    _merchant_tag_map.c.pinned.is_(False),
                )
            )

        # Step 5: Remove new subcategories (delete if unreferenced, else archive)
        sub_rows = bind.execute(
            sa.select(_categories.c.id).where(
                _categories.c.user_id == user_id,
                _categories.c.parent_id.is_not(None),
                _categories.c.name.in_(new_sub_names),
                _categories.c.is_seeded.is_(True),
            )
        ).all()
        for row in sub_rows:
            if not _is_category_referenced(bind, user_id, row.id):
                bind.execute(
                    _categories.delete().where(
                        _categories.c.user_id == user_id,
                        _categories.c.id == row.id,
                    )
                )
            else:
                bind.execute(
                    _categories.update()
                    .where(
                        _categories.c.user_id == user_id,
                        _categories.c.id == row.id,
                    )
                    .values(archived_at=now)
                )

        # Step 6: Remove new roots added by 0049 (delete if unreferenced, else archive)
        root_rows = bind.execute(
            sa.select(_categories.c.id).where(
                _categories.c.user_id == user_id,
                _categories.c.parent_id.is_(None),
                _categories.c.name.in_(new_root_names),
                _categories.c.is_seeded.is_(True),
            )
        ).all()
        for row in root_rows:
            if not _is_category_referenced(bind, user_id, row.id):
                bind.execute(
                    _categories.delete().where(
                        _categories.c.user_id == user_id,
                        _categories.c.id == row.id,
                    )
                )
            else:
                bind.execute(
                    _categories.update()
                    .where(
                        _categories.c.user_id == user_id,
                        _categories.c.id == row.id,
                    )
                    .values(archived_at=now)
                )
