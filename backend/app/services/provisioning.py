"""New-user provisioning (PRD §Users & access v2).

Categories are per-user reference data (unlike the global ``benchmark`` /
``fx_rate_quote`` tables). A freshly registered user therefore needs the same
default category set the seeded demo user has — otherwise their board has no
categories to tag against.

This is the **runtime source of truth** for that default set, and since
[ADR-0012](../../../docs/adr/0012-category-hierarchy.md) that set is a *two-level
taxonomy*, not a flat list: ``_DEFAULT_SPEND_TAXONOMY`` and
``_DEFAULT_INCOME_TAXONOMY``, each root carrying its hex and **every subcategory
carrying ``color=None``** so it inherits the parent's hue (PRD §F5). Each kind has
a root fallback "Other" (ADR-0012 rule 8). Migrations ``0034_seed_category_taxonomy``
and ``0039_fallback_roots_and_cashback_spend`` bring an existing user to the same
shape, and
``tests/test_migration_parity.py::test_provisioning_matches_migration_seed``
polices the two staying in agreement — including colour, which no other test
pins. Migrations stay frozen snapshots and never import this module; only
:mod:`app.services.auth_service` (register) and :mod:`app.services.guest_service` (guest
sandbox) call it.

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

# (parent_name, color, subcategories) — 2-level streamlined English taxonomy.
_DEFAULT_SPEND_TAXONOMY: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "Food & Dining",
        "#d95926",
        (
            "Groceries",
            "Dining Out & Delivery",
        ),
    ),
    (
        "Housing & Utilities",
        "#6c5cd6",
        (
            "Rent & Maintenance",
            "Bills & Utilities",
        ),
    ),
    (
        "Transport & Commute",
        "#2a78d6",
        (
            "Daily Commute",
            "Vehicle Service & Fuel",
        ),
    ),
    (
        "Shopping",
        "#d55181",
        (
            "Clothing & Personal",
            "Electronics & Home",
        ),
    ),
    (
        "Healthcare",
        "#e34948",
        (
            "Pharmacy & Medicines",
            "Doctor & Diagnostics",
        ),
    ),
    (
        "Entertainment & Subscriptions",
        "#b246c0",
        (
            "Streaming & Subscriptions",
            "Events & Outings",
        ),
    ),
    (
        "Investments & Savings",
        "#008300",
        (
            "Mutual Funds & SIPs",
            "Stocks & Securities",
        ),
    ),
    (
        "Loans & EMIs",
        "#c23b6b",
        ("Loan EMIs",),
    ),
    (
        "Travel & Vacations",
        "#0e97c4",
        (
            "Flights & Trains",
            "Hotels & Stays",
        ),
    ),
    # Purchase cashback is a rebate on spending, so it is spend-kind (ADR-0013).
    (
        "Cashback",
        "#c98500",
        (),
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
            "Investment Returns",
            "Rental Income",
        ),
    ),
    (
        "Other",
        "#94a3b8",
        (),
    ),
)

# (pattern, canonical, category_name) — the seed merchant dictionary (research §13.6).
# Every string is written ALREADY in normalize_merchant()-normalized form (lowercase,
# single-spaced) — test_seed_dictionary_entries_are_pre_normalized enforces that invariant
# so a typo can't silently produce a dead pattern. category_name must name one of this
# user's active spend categories (a _DEFAULT_SPEND_TAXONOMY parent or subcategory) or the
# entry inserts nothing (see provision_seed_merchant_dictionary). Two rows deliberately
# fold a spelling variant onto a shared canonical (BigBasket, Hotstar's Disney+ rename) —
# the fan-in this arc exists to prove. Every entry, including the ~94 where pattern ==
# canonical, gets its own merchant_alias row — that identity row is what lets
# AliasResolver.canonical() match an unseen variant like "upi/swiggy/9876@ybl" down to
# "swiggy"; decision 8's identity fallback
# only covers a string that already equals a canonical outright, not one that merely
# contains it as a token. See provision_seed_merchant_dictionary.
_MERCHANT_DICTIONARY: tuple[tuple[str, str, str], ...] = (
    # Food & Dining
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
    # Groceries & Quick Commerce
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
    # Transport & Commute
    ("uber", "uber", "Daily Commute"),
    # Narrower than "uber" and therefore matched first (decision 2's longest-first sort), so
    # Uber Eats gets its OWN canonical instead of folding onto Uber rides. Without it a single
    # shared merchant memory decides the category for both and whichever the user confirms
    # first wins for the other — the same hazard "swiggy instamart" and "amazon prime video"
    # guard against. Two more 1-token patterns here have known cross-category sub-brands and
    # are deliberately left unsplit: "ola" (Ola Money, Ola Electric) and "airtel" (Airtel
    # Payments Bank). Both are wallet-load / transfer shaped, so any category would be
    # invented, and a wrong seeded suggestion still costs the user a correction (AGENTS.md
    # §Simplicity first).
    ("uber eats", "uber eats", "Dining Out & Delivery"),
    ("ola", "ola", "Daily Commute"),
    ("rapido", "rapido", "Daily Commute"),
    ("namma yatri", "namma yatri", "Daily Commute"),
    ("yulu", "yulu", "Daily Commute"),
    # Streaming & Subscriptions
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
    # Travel & Vacations
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
    # Healthcare
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
    # Shopping
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
    # Bills & Utilities
    ("airtel", "airtel", "Bills & Utilities"),
    ("bsnl", "bsnl", "Bills & Utilities"),
    ("tata play", "tata play", "Bills & Utilities"),
    ("act fibernet", "act fibernet", "Bills & Utilities"),
    ("jiofiber", "jiofiber", "Bills & Utilities"),
    ("vodafone idea", "vodafone idea", "Bills & Utilities"),
    # Entertainment & Events
    ("bookmyshow", "bookmyshow", "Events & Outings"),
    ("inox", "inox", "Events & Outings"),
    ("cinepolis", "cinepolis", "Events & Outings"),
    ("pvr", "pvr", "Events & Outings"),
)

# Payment-gateway fallback (ADR-0011 rule 12: "the PSP list beside the seed dictionary").
# Code, not alias rows: applied only when no alias matches, and never seeded. The entries
# come from general knowledge of Indian UPI handles, not from measured statements; every
# entry has a literal example in tests/services/test_merchant_alias.py, and a test fails if
# one is added without it. Reopen when a bank parser emits a clean payee field (ADR-0011
# Consequences).
PAYMENT_GATEWAYS: tuple[str, ...] = ("razorpay", "cashfree", "payu", "billdesk", "ccavenue")
# (regex fully matching a VPA local part, issuer; None means use the handle's own PSP name)
UPI_QR_IDS: tuple[tuple[str, str | None], ...] = (
    (r"paytmqr[a-z0-9]{6,}", "paytm"),
    (r"q[0-9]{6,}", None),
    (r"bharatpe[._]?[0-9]{6,}", "bharatpe"),
)
# (UPI handle after the "@", the app that issues it)
PSP_HANDLES: tuple[tuple[str, str], ...] = (
    ("ybl", "phonepe"),
    ("ibl", "phonepe"),
    ("axl", "phonepe"),
    ("paytm", "paytm"),
    ("ptyes", "paytm"),
    ("ptaxis", "paytm"),
    ("pthdfc", "paytm"),
    ("ptsbi", "paytm"),
    ("okaxis", "google pay"),
    ("okhdfcbank", "google pay"),
    ("okicici", "google pay"),
    ("oksbi", "google pay"),
    ("apl", "amazon pay"),
    ("fbpe", "bharatpe"),
    ("upi", "bhim"),
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
    user's :mod:`app.core.demo_data` writes several of these exact brands' rule rows at real
    hit_counts directly (``demo_seed._write_demo_rule_rows``, raising a seed row's count).

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
