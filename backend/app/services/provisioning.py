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
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from app.models import Category

# (name, color) — the active default spend categories. Colors are drawn from the
# validated CATEGORY_PALETTE (frontend lib/categories.ts) and mirror migration
# 0018; each spend seed gets a distinct hue, "Other" the neutral grey.
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
# (name, color) — the active default income categories. Colors mirror 0018;
# income "Other" reuses the neutral grey.
_DEFAULT_INCOME_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("Salary", "#008300"),
    ("Freelancing", "#2a78d6"),
    ("Cashback", "#c98500"),
    ("Other", "#94a3b8"),
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
