"""What the DB actually hands back for a ``DateTime`` column.

This is a **contract net**, not a feature test. Every service test in this
directory runs with ``expire_on_commit=False``, so none of them ever re-reads a
column from the database — the identity map answers instead. That means nothing in
the suite observes the single most surprising thing about this stack:

    a ``datetime`` written **aware** (UTC) comes back **naive** from SQLite.

Production depends on that being true, because 7 ``clock.utcnow()`` call sites still write
*aware* values to two columns (``confirmed_at``, ``archived_at``) and rely on SQLite
normalizing them — ADR-0001 rule 5 lists them as Postgres-cutover items. Without the
assertions below, nothing in the suite observes the boundary at all. (``nav_updated_at``
was the third such column until every one of its writers went naive.)

(The ``.date()`` calls in ``nav_snapshot_service`` / ``performance_service`` are *not*
what these tests protect, contrary to an earlier reading: they are mandatory
``datetime`` → ``date`` conversions — ``as_of`` is a ``date`` — so deleting one is an
immediate ``TypeError``, not a silent regression. They need no net.)

``last_used`` on both merchant maps is the concrete case, and since remediation step 11
it has three write paths, all pinned below: an explicit caller-supplied value, the
``base.utcnow_default`` Python default on INSERT, and the ``server_default=func.now()``
backstop that only a raw non-ORM ``INSERT`` can still reach.

The second group of tests is the ADR-0001 rule-5 gate: they pin ``clock.naive_utcnow`` to
a fixed instant and assert the *app* clock — not the DB's — wrote the column. That is the
only assertion here that can go red on SQLite, where ``func.now()`` and the app clock are
otherwise indistinguishable.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import DateTime, select, text
from sqlalchemy.orm import Session

from app.core import clock
from app.models import Base, Category, Label, MerchantLabelMap, MerchantTagMap, User


def _category(session: Session, user_id: object, name: str) -> Category:
    c = Category(user_id=user_id, name=name)
    session.add(c)
    session.flush()
    return c


def test_aware_datetime_reads_back_naive_across_a_session_boundary(
    session: Session, fresh_session: Session, user: User
) -> None:
    """An aware-UTC write is returned NAIVE, with the wall-clock instant preserved.

    The wall-clock equality matters as much as the missing tzinfo: SQLite is not
    shifting the value, it is dropping the offset. So ``.replace(tzinfo=UTC)`` is a
    valid way to recover it, and ``.date()`` is a valid way to compare it — which is
    exactly what production does.
    """
    written = datetime(2026, 3, 14, 9, 30, 0, tzinfo=UTC)
    food = _category(session, user.id, "Food")
    # Bind the instance to a local and KEEP it bound. SQLAlchemy's identity map
    # holds only weak references, so an inline-constructed row is collected right
    # after commit and even a same-session re-read would hit the DB — which would
    # make this test pass for the wrong reason and hide a regression in the
    # boundary itself. Holding `written_row` keeps `session` able to answer from
    # memory, so the assertions below can only pass via `fresh_session`.
    written_row = MerchantTagMap(
        user_id=user.id,
        merchant_normalized="swiggy",
        category_id=food.id,
        hit_count=1,
        last_used=written,
    )
    session.add(written_row)
    session.commit()
    # Proof the precondition holds: the writing session still has it, still aware.
    assert written_row.last_used is written

    row = fresh_session.scalar(
        select(MerchantTagMap).where(MerchantTagMap.merchant_normalized == "swiggy")
    )
    assert row is not None
    assert row is not written_row  # a genuinely separate, DB-loaded instance

    # THE contract: the offset is gone.
    assert row.last_used.tzinfo is None
    # ...and nothing else moved — same wall clock, so the offset was dropped, not applied.
    assert row.last_used == written.replace(tzinfo=None)
    # Hence the two idioms production relies on both hold.
    assert row.last_used.replace(tzinfo=UTC) == written
    assert row.last_used.date() == written.date()

    # Direct comparison is the trap this net exists to keep visible.
    try:
        _ = row.last_used > written
    except TypeError:
        pass
    else:  # pragma: no cover - only reachable if SQLAlchemy starts returning aware
        raise AssertionError(
            "naive/aware comparison unexpectedly succeeded — if the driver now "
            "returns aware datetimes, the .date() funnels in nav_snapshot_service "
            "can be revisited"
        )


def test_server_default_datetime_also_reads_back_naive(
    session: Session, fresh_session: Session, user: User
) -> None:
    """The ``server_default=func.now()`` path is naive too.

    Since step 11 gave ``last_used`` a Python-side default, neither an ORM insert nor a
    Core ``insert()`` reaches the server default any more — SQLAlchemy applies column
    defaults on both paths. Only **textual** SQL bypasses the mapper, so that is what
    this drives. It is exactly the raw-``INSERT`` backstop the model keeps
    ``server_default`` for and that ``test_migration_parity`` protects, so it must stay
    pinned independently of the Python default.
    """
    food = _category(session, user.id, "Food")
    session.commit()

    session.execute(
        text(
            "INSERT INTO merchant_tag_map "
            "  (user_id, merchant_normalized, category_id, hit_count, pinned) "
            "VALUES (:u, 'zomato', :c, 1, 0)"
        ).bindparams(u=user.id.hex, c=food.id)
    )
    session.commit()

    row = fresh_session.scalar(
        select(MerchantTagMap).where(MerchantTagMap.merchant_normalized == "zomato")
    )
    assert row is not None
    assert row.last_used.tzinfo is None


# --------------------------------------------------------------------------------------
# ADR-0001 rule 5: the APP clock writes these columns, not the DB's.
#
# These are the only assertions in the suite that can distinguish the two on SQLite,
# where func.now() and clock.naive_utcnow() are both UTC and therefore identical in
# value. Pinning the app clock to an instant no wall clock will ever produce is what
# makes "which clock wrote this column" observable — and what makes reverting step 11
# go red instead of silently green until the Postgres cutover.
# --------------------------------------------------------------------------------------

PINNED = datetime(2031, 5, 17, 4, 30, 9)
LATER = datetime(2031, 5, 18, 6, 45, 12)


def test_no_datetime_column_relies_on_the_db_clock_alone() -> None:
    """ADR-0001 rule 5, machine-checked over the whole metadata.

    This replaces the gate the remediation plan prescribed — ``grep func.now()
    backend/app/models`` returning nothing — which is **unsatisfiable**: the models must
    KEEP ``server_default`` as the raw-``INSERT`` backstop, and
    ``tests/test_migration_parity`` compares DB-side defaults, so removing it without
    editing all seven migrations reddens that gate instead. The invariant the grep was
    reaching for is this one, and unlike a grep it also covers models nobody has written
    yet: a ``DateTime`` column may carry a DB-side default, but never *only* that, or
    Python reads back whatever the database server's timezone happened to be.
    """
    offenders = [
        f"{t.name}.{c.name}"
        for t in Base.metadata.sorted_tables
        for c in t.columns
        if isinstance(c.type, DateTime) and c.server_default is not None and c.default is None
    ]
    assert offenders == [], (
        f"{len(offenders)} DateTime column(s) defaulted by the DB clock alone: {offenders}. "
        "Add default=utcnow_default (app/models/base.py); keep the server_default."
    )


@pytest.fixture
def pinned_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Freeze the app clock. One patch target, per ``app.core.clock``'s module docstring."""
    monkeypatch.setattr(clock, "naive_utcnow", lambda: PINNED)


