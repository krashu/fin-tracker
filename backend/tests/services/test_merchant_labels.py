"""Unit tests for :mod:`app.services.merchant_labels` (F3a Phase 2).

Covers:

* :func:`prefetch_label_map` — single SELECT, returns the *set* of labels per
  merchant whose ``hit_count`` clears :data:`LABEL_PREFILL_MIN` (< threshold
  excluded); user-scoped.
* :func:`record_label` — one-statement INSERT … ON CONFLICT DO UPDATE hit_count+1 upsert.
* The composite same-user FK cascade: hard-deleting a label clears its map rows.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.models import Label, MerchantLabelMap, User
from app.services.merchant_alias import EMPTY_RESOLVER, AliasResolver
from app.services.merchant_labels import (
    LABEL_PREFILL_MIN,
    pin_label,
    prefetch_label_map,
    record_label,
    set_label_pinned,
)


def _make_label(session: Session, user_id: uuid.UUID, name: str) -> Label:
    label = Label(user_id=user_id, name=name)
    session.add(label)
    session.flush()
    return label


# ---------- prefetch_label_map --------------------------------------------


def test_prefetch_returns_empty_dict_for_empty_map(session: Session, user: User) -> None:
    assert prefetch_label_map(session, user_id=user.id, resolver=EMPTY_RESOLVER) == {}


def test_prefetch_includes_labels_at_threshold_excludes_below(session: Session, user: User) -> None:
    """hit_count ≥ LABEL_PREFILL_MIN prefills; a below-threshold label does not."""
    online = _make_label(session, user.id, "online")
    food = _make_label(session, user.id, "food")
    gift = _make_label(session, user.id, "gift")
    session.add_all(
        [
            MerchantLabelMap(
                user_id=user.id,
                merchant_normalized="swiggy",
                label_id=online.id,
                hit_count=LABEL_PREFILL_MIN,
            ),
            MerchantLabelMap(
                user_id=user.id,
                merchant_normalized="swiggy",
                label_id=food.id,
                hit_count=LABEL_PREFILL_MIN + 4,
            ),
            MerchantLabelMap(
                user_id=user.id,
                merchant_normalized="swiggy",
                label_id=gift.id,
                hit_count=LABEL_PREFILL_MIN - 1,  # one short → suppressed
            ),
        ]
    )
    session.flush()

    result = prefetch_label_map(session, user_id=user.id, resolver=EMPTY_RESOLVER)
    # Both threshold-clearing labels present, ordered hit_count DESC; the one-off
    # gift label is excluded.
    assert result == {"swiggy": [food.id, online.id]}


def test_prefetch_user_scoped(session: Session, user: User) -> None:
    other = User(id=uuid.uuid4())
    session.add(other)
    session.flush()

    mine = _make_label(session, user.id, "online")
    theirs = _make_label(session, other.id, "online")
    session.add_all(
        [
            MerchantLabelMap(
                user_id=other.id,
                merchant_normalized="amazon",
                label_id=theirs.id,
                hit_count=LABEL_PREFILL_MIN + 5,
            ),
            MerchantLabelMap(
                user_id=user.id,
                merchant_normalized="amazon",
                label_id=mine.id,
                hit_count=LABEL_PREFILL_MIN,
            ),
        ]
    )
    session.flush()

    assert prefetch_label_map(session, user_id=user.id, resolver=EMPTY_RESOLVER) == {
        "amazon": [mine.id]
    }


# ---------- record_label --------------------------------------------------


def test_record_label_inserts_when_missing(session: Session, user: User) -> None:
    online = _make_label(session, user.id, "online")

    record_label(session, user_id=user.id, merchant_normalized="swiggy", label_id=online.id)
    session.flush()

    row = session.scalar(
        select(MerchantLabelMap).where(MerchantLabelMap.merchant_normalized == "swiggy")
    )
    assert row is not None
    assert row.label_id == online.id
    assert row.hit_count == 1


def test_record_label_increments_existing(session: Session, user: User) -> None:
    online = _make_label(session, user.id, "online")
    old_last_used = datetime.now(UTC) - timedelta(days=30)
    session.add(
        MerchantLabelMap(
            user_id=user.id,
            merchant_normalized="swiggy",
            label_id=online.id,
            hit_count=1,
            last_used=old_last_used,
        )
    )
    session.flush()

    record_label(session, user_id=user.id, merchant_normalized="swiggy", label_id=online.id)
    session.flush()

    row = session.scalar(
        select(MerchantLabelMap).where(MerchantLabelMap.merchant_normalized == "swiggy")
    )
    assert row is not None
    assert row.hit_count == 2
    assert row.last_used.replace(tzinfo=None) > old_last_used.replace(tzinfo=None)


def test_record_label_noop_on_empty_merchant(session: Session, user: User) -> None:
    online = _make_label(session, user.id, "online")

    record_label(session, user_id=user.id, merchant_normalized="", label_id=online.id)
    session.flush()

    assert session.scalars(select(MerchantLabelMap)).all() == []


def test_record_label_repeated_bumps_accumulate(session: Session, user: User) -> None:
    """Two same-triple bumps add +2 (locks read-modify-write against a deferred
    SQL-expression regression under autoflush=False)."""
    online = _make_label(session, user.id, "online")
    session.add(
        MerchantLabelMap(
            user_id=user.id, merchant_normalized="swiggy", label_id=online.id, hit_count=5
        )
    )
    session.flush()

    for _ in range(2):
        record_label(session, user_id=user.id, merchant_normalized="swiggy", label_id=online.id)
    session.flush()

    row = session.scalar(
        select(MerchantLabelMap).where(MerchantLabelMap.merchant_normalized == "swiggy")
    )
    assert row is not None
    assert row.hit_count == 7


# ---------- composite same-user FK cascade --------------------------------


def test_hard_delete_label_cascades_map_rows(session: Session, user: User) -> None:
    """Deleting a label clears its merchant_label_map rows via ON DELETE CASCADE
    (the composite (label_id, user_id) FK) — no manual cleanup in the DELETE route.
    """
    online = _make_label(session, user.id, "online")
    keep = _make_label(session, user.id, "food")
    session.add_all(
        [
            MerchantLabelMap(
                user_id=user.id, merchant_normalized="swiggy", label_id=online.id, hit_count=3
            ),
            MerchantLabelMap(
                user_id=user.id, merchant_normalized="swiggy", label_id=keep.id, hit_count=3
            ),
        ]
    )
    session.commit()

    session.delete(online)
    session.commit()

    remaining = session.scalars(select(MerchantLabelMap)).all()
    assert len(remaining) == 1
    assert remaining[0].label_id == keep.id


# ---------- concurrent rows / statement shape (mirrors tag_service) -------


def _preinsert_racy(engine: Engine, user: User, label_id: int) -> None:
    """Commit a (user, "racy", label_id) rule via a parallel session so the next
    INSERT of the same triple trips the unique constraint."""
    other_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    other = other_factory()
    try:
        other.add(
            MerchantLabelMap(
                user_id=user.id, merchant_normalized="racy", label_id=label_id, hit_count=1
            )
        )
        other.commit()
    finally:
        other.close()


def _stub_scalar_misses(session: Session, monkeypatch: pytest.MonkeyPatch, misses: int) -> None:
    """Force ``session.scalar`` to return None for its next ``misses`` calls."""
    real_scalar = session.scalar
    remaining = [misses]

    def stubbed(*args: object, **kwargs: object) -> object:
        if remaining[0] > 0:
            remaining[0] -= 1
            return None
        return real_scalar(*args, **kwargs)

    monkeypatch.setattr(session, "scalar", stubbed)


def test_record_label_handles_concurrent_insert_race(
    engine: Engine, session: Session, user: User
) -> None:
    """A row committed by another connection is bumped, not duplicated."""
    online = _make_label(session, user.id, "online")
    session.commit()
    _preinsert_racy(engine, user, online.id)

    record_label(session, user_id=user.id, merchant_normalized="racy", label_id=online.id)
    session.commit()

    rows = session.scalars(
        select(MerchantLabelMap).where(MerchantLabelMap.merchant_normalized == "racy")
    ).all()
    assert len(rows) == 1
    assert rows[0].hit_count == 2


def test_record_label_race_preserves_caller_pending_state(
    engine: Engine, session: Session, user: User
) -> None:
    """record_label leaves the caller's pending, unflushed mutation alone (no
    SAVEPOINT, no rollback — the silent-data-loss regression guard)."""
    online = _make_label(session, user.id, "online")
    session.commit()
    _preinsert_racy(engine, user, online.id)

    pending = Label(user_id=user.id, name="pendingmutation")
    session.add(pending)  # no flush — a full rollback would discard it

    record_label(session, user_id=user.id, merchant_normalized="racy", label_id=online.id)
    session.commit()

    assert pending.id is not None
    assert session.scalar(select(Label).where(Label.name == "pendingmutation")) is not None
    rows = session.scalars(
        select(MerchantLabelMap).where(MerchantLabelMap.merchant_normalized == "racy")
    ).all()
    assert len(rows) == 1
    assert rows[0].hit_count == 2


def test_record_label_reraises_unexpected_integrity_error(session: Session, user: User) -> None:
    """A non-conflict IntegrityError (FK to a nonexistent label) propagates."""
    with pytest.raises(IntegrityError):
        record_label(session, user_id=user.id, merchant_normalized="racy", label_id=999_999)


def test_record_label_increment_is_a_single_statement(
    engine: Engine, session: Session, user: User
) -> None:
    """ADR-0004 rule 7: the bump is one ``INSERT … ON CONFLICT DO UPDATE``, no SELECT."""
    online = _make_label(session, user.id, "online")
    session.add(
        MerchantLabelMap(
            user_id=user.id, merchant_normalized="swiggy", label_id=online.id, hit_count=5
        )
    )
    session.flush()

    statements: list[str] = []

    def capture(conn: object, cursor: object, statement: str, *rest: object) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        record_label(session, user_id=user.id, merchant_normalized="swiggy", label_id=online.id)
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert len(statements) == 1
    sql = statements[0].upper()
    assert sql.startswith("INSERT INTO MERCHANT_LABEL_MAP")
    assert "ON CONFLICT" in sql
    assert "DO UPDATE" in sql
    assert "SELECT" not in sql.split("RETURNING")[0]
    row = session.scalar(select(MerchantLabelMap))
    assert row is not None
    assert row.hit_count == 6


# ---------- pin_label / set_label_pinned (F3a rule authoring) -------------


def test_prefetch_pinned_label_below_threshold_prefills(session: Session, user: User) -> None:
    """The behavioural change on this path: a pinned label prefills even though its
    hit_count is below LABEL_PREFILL_MIN."""
    online = _make_label(session, user.id, "online")
    session.add(
        MerchantLabelMap(
            user_id=user.id,
            merchant_normalized="amazon",
            label_id=online.id,
            hit_count=1,
            pinned=True,
        )
    )
    session.flush()
    assert prefetch_label_map(session, user_id=user.id, resolver=EMPTY_RESOLVER) == {
        "amazon": [online.id]
    }


def test_pin_label_inserts_new_pinned_row(session: Session, user: User) -> None:
    online = _make_label(session, user.id, "online")
    row = pin_label(session, user_id=user.id, merchant_normalized="amazon", label_id=online.id)
    session.flush()
    assert row.pinned is True
    assert row.hit_count == 1  # authored floor


def test_pin_label_pins_existing_without_touching_hit_count(session: Session, user: User) -> None:
    online = _make_label(session, user.id, "online")
    old = datetime.now(UTC) - timedelta(days=30)
    session.add(
        MerchantLabelMap(
            user_id=user.id,
            merchant_normalized="amazon",
            label_id=online.id,
            hit_count=6,
            last_used=old,
        )
    )
    session.flush()

    pin_label(session, user_id=user.id, merchant_normalized="amazon", label_id=online.id)
    session.flush()

    row = session.scalar(
        select(MerchantLabelMap).where(MerchantLabelMap.merchant_normalized == "amazon")
    )
    assert row is not None
    assert row.pinned is True
    assert row.hit_count == 6  # untouched
    assert row.last_used.replace(tzinfo=None) == old.replace(tzinfo=None)  # untouched


def test_pin_label_allows_multiple_pinned_per_merchant(session: Session, user: User) -> None:
    """Labels are a set — pinning a second label does NOT un-pin the first
    (contrast tag_service.pin_tag's single-pinned-category invariant)."""
    online = _make_label(session, user.id, "online")
    gift = _make_label(session, user.id, "gift")
    pin_label(session, user_id=user.id, merchant_normalized="amazon", label_id=online.id)
    pin_label(session, user_id=user.id, merchant_normalized="amazon", label_id=gift.id)
    session.flush()

    pinned = session.scalars(
        select(MerchantLabelMap).where(
            MerchantLabelMap.merchant_normalized == "amazon",
            MerchantLabelMap.pinned.is_(True),
        )
    ).all()
    assert len(pinned) == 2


def test_pin_label_recovers_from_insert_race(
    engine: Engine, session: Session, user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Loser of the insert race refetches the existing row and pins it (no bump) —
    the label mirror of test_pin_tag_recovers_from_insert_race, guarding the
    conflict→refetch→pin success branch (record miss would ship green otherwise)."""
    online = _make_label(session, user.id, "online")
    session.commit()
    _preinsert_racy(engine, user, online.id)  # hit_count=1, unpinned

    _stub_scalar_misses(session, monkeypatch, 1)  # probe miss → INSERT → conflict → refetch
    row = pin_label(session, user_id=user.id, merchant_normalized="racy", label_id=online.id)
    session.commit()

    assert row.pinned is True
    assert row.hit_count == 1  # refetched existing row — hit_count untouched
    rows = session.scalars(
        select(MerchantLabelMap).where(MerchantLabelMap.merchant_normalized == "racy")
    ).all()
    assert len(rows) == 1


def test_pin_label_conflict_no_winner_raises(
    engine: Engine, session: Session, user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit pin fails loudly on a post-conflict refetch miss (never a
    silent success), unlike record_label's tolerated log-and-return."""
    online = _make_label(session, user.id, "online")
    session.commit()
    _preinsert_racy(engine, user, online.id)

    _stub_scalar_misses(session, monkeypatch, 2)  # probe miss + refetch miss
    with pytest.raises(RuntimeError, match="winner refetch missed"):
        pin_label(session, user_id=user.id, merchant_normalized="racy", label_id=online.id)


def test_set_label_pinned_toggle_preserves_hit_count(session: Session, user: User) -> None:
    online = _make_label(session, user.id, "online")
    session.add(
        MerchantLabelMap(
            user_id=user.id, merchant_normalized="amazon", label_id=online.id, hit_count=1
        )
    )
    session.flush()
    row = session.scalar(
        select(MerchantLabelMap).where(MerchantLabelMap.merchant_normalized == "amazon")
    )
    assert row is not None
    assert (
        prefetch_label_map(session, user_id=user.id, resolver=EMPTY_RESOLVER) == {}
    )  # below bar, unpinned

    set_label_pinned(session, user_id=user.id, map_id=row.id, pinned=True)
    session.flush()
    assert prefetch_label_map(session, user_id=user.id, resolver=EMPTY_RESOLVER) == {
        "amazon": [online.id]
    }
    assert row.hit_count == 1  # untouched

    set_label_pinned(session, user_id=user.id, map_id=row.id, pinned=False)
    session.flush()
    assert prefetch_label_map(session, user_id=user.id, resolver=EMPTY_RESOLVER) == {}  # reverted
    assert row.hit_count == 1  # still untouched


def test_set_label_pinned_missing_returns_none(session: Session, user: User) -> None:
    assert set_label_pinned(session, user_id=user.id, map_id=999999, pinned=True) is None


def test_record_label_bump_preserves_pinned(session: Session, user: User) -> None:
    """Learning bumps a pinned label's hit_count but never clears the pin."""
    online = _make_label(session, user.id, "online")
    pin_label(session, user_id=user.id, merchant_normalized="amazon", label_id=online.id)
    session.flush()

    record_label(session, user_id=user.id, merchant_normalized="amazon", label_id=online.id)
    session.flush()

    row = session.scalar(
        select(MerchantLabelMap).where(MerchantLabelMap.merchant_normalized == "amazon")
    )
    assert row is not None
    assert row.pinned is True
    assert row.hit_count == 2  # 1 (authored floor) + 1 (learned)


# ---------- Phase A2: aggregation across an alias --------------------------


def test_prefetch_label_map_sums_hit_count_across_aliased_merchants(
    session: Session, user: User
) -> None:
    """Three raw descriptors at hit_count=1 each cannot individually clear
    LABEL_PREFILL_MIN, but their alias-summed canonical does — the label-side
    half of required test 3."""
    online = _make_label(session, user.id, "online")
    resolver = AliasResolver(((("swiggy",), "food"),))
    session.add_all(
        [
            MerchantLabelMap(
                user_id=user.id, merchant_normalized="swiggy blr 1", label_id=online.id
            ),
            MerchantLabelMap(
                user_id=user.id, merchant_normalized="swiggy blr 2", label_id=online.id
            ),
            MerchantLabelMap(
                user_id=user.id, merchant_normalized="upi swiggy 3", label_id=online.id
            ),
        ]
    )
    session.flush()
    assert prefetch_label_map(session, user_id=user.id, resolver=resolver) == {"food": [online.id]}


def test_prefetch_label_map_skips_unknown_via_groups(session: Session, user: User) -> None:
    from app.services.merchant_alias import build_resolver

    online = _make_label(session, user.id, "online")
    session.add(
        MerchantLabelMap(
            user_id=user.id,
            merchant_normalized="upi/9876543210@ybl",
            label_id=online.id,
            hit_count=LABEL_PREFILL_MIN + 1,
        )
    )
    session.flush()
    assert prefetch_label_map(session, user_id=user.id, resolver=build_resolver([])) == {}
