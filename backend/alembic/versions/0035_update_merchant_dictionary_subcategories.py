"""update merchant dictionary subcategories

Revision ID: 0035_update_merchant_dictionary_subcategories
Revises: 0034_seed_category_taxonomy
Create Date: 2026-08-14

Updates existing users' seed merchant_tag_map rows (hit_count = 0 AND pinned = False)
from legacy parent categories to fine-grained subcategories under the 2-level taxonomy.
Preserves all user-learned (hit_count > 0) and user-pinned (pinned = True) tag rules.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0035_update_merchant_dictionary_subcategories"
down_revision: str | Sequence[str] | None = "0034_seed_category_taxonomy"
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
    sa.column("archived_at", sa.DateTime()),
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

# Upgraded merchant dictionary with fine-grained subcategories
_MERCHANT_DICTIONARY: tuple[tuple[str, str, str], ...] = (
    # Food & Dining
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
    # Groceries & Quick Commerce
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
    # Commute & Transportation
    ("uber", "uber", "Ride-Hailing & Taxis"),
    ("uber eats", "uber eats", "Online Food Delivery"),
    ("ola", "ola", "Ride-Hailing & Taxis"),
    ("rapido", "rapido", "Ride-Hailing & Taxis"),
    ("namma yatri", "namma yatri", "Ride-Hailing & Taxis"),
    ("yulu", "yulu", "Ride-Hailing & Taxis"),
    # Digital Subscriptions & Streaming
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
    # Travel
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
    # Health
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
    # Shopping & Lifestyle
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
    # Bills & Utilities
    ("airtel", "airtel", "Mobile & Broadband"),
    ("bsnl", "bsnl", "Mobile & Broadband"),
    ("tata play", "tata play", "Cable & Satellite TV"),
    ("act fibernet", "act fibernet", "Mobile & Broadband"),
    ("jiofiber", "jiofiber", "Mobile & Broadband"),
    ("vodafone idea", "vodafone idea", "Mobile & Broadband"),
    # Entertainment
    ("bookmyshow", "bookmyshow", "Entertainment"),
    ("inox", "inox", "Entertainment"),
    ("cinepolis", "cinepolis", "Entertainment"),
    ("pvr", "pvr", "Entertainment"),
)

# Legacy dictionary mappings for downgrade
_LEGACY_MERCHANT_DICTIONARY: tuple[tuple[str, str, str], ...] = (
    # Food
    ("swiggy", "swiggy", "Food"),
    ("zomato", "zomato", "Food"),
    ("eatsure", "eatsure", "Food"),
    ("box8", "box8", "Food"),
    ("faasos", "faasos", "Food"),
    ("dominos", "dominos", "Food"),
    ("pizza hut", "pizza hut", "Food"),
    ("mcdonalds", "mcdonalds", "Food"),
    ("kfc", "kfc", "Food"),
    ("burger king", "burger king", "Food"),
    ("subway", "subway", "Food"),
    ("starbucks", "starbucks", "Food"),
    ("barista", "barista", "Food"),
    ("chaayos", "chaayos", "Food"),
    # Groceries
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
    # Transport
    ("uber", "uber", "Transport"),
    ("uber eats", "uber eats", "Food"),
    ("ola", "ola", "Transport"),
    ("rapido", "rapido", "Transport"),
    ("namma yatri", "namma yatri", "Transport"),
    ("yulu", "yulu", "Transport"),
    # Subscriptions
    ("netflix", "netflix", "Subscriptions"),
    ("spotify", "spotify", "Subscriptions"),
    ("hotstar", "hotstar", "Subscriptions"),
    ("disney hotstar", "hotstar", "Subscriptions"),
    ("sonyliv", "sonyliv", "Subscriptions"),
    ("zee5", "zee5", "Subscriptions"),
    ("jiocinema", "jiocinema", "Subscriptions"),
    ("youtube premium", "youtube premium", "Subscriptions"),
    ("amazon prime", "amazon prime", "Subscriptions"),
    ("amazon prime video", "amazon prime video", "Subscriptions"),
    ("wynk music", "wynk music", "Subscriptions"),
    ("audible", "audible", "Subscriptions"),
    ("kindle unlimited", "kindle unlimited", "Subscriptions"),
    ("icloud", "icloud", "Subscriptions"),
    ("office 365", "office 365", "Subscriptions"),
    ("google one", "google one", "Subscriptions"),
    ("linkedin premium", "linkedin premium", "Subscriptions"),
    # Travel
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
    # Health
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
    # Shopping
    ("flipkart", "flipkart", "Shopping"),
    ("myntra", "myntra", "Shopping"),
    ("ajio", "ajio", "Shopping"),
    ("nykaa", "nykaa", "Shopping"),
    ("croma", "croma", "Shopping"),
    ("tata cliq", "tata cliq", "Shopping"),
    ("meesho", "meesho", "Shopping"),
    ("lenskart", "lenskart", "Shopping"),
    ("decathlon", "decathlon", "Shopping"),
    ("ikea", "ikea", "Shopping"),
    ("pepperfry", "pepperfry", "Shopping"),
    ("urban company", "urban company", "Shopping"),
    # Utilities
    ("airtel", "airtel", "Utilities"),
    ("bsnl", "bsnl", "Utilities"),
    ("tata play", "tata play", "Utilities"),
    ("act fibernet", "act fibernet", "Utilities"),
    ("jiofiber", "jiofiber", "Utilities"),
    ("vodafone idea", "vodafone idea", "Utilities"),
    # Entertainment
    ("bookmyshow", "bookmyshow", "Entertainment"),
    ("inox", "inox", "Entertainment"),
    ("cinepolis", "cinepolis", "Entertainment"),
    ("pvr", "pvr", "Entertainment"),
)


def upgrade() -> None:
    bind = op.get_bind()
    user_ids = [row.id for row in bind.execute(sa.select(_users.c.id)).all()]
    needed_names = {name for _, _, name in _MERCHANT_DICTIONARY}

    for user_id in user_ids:
        name_to_id = {
            name: category_id
            for name, category_id in bind.execute(
                sa.select(_categories.c.name, _categories.c.id).where(
                    _categories.c.user_id == user_id,
                    _categories.c.kind == "spend",
                    _categories.c.archived_at.is_(None),
                    _categories.c.name.in_(needed_names),
                )
            ).all()
        }

        # Fetch all existing tag map rows for this user
        existing_rows = bind.execute(
            sa.select(
                _merchant_tag_map.c.id,
                _merchant_tag_map.c.merchant_normalized,
                _merchant_tag_map.c.category_id,
                _merchant_tag_map.c.hit_count,
                _merchant_tag_map.c.pinned,
            ).where(_merchant_tag_map.c.user_id == user_id)
        ).all()

        # Index existing rows by merchant_normalized -> list of row tuples
        rows_by_merchant: dict[str, list[object]] = {}
        for row in existing_rows:
            rows_by_merchant.setdefault(row.merchant_normalized, []).append(row)

        for _, canonical, subcategory_name in _MERCHANT_DICTIONARY:
            target_cat_id = name_to_id.get(subcategory_name)
            if target_cat_id is None:
                continue

            merchant_rows = rows_by_merchant.get(canonical, [])
            target_row = next((r for r in merchant_rows if r.category_id == target_cat_id), None)
            seed_row = next((r for r in merchant_rows if r.hit_count == 0 and not r.pinned), None)

            if target_row is not None:
                # If target mapping already exists and there is another unpinned seed row,
                # delete obsolete seed row
                if seed_row is not None and seed_row.id != target_row.id:
                    bind.execute(
                        _merchant_tag_map.delete().where(_merchant_tag_map.c.id == seed_row.id)
                    )
            elif seed_row is not None:
                # Update existing unpinned seed row to the target category
                bind.execute(
                    _merchant_tag_map.update()
                    .where(_merchant_tag_map.c.id == seed_row.id)
                    .values(category_id=target_cat_id)
                )


def downgrade() -> None:
    bind = op.get_bind()
    user_ids = [row.id for row in bind.execute(sa.select(_users.c.id)).all()]
    needed_names = {name for _, _, name in _LEGACY_MERCHANT_DICTIONARY}

    for user_id in user_ids:
        name_to_id = {
            name: category_id
            for name, category_id in bind.execute(
                sa.select(_categories.c.name, _categories.c.id).where(
                    _categories.c.user_id == user_id,
                    _categories.c.kind == "spend",
                    _categories.c.archived_at.is_(None),
                    _categories.c.name.in_(needed_names),
                )
            ).all()
        }

        existing_rows = bind.execute(
            sa.select(
                _merchant_tag_map.c.id,
                _merchant_tag_map.c.merchant_normalized,
                _merchant_tag_map.c.category_id,
                _merchant_tag_map.c.hit_count,
                _merchant_tag_map.c.pinned,
            ).where(_merchant_tag_map.c.user_id == user_id)
        ).all()

        rows_by_merchant: dict[str, list[object]] = {}
        for row in existing_rows:
            rows_by_merchant.setdefault(row.merchant_normalized, []).append(row)

        for _, canonical, legacy_cat_name in _LEGACY_MERCHANT_DICTIONARY:
            legacy_cat_id = name_to_id.get(legacy_cat_name)
            if legacy_cat_id is None:
                continue

            merchant_rows = rows_by_merchant.get(canonical, [])
            target_row = next((r for r in merchant_rows if r.category_id == legacy_cat_id), None)
            seed_row = next((r for r in merchant_rows if r.hit_count == 0 and not r.pinned), None)

            if target_row is not None:
                if seed_row is not None and seed_row.id != target_row.id:
                    bind.execute(
                        _merchant_tag_map.delete().where(_merchant_tag_map.c.id == seed_row.id)
                    )
            elif seed_row is not None:
                bind.execute(
                    _merchant_tag_map.update()
                    .where(_merchant_tag_map.c.id == seed_row.id)
                    .values(category_id=legacy_cat_id)
                )
