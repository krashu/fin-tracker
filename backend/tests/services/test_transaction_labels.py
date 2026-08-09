"""Unit tests for :mod:`app.services.transaction_labels` (PRD §F3a).

Covers ``normalize_label_name`` (pure), ``resolve_label_names`` (get-or-create,
dedupe, order, blanks, conflict recovery) and ``set_labels_on_transaction``
(replace-set join diff, user_id stamping).
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Account, Label, Transaction, TransactionLabel, User
from app.services.transaction_labels import (
    _get_or_create_label,
    normalize_label_name,
    resolve_label_names,
    set_labels_on_transaction,
)


def _make_txn(session: Session, user_id: uuid.UUID, fp: str = "aa") -> Transaction:
    account = session.scalar(select(Account).where(Account.user_id == user_id))
    if account is None:
        account = Account(user_id=user_id, name="Axis CC", type="credit_card")
        session.add(account)
        session.flush()
    txn = Transaction(
        user_id=user_id,
        account_id=account.id,
        date=date(2026, 3, 5),
        amount_paise=-8500,
        transaction_type="spend",
        merchant_raw="STARBUCKS",
        merchant_normalized="starbucks",
        fingerprint=fp * 32,
        source="manual",
    )
    session.add(txn)
    session.flush()
    return txn


# --------------------------------------------------------------- normalize
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("travel", "travel"),
        ("#travel", "travel"),
        ("#Travel", "travel"),
        ("  #Food Court  ", "food court"),
        ("food   court", "food court"),  # internal whitespace collapsed
        ("a;b;c", "abc"),  # ';' removed (backup delimiter)
        ("#", None),  # just a hash → empty
        ("   ", None),  # blank
        ("", None),
        (None, None),
        ("a" * 80, "a" * 64),  # capped at 64
    ],
)
def test_normalize(raw: str | None, expected: str | None) -> None:
    assert normalize_label_name(raw) == expected


# ---------------------------------------------------------- resolve_label_names
def test_resolve_empty(session: Session, user: User) -> None:
    assert resolve_label_names(session, user_id=user.id, names=[]) == []
    assert resolve_label_names(session, user_id=user.id, names=["", "  ", "#"]) == []


def test_resolve_creates_new(session: Session, user: User) -> None:
    labels = resolve_label_names(session, user_id=user.id, names=["#Online", "restaurant"])
    assert [lab.name for lab in labels] == ["online", "restaurant"]
    assert session.scalar(select(func.count()).select_from(Label)) == 2


def test_resolve_reuses_existing(session: Session, user: User) -> None:
    first = resolve_label_names(session, user_id=user.id, names=["travel"])
    second = resolve_label_names(session, user_id=user.id, names=["#Travel"])
    assert first[0].id == second[0].id
    assert session.scalar(select(func.count()).select_from(Label)) == 1


def test_resolve_dedupes_and_preserves_order(session: Session, user: User) -> None:
    labels = resolve_label_names(
        session, user_id=user.id, names=["travel", "#TRAVEL", "food", "  travel  "]
    )
    assert [lab.name for lab in labels] == ["travel", "food"]
    assert session.scalar(select(func.count()).select_from(Label)) == 2


def test_get_or_create_recovers_from_conflict(session: Session, user: User) -> None:
    """The SAVEPOINT + refetch branch: inserting an already-present (user, name)
    trips uq_labels_user_name, is caught, and returns the existing row."""
    existing = Label(user_id=user.id, name="travel")
    session.add(existing)
    session.flush()

    winner = _get_or_create_label(session, user_id=user.id, name="travel")
    assert winner.id == existing.id
    assert session.scalar(select(func.count()).select_from(Label)) == 1


# ------------------------------------------------------ set_labels_on_transaction
def test_set_labels_adds(session: Session, user: User) -> None:
    txn = _make_txn(session, user.id)
    labels = resolve_label_names(session, user_id=user.id, names=["online", "restaurant"])
    set_labels_on_transaction(session, txn=txn, labels=labels)
    session.commit()

    assert {lab.name for lab in txn.labels} == {"online", "restaurant"}
    rows = list(session.scalars(select(TransactionLabel)))
    assert len(rows) == 2
    # user_id stamped on every join row (the composite same-user FK member).
    assert all(r.user_id == user.id for r in rows)


def test_set_labels_replaces(session: Session, user: User) -> None:
    txn = _make_txn(session, user.id)
    set_labels_on_transaction(
        session, txn=txn, labels=resolve_label_names(session, user_id=user.id, names=["a", "b"])
    )
    session.commit()

    # Replace {a, b} with {b, c}: a removed, c added, b kept.
    set_labels_on_transaction(
        session, txn=txn, labels=resolve_label_names(session, user_id=user.id, names=["b", "c"])
    )
    session.commit()

    assert {lab.name for lab in txn.labels} == {"b", "c"}
    assert session.scalar(select(func.count()).select_from(TransactionLabel)) == 2


def test_set_labels_clear(session: Session, user: User) -> None:
    txn = _make_txn(session, user.id)
    set_labels_on_transaction(
        session, txn=txn, labels=resolve_label_names(session, user_id=user.id, names=["x"])
    )
    session.commit()

    set_labels_on_transaction(session, txn=txn, labels=[])
    session.commit()

    assert list(txn.labels) == []
    assert session.scalar(select(func.count()).select_from(TransactionLabel)) == 0