def test_created_at_comes_from_the_app_clock(
    session: Session, fresh_session: Session, user: User, pinned_clock: None
) -> None:
    """``TimestampMixin.created_at`` is stamped by ``base.utcnow_default``.

    Without the Python-side default the DB's ``func.now()`` fires instead and this reads
    the real wall clock — 2031 is unreachable, so the assertion is decisive.
    """
    food = _category(session, user.id, "Food")
    session.commit()

    row = fresh_session.scalar(select(Category).where(Category.id == food.id))
    assert row is not None
    assert row.created_at == PINNED
    assert row.updated_at == PINNED


def test_updated_at_is_restamped_by_the_app_clock_on_update(
    session: Session, fresh_session: Session, user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``onupdate`` runs the app clock too, so a mutation moves ``updated_at`` and not
    ``created_at``. Guards the ``onupdate=func.now()`` → ``utcnow_default`` half of the
    change, which is invisible to the schema and so has no other net."""
    monkeypatch.setattr(clock, "naive_utcnow", lambda: PINNED)
    food = _category(session, user.id, "Food")
    session.commit()

    monkeypatch.setattr(clock, "naive_utcnow", lambda: LATER)
    food.name = "Groceries"
    session.commit()

    row = fresh_session.scalar(select(Category).where(Category.id == food.id))
    assert row is not None
    assert row.created_at == PINNED, "created_at must not move on UPDATE"
    assert row.updated_at == LATER


def test_last_used_insert_comes_from_the_app_clock(
    session: Session, fresh_session: Session, user: User, pinned_clock: None
) -> None:
    """Both merchant maps' ``last_used`` takes the app clock on INSERT (B#58).

    The bump path already used the app clock; before step 11 the INSERT took the DB's,
    so one column was written by two clocks and the v1.5 stale-rule prune the column
    exists for would have ranked rows by which write path touched them last.
    """
    food = _category(session, user.id, "Food")
    label = Label(user_id=user.id, name="reimbursable")
    session.add(label)
    session.flush()

    session.add(
        MerchantTagMap(
            user_id=user.id, merchant_normalized="swiggy", category_id=food.id, hit_count=1
        )
    )
    session.add(
        MerchantLabelMap(
            user_id=user.id, merchant_normalized="swiggy", label_id=label.id, hit_count=1
        )
    )
    session.commit()

    tag_row = fresh_session.scalar(
        select(MerchantTagMap).where(MerchantTagMap.merchant_normalized == "swiggy")
    )
    label_row = fresh_session.scalar(
        select(MerchantLabelMap).where(MerchantLabelMap.merchant_normalized == "swiggy")
    )
    assert tag_row is not None and label_row is not None
    assert tag_row.last_used == PINNED
    assert label_row.last_used == PINNED


def test_last_used_bump_is_naive_before_any_readback(session: Session, user: User) -> None:
    """The bump writes a NAIVE instant, asserted in-memory rather than after a readback.

    This is the only way to catch a bump site regressing to ``clock.utcnow()``: SQLite
    strips the offset on the way in, so once the value has been through the DB, aware and
    naive are indistinguishable.

    ``bumped`` is bound to a local and KEPT bound for the reason the top of this module
    documents — the identity map holds only weak references, so without it the row is
    collected after the first commit, the re-read hits the DB, and the assertion passes
    naive no matter what the service assigned. That is not a hypothetical: this test did
    exactly that before the local was added.
    """
    from app.services.tag_service import record_tag

    food = _category(session, user.id, "Food")
    session.commit()
    record_tag(session, user_id=user.id, merchant_normalized="swiggy", category_id=food.id)
    session.commit()

    bumped = session.scalar(select(MerchantTagMap).where(MerchantTagMap.hit_count == 1))
    assert bumped is not None
    record_tag(session, user_id=user.id, merchant_normalized="swiggy", category_id=food.id)
    session.commit()

    assert bumped.hit_count == 2, "second record_tag should have taken the bump branch"
    # Never re-read: this is the instant the service assigned, not what SQLite returned.
    assert bumped.last_used.tzinfo is None
