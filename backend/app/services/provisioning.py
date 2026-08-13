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

# (parent_name, color, subcategories) — 2-level English taxonomy for Indian personal finance.
_DEFAULT_SPEND_TAXONOMY: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "Food & Dining",
        "#d95926",
        (
            "Food",
            "Groceries",
            "Online Food Delivery",
            "Restaurants & Cafes",
            "Street Food & Snacks",
            "Tea & Beverages",
        ),
    ),
    (
        "Household & Living",
        "#6c5cd6",
        (
            "Rent",
            "Rent & Maintenance",
            "Quick Commerce",
            "Domestic Staff / Household Help",
            "Home Improvements",
            "Furniture & Appliances",
        ),
    ),
    (
        "Bills & Utilities",
        "#0e97c4",
        (
            "Utilities",
            "Mobile & Wi-Fi Recharge",
            "Electricity",
            "LPG / Piped Gas",
            "DTH / Cable",
            "Water & Municipal Bills",
        ),
    ),
    (
        "Commute & Transportation",
        "#2a78d6",
        (
            "Transport",
            "Travel",
            "Fuel / Petrol",
            "Metro & Public Transit",
            "Auto / Cab / Ride-Hailing",
            "FASTag & Tolls",
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
            "OTT & Subscriptions",
        ),
    ),
    (
        "Family & Social",
        "#b246c0",
        (
            "Gifts & Festival Celebrations",
            "Family Support / Remittance",
            "Education & Tuition",
            "Charity & Donations",
        ),
    ),
    (
        "Savings & Investments",
        "#008300",
        (
            "Investment",
            "Mutual Funds / SIP",
            "Gold",
            "Stocks & Securities",
            "Term & Health Insurance",
            "Fixed Deposits / Savings",
        ),
    ),
    (
        "Loans & Settlements",
        "#c23b6b",
        (
            "EMI",
            "Credit Card Bill",
            "Personal & Home Loan EMI",
            "Friend & Group Splits",
        ),
    ),
    (
        "Other",
        "#94a3b8",
        (
            "Other",
        ),
    ),
)

_DEFAULT_INCOME_TAXONOMY: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "Income",
        "#008300",
        (
            "Salary",
            "Freelancing",
            "Freelancing / Consulting",
            "Cashback",
            "Investment Returns",
            "Cashback & Rewards",
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
    # Narrower than "uber" and therefore matched first (decision 2's longest-first sort), so
    # Uber Eats gets its OWN canonical instead of folding onto Uber rides. Without it a single
    # shared merchant memory decides the category for both and whichever the user confirms
    # first wins for the other — the same hazard "swiggy instamart" and "amazon prime video"
    # guard against. Two more 1-token patterns here have known cross-category sub-brands and
    # are deliberately left unsplit: "ola" (Ola Money, Ola Electric) and "airtel" (Airtel
    # Payments Bank). Both are wallet-load / transfer shaped, so any category would be
    # invented, and a wrong seeded suggestion still costs the user a correction (AGENTS.md
    # §Simplicity first).
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


def provision_default_categories(session: Session, user_id: UUID) -> None:
    """Insert the default spend + income categories for ``user_id``.

    Does NOT commit — the caller (register) commits the user + categories in one
    transaction. ``is_seeded=True`` so they read as app defaults, not
    user-created.
    """
    rows = [
        Category(user_id=user_id, name=name, kind="spend", is_seeded=True, color=color)
        for name, color in _DEFAULT_SPEND_CATEGORIES
    ] + [
        Category(user_id=user_id, name=name, kind="income", is_seeded=True, color=color)
        for name, color in _DEFAULT_INCOME_CATEGORIES
    ]
    session.add_all(rows)


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
