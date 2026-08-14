"""provisioning.py service tests — the seed merchant dictionary (Phase A5, merchant-alias arc).

Coverage this module owns: ``provision_seed_merchant_dictionary``'s write/skip logic and the
cold-start prefill proof (``test_cold_start_prefill_resolves_from_seed``). Category-seeding
(``provision_default_categories``) already has its own migration-parity coverage in
``tests/test_migration_parity.py``; this module treats it as a fixture dependency, not a subject.

Deliberately NOT tested here: "``record_tag`` on a seeded merchant bumps ``hit_count`` 0→1" —
Stage A writes stay on the raw key (``merchant.py``'s CHANGE HAZARD / the merchant-alias brief's
§Writes-stay-raw note), so that only holds in the degenerate raw-equals-canonical case this
seed's fold rows don't produce.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core import clock
from app.models import Category, MerchantAlias, MerchantTagMap, User
from app.services import auth_service
from app.services.merchant import normalize_merchant
from app.services.merchant_alias import load_alias_resolver
from app.services.provisioning import (
    _DEFAULT_INCOME_TAXONOMY,
    _DEFAULT_SPEND_TAXONOMY,
    _MERCHANT_DICTIONARY,
    provision_default_categories,
    provision_seed_merchant_dictionary,
)
from app.services.tag_service import prefetch_tag_map


def _category_id(session: Session, user_id: uuid.UUID, name: str) -> int:
    return session.scalars(
        select(Category.id).where(
            Category.user_id == user_id, Category.kind == "spend", Category.name == name
        )
    ).one()


def test_seed_dictionary_entries_are_pre_normalized() -> None:
    """Every pattern/canonical is already ``normalize_merchant()``-normalized — a typo (stray
    capital, double space) would otherwise produce a pattern that can never match anything,
    silently, since nothing else calls ``normalize_merchant()`` on these literals."""
    for pattern, canonical, _ in _MERCHANT_DICTIONARY:
        assert normalize_merchant(pattern) == pattern
        assert normalize_merchant(canonical) == canonical


def test_provision_seed_merchant_dictionary_creates_aliases_and_map_rows(
    session: Session, user: User
) -> None:
    provision_default_categories(session, user.id)
    session.flush()
    provision_seed_merchant_dictionary(session, user.id)
    session.flush()

    aliases = list(session.scalars(select(MerchantAlias).where(MerchantAlias.user_id == user.id)))
    maps = list(session.scalars(select(MerchantTagMap).where(MerchantTagMap.user_id == user.id)))

    distinct_canonicals = {canonical for _, canonical, _ in _MERCHANT_DICTIONARY}
    assert len(maps) == len(distinct_canonicals)
    assert all(m.hit_count == 0 for m in maps)

    # Every entry gets an alias row, INCLUDING pattern == canonical ones (e.g. "swiggy" —
    # this is the mechanism that resolves an unseen variant like "upi/swiggy/9876@ybl" to the
    # canonical; decision 8's identity fallback only covers a merchant string that already
    # equals a canonical outright, not one that merely contains it as a token).
    all_patterns = {p for p, _, _ in _MERCHANT_DICTIONARY}
    assert len(aliases) == len(all_patterns)
    assert all(a.is_seeded for a in aliases)
    assert {a.pattern for a in aliases} == all_patterns
    assert any(a.pattern == "swiggy" and a.canonical == "swiggy" for a in aliases)


def test_seed_skips_existing_map_row(session: Session, user: User) -> None:
    """A pre-existing learned row (hit_count=1) at the seed's exact key must survive untouched —
    never merged, never bumped, never overwritten with the seed's hit_count=0."""
    provision_default_categories(session, user.id)
    session.flush()
    food_id = _category_id(session, user.id, "Online Food Delivery")
    learned = MerchantTagMap(
        user_id=user.id, merchant_normalized="swiggy", category_id=food_id, hit_count=1
    )
    session.add(learned)
    session.flush()

    provision_seed_merchant_dictionary(session, user.id)
    session.flush()

    rows = list(
        session.scalars(
            select(MerchantTagMap).where(
                MerchantTagMap.user_id == user.id,
                MerchantTagMap.merchant_normalized == "swiggy",
                MerchantTagMap.category_id == food_id,
            )
        )
    )
    assert len(rows) == 1
    assert rows[0].id == learned.id
    assert rows[0].hit_count == 1


def test_seed_skips_existing_pinned_row(session: Session, user: User) -> None:
    provision_default_categories(session, user.id)
    session.flush()
    food_id = _category_id(session, user.id, "Online Food Delivery")
    pinned = MerchantTagMap(
        user_id=user.id,
        merchant_normalized="swiggy",
        category_id=food_id,
        hit_count=1,
        pinned=True,
    )
    session.add(pinned)
    session.flush()

    provision_seed_merchant_dictionary(session, user.id)
    session.flush()

    rows = list(
        session.scalars(
            select(MerchantTagMap).where(
                MerchantTagMap.user_id == user.id,
                MerchantTagMap.merchant_normalized == "swiggy",
                MerchantTagMap.category_id == food_id,
            )
        )
    )
    assert len(rows) == 1
    assert rows[0].pinned is True
    assert rows[0].hit_count == 1


