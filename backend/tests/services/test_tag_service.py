"""Unit tests for :mod:`app.services.tag_service`.

Covers the two public callables:

* :func:`prefetch_tag_map` — single SELECT, returns the winning category
  per merchant (highest hit_count → most-recent last_used → highest id).
* :func:`record_tag` — INSERT or hit_count+1 upsert with race recovery.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.models import Category, MerchantTagMap, User
from app.services import tag_service
from app.services.tag_service import (
    pin_tag,
    prefetch_tag_map,
    record_tag,
    set_tag_pinned,
    should_learn_tag,
)


def _make_category(session: Session, user_id: uuid.UUID, name: str) -> Category:
    c = Category(user_id=user_id, name=name)
    session.add(c)
    session.flush()
    return c


# ---------- prefetch_tag_map ----------------------------------------------


def test_prefetch_returns_empty_dict_for_empty_map(session: Session, user: User) -> None:
    assert prefetch_tag_map(session, user_id=user.id) == {}


def test_prefetch_picks_highest_hit_count_per_merchant(session: Session, user: User) -> None:
    food = _make_category(session, user.id, "Food")
    subs = _make_category(session, user.id, "Subscriptions")
    session.add_all(
        [
            MerchantTagMap(
                user_id=user.id,
                merchant_normalized="swiggy",
                category_id=food.id,
                hit_count=5,
            ),
            MerchantTagMap(
                user_id=user.id,
                merchant_normalized="swiggy",
                category_id=subs.id,
                hit_count=2,
            ),
        ]
    )
    session.flush()

    result = prefetch_tag_map(session, user_id=user.id)
    assert result == {"swiggy": food.id}


def test_prefetch_tiebreaks_by_last_used_then_id_desc(session: Session, user: User) -> None:
    """Same hit_count + same second-precision last_used → id DESC decides."""
    food = _make_category(session, user.id, "Food")
    subs = _make_category(session, user.id, "Subscriptions")
    same_instant = datetime.now(UTC).replace(microsecond=0)
    session.add_all(
        [
            MerchantTagMap(
                user_id=user.id,
                merchant_normalized="zomato",
                category_id=food.id,
                hit_count=3,
                last_used=same_instant,
            ),
            MerchantTagMap(
                user_id=user.id,
                merchant_normalized="zomato",
                category_id=subs.id,
                hit_count=3,
                last_used=same_instant,
            ),
        ]
    )
    session.flush()

    # Sanity: the second insert has the larger id.
    rows = session.scalars(
        select(MerchantTagMap)
        .where(MerchantTagMap.merchant_normalized == "zomato")
        .order_by(MerchantTagMap.id.desc())
    ).all()
    winner_category_id = rows[0].category_id

    result = prefetch_tag_map(session, user_id=user.id)
    assert result["zomato"] == winner_category_id


def test_prefetch_user_scoped(session: Session, user: User) -> None:
    other = User(id=uuid.uuid4())
    session.add(other)
    session.flush()

    food = _make_category(session, user.id, "Food")
    other_food = _make_category(session, other.id, "Food")
    session.add_all(
        [
            MerchantTagMap(
                user_id=other.id,
                merchant_normalized="amazon",
                category_id=other_food.id,
                hit_count=10,
            ),
            MerchantTagMap(
                user_id=user.id,
                merchant_normalized="amazon",
                category_id=food.id,
                hit_count=1,
            ),
        ]
    )
    session.flush()

    result = prefetch_tag_map(session, user_id=user.id)
    assert result == {"amazon": food.id}


def test_prefetch_skips_archived_category(session: Session, user: User) -> None:
    """The load-bearing guard: a tag-map row pointing at an archived category is
    invisible to the prefetch. Not defence-in-depth — category DELETE is a pure
    ``archived_at`` UPDATE that deliberately KEEPS the tag-map row, so this JOIN
    filter is the only thing stopping F3 prefilling an archived bucket.
    """
    archived = _make_category(session, user.id, "OldStuff")
    archived.archived_at = datetime.now(UTC)
    session.add(
        MerchantTagMap(
            user_id=user.id,
            merchant_normalized="orphan",
            category_id=archived.id,
            hit_count=5,
        )
    )
    session.flush()

    result = prefetch_tag_map(session, user_id=user.id)
    assert result == {}


def test_prefetch_ignores_a_map_row_pointing_at_another_users_category(
    session: Session, user: User
) -> None:
    """The join restates ``Category.user_id``, not just ``MerchantTagMap.user_id``.

    Storable precisely because ``merchant_tag_map.category_id`` is a plain
    ``ForeignKey("categories.id")`` — unlike the sibling ``merchant_label_map``, which
    carries ADR-0002's composite same-user FK and so is rejected by the DB rather than
    filtered on read. That divergence is why ``prefetch_label_map`` needs no equivalent
    predicate and this one does.

    Defence-in-depth, NOT a live leak: every write path into ``category_id`` validates
    ownership first (``create_category_rule`` via ``validate_category_ids``,
    ``patch_category_rule`` touches only ``pinned``, and ``record_tag`` / ``pin_tag``
    are only reached with an id already through ``_assert_category_id_or_422``). What
    makes it worth closing anyway is that this is the one merchant-memory read whose
    result gets WRITTEN onto imported rows as ``auto_category_id``.
    """
    other = User(id=uuid.uuid4())
    session.add(other)
    session.flush()
    theirs = _make_category(session, other.id, "TheirGroceries")
    session.add(
        MerchantTagMap(
            user_id=user.id,  # our row...
            merchant_normalized="bigbasket",
            category_id=theirs.id,  # ...pointing at their category
            hit_count=9,
        )
    )
    session.flush()

    assert prefetch_tag_map(session, user_id=user.id) == {}


# ---------- record_tag ----------------------------------------------------


def test_record_tag_inserts_when_missing(session: Session, user: User) -> None:
    food = _make_category(session, user.id, "Food")

    record_tag(
        session,
        user_id=user.id,
        merchant_normalized="swiggy",
        category_id=food.id,
    )
    session.flush()

    row = session.scalar(
        select(MerchantTagMap).where(MerchantTagMap.merchant_normalized == "swiggy")
    )
    assert row is not None
    assert row.category_id == food.id
    assert row.hit_count == 1


def test_record_tag_increments_existing(session: Session, user: User) -> None:
    food = _make_category(session, user.id, "Food")
    old_last_used = datetime.now(UTC) - timedelta(days=30)
    session.add(
        MerchantTagMap(
            user_id=user.id,
            merchant_normalized="swiggy",
            category_id=food.id,
            hit_count=1,
            last_used=old_last_used,
        )
    )
    session.flush()

    record_tag(
        session,
        user_id=user.id,
        merchant_normalized="swiggy",
        category_id=food.id,
    )
    session.flush()

    row = session.scalar(
        select(MerchantTagMap).where(MerchantTagMap.merchant_normalized == "swiggy")
    )
    assert row is not None
    assert row.hit_count == 2
    # last_used advanced — strip tz for cross-dialect compare (SQLite stores naive).
    assert row.last_used.replace(tzinfo=None) > old_last_used.replace(tzinfo=None)


def test_record_tag_retag_leaves_old_row_alone(session: Session, user: User) -> None:
    food = _make_category(session, user.id, "Food")
    subs = _make_category(session, user.id, "Subscriptions")
    session.add(
        MerchantTagMap(
            user_id=user.id,
            merchant_normalized="netflix",
            category_id=food.id,
            hit_count=3,
        )
    )
    session.flush()

    record_tag(
        session,
        user_id=user.id,
        merchant_normalized="netflix",
        category_id=subs.id,
    )
    session.flush()

    food_row = session.scalar(
        select(MerchantTagMap).where(
            MerchantTagMap.merchant_normalized == "netflix",
            MerchantTagMap.category_id == food.id,
        )
    )
    subs_row = session.scalar(
        select(MerchantTagMap).where(
            MerchantTagMap.merchant_normalized == "netflix",
            MerchantTagMap.category_id == subs.id,
        )
    )
    assert food_row is not None
    assert food_row.hit_count == 3  # untouched
    assert subs_row is not None
    assert subs_row.hit_count == 1  # freshly inserted


def test_record_tag_noop_on_empty_merchant(session: Session, user: User) -> None:
    food = _make_category(session, user.id, "Food")

    record_tag(
        session,
        user_id=user.id,
        merchant_normalized="",
        category_id=food.id,
    )
    session.flush()

    assert session.scalars(select(MerchantTagMap)).all() == []


def test_record_tag_handles_concurrent_insert_race(
    engine: Engine,
    session: Session,
    user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loser of the SELECT-then-INSERT race falls through to UPDATE.

    Simulating a real two-connection race against SQLite-with-StaticPool is
    awkward — the writer lock serialises everything so the first SELECT
    always sees the parallel session's committed row, taking the
    "row exists, increment" path and skipping the IntegrityError branch.

    Instead force the race window deterministically: pre-insert the row via
    a parallel session, then patch ``session.scalar`` to return ``None`` on
    the *first* call inside :func:`record_tag` (the existence probe). The
    subsequent INSERT trips the uniqueness constraint, the except branch
    rolls back, re-SELECTs (this time unpatched), and bumps ``hit_count``.

    This is the v2 Postgres scenario: two workers, two connections, both
    SELECT-miss, one INSERTs and commits, the other INSERTs and IntegrityErrors.
    """
    food = _make_category(session, user.id, "Food")
    session.commit()

    other_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    other = other_factory()
    try:
        other.add(
            MerchantTagMap(
                user_id=user.id,
                merchant_normalized="racy",
                category_id=food.id,
                hit_count=1,
            )
        )
        other.commit()
    finally:
        other.close()

    real_scalar = session.scalar
    miss_remaining = [1]

    def stubbed_scalar(*args: object, **kwargs: object) -> object:
        if miss_remaining[0] > 0:
            miss_remaining[0] -= 1
            return None
        return real_scalar(*args, **kwargs)

    monkeypatch.setattr(session, "scalar", stubbed_scalar)

    record_tag(
        session,
        user_id=user.id,
        merchant_normalized="racy",
        category_id=food.id,
    )
    session.commit()

    rows = session.scalars(
        select(MerchantTagMap).where(MerchantTagMap.merchant_normalized == "racy")
    ).all()
    assert len(rows) == 1
    assert rows[0].hit_count == 2


