"""seed merchant dictionary (Stage A, Phase A5 -- merchant-alias layer)

Revision ID: 0032_seed_merchant_dictionary
Revises: 0031_add_merchant_alias
Create Date: 2026-08-12

Backfills every EXISTING user (not just v1_user_id -- multi-user landed in 0017, after
0003/0008/0012's category seeds were written, so this is the first data-seed migration that
loops over the whole `users` table rather than one fixed row) with ~96 seed
`merchant_alias` + `merchant_tag_map` rows, at `is_seeded=True` / `hit_count=0` (decision 4 of
the merchant-alias arc -- `hit_count == 0` is the seeded marker everywhere one is needed). New
registrants get the identical set from
`app.services.provisioning.provision_seed_merchant_dictionary`; this migration exists only for
users who registered before that landed.

Cannot import `app.*` -- no migration in this repo does (a migration must be frozen against its
own revision, or a future edit to provisioning.py silently rewrites already-applied history).
The `_MERCHANT_DICTIONARY` tuple below is therefore a duplicate, frozen-snapshot literal of
`app.services.provisioning._MERCHANT_DICTIONARY` as it stood at this revision --
`tests/test_migration_parity.py::test_merchant_dictionary_matches_migration_seed` polices the two
staying in agreement, the same way `test_provisioning_matches_migration_seed` already does for
categories.

THE COLLISION THIS MIGRATION MUST SURVIVE: `app/core/demo_data.py` teaches the demo user
(`swiggy`, `zomato`, `uber`, `netflix`, `spotify`, `apollo pharmacy`, `makemytrip`, `big basket`,
plus a few more this dictionary also seeds -- `croma`, `myntra`, `bookmyshow`, `airtel`) via the
ordinary `record_tag` path, at real hit_counts -- NOT via a migration. Any dev DB or Docker
deploy that has ever run `app.services.demo_seed.seed_demo_data` already carries those rows, so a
naive `INSERT` would collide with `uq_merchant_tag_map_user_merchant_category` and abort
`alembic upgrade head`. Every insert below goes through a dialect-aware `ON CONFLICT DO NOTHING`
keyed on the real unique-constraint columns -- skip, never merge, never bump. A seed must never
disturb a learned or user-authored row.

Every dictionary entry gets a `merchant_alias` row, INCLUDING the ~94 where `pattern ==
canonical` -- an earlier draft skipped those as "redundant with decision 8's identity fallback."
That was wrong: decision 8 only covers a merchant string that already equals a canonical
outright, not a variant that merely CONTAINS the pattern as a token (the actual motivating case
-- `upi/swiggy/9876@ybl`). Without a real `pattern="swiggy"` rule, `AliasResolver.canonical()`
never rewrites that variant to `"swiggy"` -- there is no matching rule to fall back from, so it
returns the string unchanged. The identity row IS the mechanism. Every entry also gets a
`merchant_tag_map` row, deduplicated by `(canonical, category_id)` since two patterns can share
one canonical (`bigbasket`/`big basket` -> `big basket`; `disney hotstar`/`hotstar` -> `hotstar`).

A dictionary entry naming a category outside this user's active provisioned set (renamed or
archived before this ran -- `uq_categories_active_user_name` is a *partial* index, so an archived
category can share a name with an active one) is silently skipped for that entry, that user, only.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from alembic import op

revision: str = "0032_seed_merchant_dictionary"
down_revision: str | Sequence[str] | None = "0031_add_merchant_alias"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen duplicate of app.services.provisioning._MERCHANT_DICTIONARY as of this revision --
# see this module's docstring for why a migration can't import it instead. Keep the two in sync
# by hand; test_merchant_dictionary_matches_migration_seed fails loudly if they drift.
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
    ("uber eats", "uber eats", "Food"),  # narrower than "uber", so Uber Eats keeps its own key
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

# Lightweight table() constructs, frozen against the DDL as of this revision -- not the ORM
# models, per this module's docstring.
_users = sa.table("users", sa.column("id", sa.Uuid()))
_categories = sa.table(
    "categories",
    sa.column("id", sa.Integer()),
    sa.column("user_id", sa.Uuid()),
    sa.column("name", sa.String()),
    sa.column("kind", sa.String()),
    sa.column("archived_at", sa.DateTime()),
)
_merchant_alias = sa.table(
    "merchant_alias",
    sa.column("user_id", sa.Uuid()),
    sa.column("pattern", sa.String()),
    sa.column("canonical", sa.String()),
    sa.column("is_seeded", sa.Boolean()),
)
_merchant_tag_map = sa.table(
    "merchant_tag_map",
    sa.column("user_id", sa.Uuid()),
    sa.column("merchant_normalized", sa.String()),
    sa.column("category_id", sa.Integer()),
    sa.column("hit_count", sa.Integer()),
)


def upgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"
    needed_names = {name for _, _, name in _MERCHANT_DICTIONARY}

    def _upsert_skip(
        table: sa.TableClause, rows: list[dict[str, object]], index_elements: list[str]
    ) -> None:
        if not rows:
            return
        insert = sqlite_insert if is_sqlite else pg_insert
        stmt = insert(table).on_conflict_do_nothing(index_elements=index_elements)
        bind.execute(stmt, rows)

    user_ids = [row.id for row in bind.execute(sa.select(_users.c.id)).all()]

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
        alias_rows: list[dict[str, object]] = []
        map_rows: list[dict[str, object]] = []
        seen: set[tuple[str, int]] = set()
        for pattern, canonical, category_name in _MERCHANT_DICTIONARY:
            category_id = name_to_id.get(category_name)
            if category_id is None:
                continue
            alias_rows.append(
                {
                    "user_id": user_id,
                    "pattern": pattern,
                    "canonical": canonical,
                    "is_seeded": True,
                }
            )
            key = (canonical, category_id)
            if key not in seen:
                seen.add(key)
                map_rows.append(
                    {
                        "user_id": user_id,
                        "merchant_normalized": canonical,
                        "category_id": category_id,
                        "hit_count": 0,
                    }
                )
        _upsert_skip(_merchant_alias, alias_rows, ["user_id", "pattern"])
        _upsert_skip(_merchant_tag_map, map_rows, ["user_id", "merchant_normalized", "category_id"])


def downgrade() -> None:
    # is_seeded=True on merchant_alias is set by NOTHING else in the codebase -- Phase A4's CRUD
    # writes is_seeded=False on create and CLEARS it on any user PATCH -- so it is an exclusive
    # marker for rows this migration (or the runtime seed it mirrors) inserted.
    #
    # hit_count=0 on merchant_tag_map is NOT exclusive on its own. record_tag/pin_tag never
    # INSERT at hit_count=0 (the column default is 1), and a row that graduated via record_tag
    # no longer matches -- but pin_tag deliberately does not bump hit_count on an EXISTING row
    # (a pin is an assertion, not an observed decision), so pinning a seed row leaves it at
    # hit_count=0 with pinned=True. That row is user-authored data; `AND pinned = FALSE` is what
    # keeps this delete from silently discarding it.
    op.execute(sa.text("DELETE FROM merchant_alias WHERE is_seeded = TRUE"))
    op.execute(sa.text("DELETE FROM merchant_tag_map WHERE hit_count = 0 AND pinned = FALSE"))