def test_seed_skips_archived_category(session: Session, user: User) -> None:
    """``uq_categories_active_user_name`` is a partial index — an archived category is silently
    skipped for its dictionary entries (trap 3), without disturbing any other category."""
    provision_default_categories(session, user.id)
    session.flush()
    health = session.scalars(
        select(Category).where(
            Category.user_id == user.id, Category.kind == "spend", Category.name == "Health"
        )
    ).one()
    health.archived_at = clock.naive_utcnow()
    session.flush()

    provision_seed_merchant_dictionary(session, user.id)
    session.flush()

    health_canonicals = {c for _, c, name in _MERCHANT_DICTIONARY if name == "Health"}
    other_canonicals = {c for _, c, name in _MERCHANT_DICTIONARY if name != "Health"}
    maps = list(session.scalars(select(MerchantTagMap).where(MerchantTagMap.user_id == user.id)))
    assert not any(m.merchant_normalized in health_canonicals for m in maps)
    assert {m.merchant_normalized for m in maps} == other_canonicals


def test_cold_start_prefill_resolves_from_seed(engine: Engine) -> None:
    """Direct proof of PRD §Verification's "a first import ... prefills categories from the
    seeded dictionary." Uses an explicit autoflush=False session (mirroring production's
    SessionLocal, app/core/db.py) rather than a bare Session(engine) — autoflush=True would mask
    a regression if register_user's second flush() were ever removed, since the pending
    Category rows would then be visible to the seed's SELECT by accident instead of by design.
    """
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as s:
        u = auth_service.register_user(
            s, email="cold-start@example.com", password="correct horse battery"
        )

        resolver = load_alias_resolver(s, user_id=u.id)
        canon = resolver.canonical(normalize_merchant("upi/swiggy/9876@ybl"))
        assert canon == "swiggy"

        tag_map = prefetch_tag_map(s, user_id=u.id, resolver=resolver)
        food_id = _category_id(s, u.id, "Online Food Delivery")
        assert tag_map[canon] == food_id


def test_seed_does_not_fold_uber_eats_onto_uber(engine: Engine) -> None:
    """A sub-brand in a different category must not share the parent brand's key.

    ``uber -> uber -> Transport`` alone caught ``uber eats …`` too, so ONE
    merchant memory decided the category for food delivery and rides both, and
    whichever the user confirmed first won for the other. The narrower
    ``uber eats`` pattern sorts ahead of it (decision 2), so each keeps its own
    canonical — and its own independently-learnable category.
    """
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as s:
        u = auth_service.register_user(
            s, email="uber-split@example.com", password="correct horse battery"
        )
        resolver = load_alias_resolver(s, user_id=u.id)

        eats = resolver.canonical(normalize_merchant("UBER EATS BLR 12345"))
        ride = resolver.canonical(normalize_merchant("UBER TRIP 998"))
        assert eats == "uber eats"
        assert ride == "uber"

        tag_map = prefetch_tag_map(s, user_id=u.id, resolver=resolver)
        assert tag_map[eats] == _category_id(s, u.id, "Online Food Delivery")
        assert tag_map[ride] == _category_id(s, u.id, "Ride-Hailing & Taxis")


def test_provision_default_categories_creates_two_level_tree(session: Session, user: User) -> None:
    """Verify that provision_default_categories constructs a valid 2-level hierarchy
    with 10 parents (9 spend + 1 income) and the exact subcategory set each names —
    derived from the taxonomy constants themselves (ADR-0012: "do not enumerate the
    taxonomy anywhere but provisioning.py"), not restated here as a literal count."""
    provision_default_categories(session, user.id)
    session.flush()

    cats = list(session.scalars(select(Category).where(Category.user_id == user.id)))
    parents = [c for c in cats if c.parent_id is None]
    children = [c for c in cats if c.parent_id is not None]

    expected_child_names = {sub for _, _, subs in _DEFAULT_SPEND_TAXONOMY for sub in subs} | {
        sub for _, _, subs in _DEFAULT_INCOME_TAXONOMY for sub in subs
    }

    assert len(parents) == 10
    assert {c.name for c in children} == expected_child_names
    assert len(children) == len(expected_child_names)

    # Ensure every child has a valid parent of matching kind
    parent_ids = {p.id: p for p in parents}
    for child in children:
        assert child.parent_id in parent_ids
        parent = parent_ids[child.parent_id]
        assert child.kind == parent.kind