def test_record_tag_race_preserves_caller_pending_state(
    engine: Engine,
    session: Session,
    user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for the original silent-data-loss bug.

    Before the savepoint fix, ``record_tag`` called ``session.rollback()``
    on IntegrityError — which discards the *entire* parent transaction
    including any pending mutations the caller had staged. The PATCH route's
    ``setattr(txn, "category_id", new_id)`` would get reverted, the route
    would commit only the tag-map bump, and the user's category change
    would silently disappear.

    Test shape: stage a pending mutation in the session, force the race
    path inside ``record_tag``, then assert the staged mutation survives
    the commit. Mirrors the PATCH route's session usage exactly — same
    session, ``setattr`` before record_tag, single commit after.
    """
    food = _make_category(session, user.id, "Food")
    session.commit()

    # Pre-insert via parallel session so the IntegrityError actually fires.
    other_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    other = other_factory()
    try:
        other.add(
            MerchantTagMap(
                user_id=user.id,
                merchant_normalized="racy",
                category_id=food.id,
                hit_count=1,
            )
        )
        other.commit()
    finally:
        other.close()

    # Stage caller-side pending state: a brand-new Category mutation. The
    # PATCH route's setattr(txn, "category_id", ...) is the analogous case;
    # using a fresh Category here keeps the test free of Transaction setup.
    pending = Category(user_id=user.id, name="PendingMutation")
    session.add(pending)
    # NOTE: no flush — leave it pending so any rollback would discard it.

    # Force the race window: stub session.scalar so record_tag's existence
    # probe misses, triggering the INSERT-then-IntegrityError fallback.
    real_scalar = session.scalar
    miss_remaining = [1]

    def stubbed_scalar(*args: object, **kwargs: object) -> object:
        if miss_remaining[0] > 0:
            miss_remaining[0] -= 1
            return None
        return real_scalar(*args, **kwargs)

    monkeypatch.setattr(session, "scalar", stubbed_scalar)

    record_tag(
        session,
        user_id=user.id,
        merchant_normalized="racy",
        category_id=food.id,
    )
    session.commit()

    # The savepoint contract: the failed INSERT rolled back, but the
    # caller's pending Category survived and was committed.
    assert pending.id is not None
    persisted = session.scalar(select(Category).where(Category.name == "PendingMutation"))
    assert persisted is not None

    # And the tag-map row bumped correctly.
    rows = session.scalars(
        select(MerchantTagMap).where(MerchantTagMap.merchant_normalized == "racy")
    ).all()
    assert len(rows) == 1
    assert rows[0].hit_count == 2


def test_record_tag_repeated_bumps_accumulate(session: Session, user: User) -> None:
    """Two same-triple bumps on a pre-existing rule add +2, not +1.

    Locks the read-modify-write increment (``hit_count += 1``) against a
    regression to a deferred SQL-expression form, which under ``autoflush=False``
    would collapse repeated same-instance bumps to a single +1.
    """
    food = _make_category(session, user.id, "Food")
    session.add(
        MerchantTagMap(
            user_id=user.id, merchant_normalized="swiggy", category_id=food.id, hit_count=5
        )
    )
    session.flush()

    for _ in range(2):
        record_tag(session, user_id=user.id, merchant_normalized="swiggy", category_id=food.id)
    session.flush()

    row = session.scalar(
        select(MerchantTagMap).where(MerchantTagMap.merchant_normalized == "swiggy")
    )
    assert row is not None
    assert row.hit_count == 7  # 5 + 2


# ---------- should_learn_tag ----------------------------------------------


@pytest.mark.parametrize(
    ("transaction_type", "merchant", "expected"),
    [
        ("spend", "swiggy", True),
        ("refund", "swiggy", True),
        ("income", "swiggy", False),
        ("transfer", "swiggy", False),
        ("spend", "", False),
    ],
)
def test_should_learn_tag(transaction_type: str, merchant: str, expected: bool) -> None:
    assert (
        should_learn_tag(transaction_type=transaction_type, merchant_normalized=merchant)
        is expected
    )


# ---------- record_tag: narrowed IntegrityError handling ------------------


def _preinsert_racy(engine: Engine, user: User, category_id: int) -> None:
    """Commit a (user, "racy", category_id) rule via a parallel session so the
    row exists and the next INSERT of the same triple trips the unique constraint."""
    other_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    other = other_factory()
    try:
        other.add(
            MerchantTagMap(
                user_id=user.id, merchant_normalized="racy", category_id=category_id, hit_count=1
            )
        )
        other.commit()
    finally:
        other.close()


def _stub_scalar_misses(session: Session, monkeypatch: pytest.MonkeyPatch, misses: int) -> None:
    """Force ``session.scalar`` to return None for its next ``misses`` calls
    (record_tag's existence probe, and optionally its post-conflict refetch)."""
    real_scalar = session.scalar
    remaining = [misses]

    def stubbed(*args: object, **kwargs: object) -> object:
        if remaining[0] > 0:
            remaining[0] -= 1
            return None
        return real_scalar(*args, **kwargs)

    monkeypatch.setattr(session, "scalar", stubbed)


def test_record_tag_reraises_unexpected_integrity_error(
    engine: Engine,
    session: Session,
    user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-(user,merchant,category) IntegrityError propagates, not swallowed.

    The over-broad ``except IntegrityError`` previously dropped the user's tag
    silently on ANY integrity failure. We simulate an "unexpected" error by
    forcing the conflict predicate to reject the (real, uq) error; record_tag
    must re-raise rather than no-op.
    """
    food = _make_category(session, user.id, "Food")
    session.commit()
    _preinsert_racy(engine, user, food.id)

    _stub_scalar_misses(session, monkeypatch, 1)  # probe misses → INSERT fires
    monkeypatch.setattr(tag_service, "_is_merchant_tag_conflict", lambda orig: False)

    with pytest.raises(IntegrityError):
        record_tag(session, user_id=user.id, merchant_normalized="racy", category_id=food.id)


def test_record_tag_conflict_no_winner_returns_without_raising(
    engine: Engine,
    session: Session,
    user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the post-conflict refetch finds no winner, record_tag logs and returns
    (no bump, no crash) instead of dereferencing None."""
    food = _make_category(session, user.id, "Food")
    session.commit()
    _preinsert_racy(engine, user, food.id)

    # Miss on BOTH the existence probe and the post-conflict refetch.
    _stub_scalar_misses(session, monkeypatch, 2)

    # Returns cleanly (no exception); the pre-existing rule is left untouched.
    record_tag(session, user_id=user.id, merchant_normalized="racy", category_id=food.id)

    row = session.scalar(select(MerchantTagMap).where(MerchantTagMap.merchant_normalized == "racy"))
    assert row is not None
    assert row.hit_count == 1  # unchanged — no bump applied


# ---------- pin_tag / set_tag_pinned (F3 rule authoring) ------------------


def test_prefetch_all_unpinned_unchanged(session: Session, user: User) -> None:
    """Regression: with no pinned rows the ``pinned DESC`` tiebreak is a no-op, so
    the winner is still the highest hit_count — byte-identical to pre-authoring."""
    food = _make_category(session, user.id, "Food")
    subs = _make_category(session, user.id, "Subscriptions")
    session.add_all(
        [
            MerchantTagMap(
                user_id=user.id, merchant_normalized="swiggy", category_id=food.id, hit_count=5
            ),
            MerchantTagMap(
                user_id=user.id, merchant_normalized="swiggy", category_id=subs.id, hit_count=9
            ),
        ]
    )
    session.flush()
    assert prefetch_tag_map(session, user_id=user.id) == {"swiggy": subs.id}


def test_prefetch_pinned_beats_higher_hit_count(session: Session, user: User) -> None:
    """The load-bearing behaviour: a pinned rule outranks any higher-hit_count
    learned row for the same merchant."""
    food = _make_category(session, user.id, "Food")
    subs = _make_category(session, user.id, "Subscriptions")
    session.add_all(
        [
            MerchantTagMap(
                user_id=user.id,
                merchant_normalized="swiggy",
                category_id=food.id,
                hit_count=1,
                pinned=True,
            ),
            MerchantTagMap(
                user_id=user.id, merchant_normalized="swiggy", category_id=subs.id, hit_count=99
            ),
        ]
    )
    session.flush()
    assert prefetch_tag_map(session, user_id=user.id) == {"swiggy": food.id}


def test_pin_tag_inserts_new_pinned_row(session: Session, user: User) -> None:
    food = _make_category(session, user.id, "Food")
    row = pin_tag(session, user_id=user.id, merchant_normalized="swiggy", category_id=food.id)
    session.flush()
    assert row.pinned is True
    assert row.hit_count == 1  # authored floor
    assert row.category_id == food.id


def test_pin_tag_pins_existing_without_touching_hit_count(session: Session, user: User) -> None:
    """A pin is an assertion, not an observed decision — hit_count/last_used stay."""
    food = _make_category(session, user.id, "Food")
    old = datetime.now(UTC) - timedelta(days=30)
    session.add(
        MerchantTagMap(
            user_id=user.id,
            merchant_normalized="swiggy",
            category_id=food.id,
            hit_count=7,
            last_used=old,
        )
    )
    session.flush()

    pin_tag(session, user_id=user.id, merchant_normalized="swiggy", category_id=food.id)
    session.flush()

    row = session.scalar(
        select(MerchantTagMap).where(MerchantTagMap.merchant_normalized == "swiggy")
    )
    assert row is not None
    assert row.pinned is True
    assert row.hit_count == 7  # untouched
    assert row.last_used.replace(tzinfo=None) == old.replace(tzinfo=None)  # untouched


def test_pin_tag_unpins_sibling_categories(session: Session, user: User) -> None:
    """Pinning a second category un-pins the first (single pinned category per
    merchant), but the sibling row survives with its learned hit_count intact."""
    food = _make_category(session, user.id, "Food")
    gift = _make_category(session, user.id, "Gift")
    session.add(
        MerchantTagMap(
            user_id=user.id,
            merchant_normalized="amazon",
            category_id=food.id,
            hit_count=3,
            pinned=True,
        )
    )
    session.flush()

    pin_tag(session, user_id=user.id, merchant_normalized="amazon", category_id=gift.id)
    session.flush()

    food_row = session.scalar(
        select(MerchantTagMap).where(
            MerchantTagMap.merchant_normalized == "amazon",
            MerchantTagMap.category_id == food.id,
        )
    )
    assert food_row is not None
    assert food_row.pinned is False  # un-pinned
    assert food_row.hit_count == 3  # learning intact
    pinned = session.scalars(
        select(MerchantTagMap).where(
            MerchantTagMap.merchant_normalized == "amazon",
            MerchantTagMap.pinned.is_(True),
        )
    ).all()
    assert len(pinned) == 1
    assert pinned[0].category_id == gift.id


def test_pin_tag_recovers_from_insert_race(
    engine: Engine, session: Session, user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Loser of the insert race refetches the existing row and pins it (no bump)."""
    food = _make_category(session, user.id, "Food")
    session.commit()
    _preinsert_racy(engine, user, food.id)  # hit_count=1, unpinned

    _stub_scalar_misses(session, monkeypatch, 1)  # probe miss → INSERT → conflict → refetch
    row = pin_tag(session, user_id=user.id, merchant_normalized="racy", category_id=food.id)
    session.commit()

    assert row.pinned is True
    assert row.hit_count == 1  # refetched existing row — hit_count untouched
    rows = session.scalars(
        select(MerchantTagMap).where(MerchantTagMap.merchant_normalized == "racy")
    ).all()
    assert len(rows) == 1


def test_pin_tag_conflict_no_winner_raises(
    engine: Engine, session: Session, user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unlike record_tag (logs + returns), an explicit pin FAILS LOUDLY if the
    post-conflict refetch finds no winner — it must never 'succeed' creating nothing."""
    food = _make_category(session, user.id, "Food")
    session.commit()
    _preinsert_racy(engine, user, food.id)

    _stub_scalar_misses(session, monkeypatch, 2)  # probe miss + refetch miss
    with pytest.raises(RuntimeError, match="winner refetch missed"):
        pin_tag(session, user_id=user.id, merchant_normalized="racy", category_id=food.id)


def test_set_tag_pinned_toggle_preserves_hit_count(session: Session, user: User) -> None:
    """Pin the lower-hit_count category → it wins; un-pin → reverts to the higher.
    hit_count is never touched by either toggle."""
    food = _make_category(session, user.id, "Food")
    subs = _make_category(session, user.id, "Subscriptions")
    session.add_all(
        [
            MerchantTagMap(
                user_id=user.id, merchant_normalized="swiggy", category_id=food.id, hit_count=2
            ),
            MerchantTagMap(
                user_id=user.id, merchant_normalized="swiggy", category_id=subs.id, hit_count=8
            ),
        ]
    )
    session.flush()
    food_row = session.scalar(
        select(MerchantTagMap).where(
            MerchantTagMap.merchant_normalized == "swiggy",
            MerchantTagMap.category_id == food.id,
        )
    )
    assert food_row is not None

    set_tag_pinned(session, user_id=user.id, map_id=food_row.id, pinned=True)
    session.flush()
    assert prefetch_tag_map(session, user_id=user.id) == {"swiggy": food.id}
    assert food_row.hit_count == 2  # untouched

    set_tag_pinned(session, user_id=user.id, map_id=food_row.id, pinned=False)
    session.flush()
    assert prefetch_tag_map(session, user_id=user.id) == {"swiggy": subs.id}  # reverted
    assert food_row.hit_count == 2  # still untouched


def test_set_tag_pinned_missing_returns_none(session: Session, user: User) -> None:
    assert set_tag_pinned(session, user_id=user.id, map_id=999999, pinned=True) is None


def test_set_tag_pinned_cross_user_returns_none(session: Session, user: User) -> None:
    other = User(id=uuid.uuid4())
    session.add(other)
    session.flush()
    other_cat = _make_category(session, other.id, "Food")
    row = MerchantTagMap(user_id=other.id, merchant_normalized="swiggy", category_id=other_cat.id)
    session.add(row)
    session.flush()

    assert set_tag_pinned(session, user_id=user.id, map_id=row.id, pinned=True) is None
    session.refresh(row)
    assert row.pinned is False  # untouched


def test_record_tag_bump_preserves_pinned(session: Session, user: User) -> None:
    """Learning bumps a pinned row's hit_count but never clears the pin."""
    food = _make_category(session, user.id, "Food")
    pin_tag(session, user_id=user.id, merchant_normalized="swiggy", category_id=food.id)
    session.flush()

    record_tag(session, user_id=user.id, merchant_normalized="swiggy", category_id=food.id)
    session.flush()

    row = session.scalar(
        select(MerchantTagMap).where(MerchantTagMap.merchant_normalized == "swiggy")
    )
    assert row is not None
    assert row.pinned is True
    assert row.hit_count == 2  # 1 (authored floor) + 1 (learned)
