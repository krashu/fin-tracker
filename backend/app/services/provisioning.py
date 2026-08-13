"""New-user provisioning (PRD §Users & access v2).

Categories are per-user reference data (unlike the global ``benchmark`` /
``fx_rate_quote`` tables). A freshly registered user therefore needs the same
default category set the seeded demo user has — otherwise their board has no
categories to tag against.

This is the **runtime source of truth** for that default set: the *active*
current shape after migrations 0003 → 0012, i.e. the 13 active spend
categories (the vestigial flat "Income" / "Transfer" seeds are excluded — they
exist on the demo user only as archived migration cruft) plus the 4 income
categories, each with its 0012 default color. Migrations stay frozen snapshots
and never import this module; only :mod:`app.services.auth_service` (register)
calls it.

Also the runtime source of truth for the seed *merchant* dictionary (merchant-
alias arc, Phase A5 — research §13.6): ~96 seed ``(pattern, canonical,
category)`` rows written at ``hit_count=0`` / ``is_seeded=True`` (decision 4),
so a first import starts partially pre-tagged instead of at 0%. Migration
``0032_seed_merchant_dictionary`` duplicates the same literal tuple as a frozen
snapshot for existing-user backfill — see that module's docstring and
``tests/test_migration_parity.py::test_merchant_dictionary_matches_migration_seed``,
which polices the two staying in agreement.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Category, MerchantAlias, MerchantTagMap

# (name, color) — the legacy flat default spend categories.
_DEFAULT_SPEND_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("Food", "#d95926"),
    ("Groceries", "#6f9e15"),
    ("Transport", "#2a78d6"),
    ("Rent", "#6c5cd6"),
    ("Utilities", "#0e97c4"),
    ("Shopping", "#d55181"),
    ("Entertainment", "#b246c0"),
    ("Health", "#e34948"),
    ("Travel", "#0e9488"),
    ("Subscriptions", "#1baf7a"),
    ("EMI", "#c23b6b"),
    ("Investment", "#008300"),
    ("Other", "#94a3b8"),
)
# (name, color) — the legacy flat default income categories.
_DEFAULT_INCOME_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("Salary", "#008300"),
    ("Freelancing", "#2a78d6"),
    ("Cashback", "#c98500"),
    ("Other", "#94a3b8"),
)

# (parent_name, color, subcategories) — 2-level pure English taxonomy for Indian personal finance.
_DEFAULT_SPEND_TAXONOMY: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "Food & Dining",
        "#d95926",
        (
            "Food",
            "Groceries",
            "Online Food Delivery",
            "Restaurants & Cafes",
            "Quick Bites & Snacks",
            "Coffee & Tea",
        ),
    ),
    (
        "Household & Living",
        "#6c5cd6",
        (
            "Rent",
            "Rent & Maintenance",
            "Instant Grocery Delivery",
            "Household Help & Domestic Staff",
            "Home Improvements & Repairs",
            "Furniture & Appliances",
        ),
    ),
    (
        "Bills & Utilities",
        "#0e97c4",
        (
            "Utilities",
            "Mobile & Broadband",
            "Electricity",
            "Cooking Gas & LPG",
            "Cable & Satellite TV",
            "Water & Municipal Taxes",
        ),
    ),
    (
        "Commute & Transportation",
        "#2a78d6",
        (
            "Transport",
            "Travel",
            "Fuel & Petrol",
            "Metro & Public Transit",
            "Ride-Hailing & Taxis",
            "Highway Tolls & Parking",
            "Vehicle Service & Repairs",
        ),
    ),
    (
        "Shopping & Lifestyle",
        "#d55181",
        (
            "Shopping",
            "Entertainment",
            "Health",
            "Subscriptions",
            "Clothing & Apparel",
            "Electronics & Gadgets",
            "Personal Care & Grooming",
            "Footwear & Accessories",
            "Digital Subscriptions & Streaming",
        ),
    ),
    (
        "Family & Social",
        "#b246c0",
        (
            "Gifts & Celebrations",
            "Family Support & Transfers",
            "Education & Tuition",
            "Charity & Donations",
        ),
    ),
    (
        "Savings & Investments",
        "#008300",
        (
            "Investment",
            "Mutual Funds & SIPs",
            "Stocks & Securities",
            "Precious Metals & Gold",
            "Health & Life Insurance",
            "Fixed Deposits & Savings",
        ),
    ),
    (
        "Loans & Settlements",
        "#c23b6b",
        (
            "EMI",
            "Credit Card Payments",
            "Loan EMIs & Repayments",
            "Home & Personal Loans",
            "Shared Expenses & Splits",
        ),
    ),
    (
        "Other",
        "#94a3b8",
        (),
    ),
)

_DEFAULT_INCOME_TAXONOMY: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "Income",
        "#008300",
        (
            "Salary",
            "Freelancing",
            "Cashback",
            "Investment Returns",
            "Rental Income",
            "Other",
        ),
    ),
)

# (pattern, canonical, category_name) — the seed merchant dictionary (research §13.6).
# Every string is written ALREADY in normalize_merchant()-normalized form (lowercase,
# single-spaced) — test_seed_dictionary_entries_are_pre_normalized enforces that invariant
# so a typo can't silently produce a dead pattern. category_name must be one of
# _DEFAULT_SPEND_CATEGORIES' names or the entry inserts nothing (see
# provision_seed_merchant_dictionary). Two rows deliberately fold a spelling variant onto a
# shared canonical (BigBasket, Hotstar's Disney+ rename) — the fan-in this arc exists to
# prove. Every entry, including the ~94 where pattern == canonical, gets its own
# merchant_alias row — that identity row is what lets AliasResolver.canonical() match an
# unseen variant like "upi/swiggy/9876@ybl" down to "swiggy"; decision 8's identity fallback
# only covers a string that already equals a canonical outright, not one that merely
# contains it as a token. See provision_seed_merchant_dictionary.
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
    # Narrower than "uber" and therefore matched first (decision 2's longest-first sort), so
    # Uber Eats gets its OWN canonical instead of folding onto Uber rides. Without it a single
    # shared merchant memory decides the category for both and whichever the user confirms
    # first wins for the other — the same hazard "swiggy instamart" and "amazon prime video"
    # guard against. Two more 1-token patterns here have known cross-category sub-brands and
    # are deliberately left unsplit: "ola" (Ola Money, Ola Electric) and "airtel" (Airtel
    # Payments Bank). Both are wallet-load / transfer shaped, so any category would be
    # invented, and a wrong seeded suggestion still costs the user a correction (AGENTS.md
    # §Simplicity first).
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


def provision_default_categories(session: Session, user_id: UUID) -> None:
    """Insert the default spend + income 2-level category taxonomy for ``user_id``.

    Does NOT commit — the caller (register) commits the user + categories in one
    transaction. ``is_seeded=True`` so they read as app defaults, not
    user-created.
    """
    # 1. Insert spend and income parent categories first
    parent_rows: list[Category] = []
    for parent_name, color, _ in _DEFAULT_SPEND_TAXONOMY:
        parent_rows.append(
            Category(
                user_id=user_id,
                name=parent_name,
                kind="spend",
                is_seeded=True,
                color=color,
                parent_id=None,
            )
        )
    for parent_name, color, _ in _DEFAULT_INCOME_TAXONOMY:
        parent_rows.append(
            Category(
                user_id=user_id,
                name=parent_name,
                kind="income",
                is_seeded=True,
                color=color,
                parent_id=None,
            )
        )
    session.add_all(parent_rows)
    session.flush()

    spend_parents = {c.name: c.id for c in parent_rows if c.kind == "spend"}
    income_parents = {c.name: c.id for c in parent_rows if c.kind == "income"}

    # 2. Insert child subcategories linked to their respective parents
    child_rows: list[Category] = []
    for parent_name, _, subcategories in _DEFAULT_SPEND_TAXONOMY:
        pid = spend_parents.get(parent_name)
        for sub_name in subcategories:
            child_rows.append(
                Category(
                    user_id=user_id,
                    name=sub_name,
                    kind="spend",
                    is_seeded=True,
                    color=None,
                    parent_id=pid,
                )
            )
    for parent_name, _, subcategories in _DEFAULT_INCOME_TAXONOMY:
        pid = income_parents.get(parent_name)
        for sub_name in subcategories:
            child_rows.append(
                Category(
                    user_id=user_id,
                    name=sub_name,
                    kind="income",
                    is_seeded=True,
                    color=None,
                    parent_id=pid,
                )
            )
    session.add_all(child_rows)


def provision_seed_merchant_dictionary(session: Session, user_id: UUID) -> None:
    """Insert the seed merchant_alias + merchant_tag_map rows for ``user_id`` from
    ``_MERCHANT_DICTIONARY``, at ``hit_count=0`` / ``is_seeded=True`` (decision 4 of the
    merchant-alias arc — ``hit_count == 0`` is the seeded marker everywhere one is needed).

    Must run AFTER ``provision_default_categories`` has been FLUSHED — it needs concrete
    ``Category.id`` values. Production's ``SessionLocal`` is ``autoflush=False``
    (:mod:`app.core.db`), so the caller must flush explicitly; this function does not flush
    for the caller, since flushing here would also flush the *categories*, silently coupling
    two independently-testable functions' transaction boundaries.

    Writes a ``merchant_alias`` row for every entry, INCLUDING ``pattern == canonical``. An
    earlier draft of this function skipped those as "redundant with decision 8's identity
    fallback" — that was wrong: decision 8 only covers a merchant string that already equals
    a canonical outright, not a variant that merely CONTAINS the pattern as a token (the actual
    motivating case — ``upi/swiggy/9876@ybl``). Without a real ``pattern="swiggy"`` rule,
    :meth:`AliasResolver.canonical` never rewrites that string to ``"swiggy"``; there is no
    matching rule to fall back from, so it returns the whole variant unchanged. The identity
    row IS the mechanism, not a no-op.

    Skips — never merges, never bumps — any ``(user_id, pattern)`` already in
    ``merchant_alias`` and any ``(user_id, canonical, category_id)`` already in
    ``merchant_tag_map``. A seed must never disturb a learned or user-authored row: the demo
    user's :mod:`app.core.demo_data` teaches several of these exact brands already, at real
    hit_counts, via the ordinary :func:`app.services.tag_service.record_tag` path.

    A dictionary entry naming a category that isn't in this user's active provisioned set
    (renamed or archived before this ran — ``uq_categories_active_user_name`` is a *partial*
    index, so an archived category can share a name with an active one) is silently skipped
    for that entry only.

    Does NOT commit — the caller commits.
    """
    needed_names = {name for _, _, name in _MERCHANT_DICTIONARY}
    name_to_category_id = {
        name: category_id
        for name, category_id in session.execute(
            select(Category.name, Category.id).where(
                Category.user_id == user_id,
                Category.kind == "spend",
                Category.archived_at.is_(None),
                Category.name.in_(needed_names),
            )
        ).all()
    }
    existing_patterns = {
        pattern
        for pattern in session.scalars(
            select(MerchantAlias.pattern).where(MerchantAlias.user_id == user_id)
        )
    }
    existing_map_keys = {
        (merchant_normalized, category_id)
        for merchant_normalized, category_id in session.execute(
            select(MerchantTagMap.merchant_normalized, MerchantTagMap.category_id).where(
                MerchantTagMap.user_id == user_id
            )
        ).all()
    }

    alias_rows: list[MerchantAlias] = []
    map_rows: list[MerchantTagMap] = []
    seen_map_keys: set[tuple[str, int]] = set()
    for pattern, canonical, category_name in _MERCHANT_DICTIONARY:
        category_id = name_to_category_id.get(category_name)
        if category_id is None:
            continue
        if pattern not in existing_patterns:
            alias_rows.append(
                MerchantAlias(user_id=user_id, pattern=pattern, canonical=canonical, is_seeded=True)
            )
        map_key = (canonical, category_id)
        if map_key not in existing_map_keys and map_key not in seen_map_keys:
            seen_map_keys.add(map_key)
            map_rows.append(
                MerchantTagMap(
                    user_id=user_id,
                    merchant_normalized=canonical,
                    category_id=category_id,
                    hit_count=0,
                )
            )
    session.add_all(alias_rows)
    session.add_all(map_rows)
