"""Tests for :mod:`app.services.bulk_categorise` around gateway handles (ADR-0011 rule 12)."""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import clock
from app.models import Account, Category, MerchantTagMap, Transaction, User
from app.services.bulk_categorise import bulk_categorise, past_rows_like
from app.services.merchant import normalize_merchant
from app.services.merchant_alias import build_resolver


def _board_spend(
    session: Session, user: User, account: Account, raw: str, n: int, category_id: int | None
) -> Transaction:
    txn = Transaction(
        user_id=user.id,
        account_id=account.id,
        date=date(2026, 5, 10),
        amount_paise=-10000,
        transaction_type="spend",
        merchant_raw=raw,
        merchant_normalized=normalize_merchant(raw),
        category_id=category_id,
        fingerprint=f"fp-{n}",
        source="import",
        confirmed_at=clock.naive_utcnow(),
    )
    session.add(txn)
    session.flush()
    return txn


def _fixture(session: Session, user: User) -> tuple[Category, Account]:
    cat = Category(color="#2a78d6", user_id=user.id, name="Food")
    acct = Account(user_id=user.id, name="Axis CC", type="credit_card", issuer="axis", last4="1234")
    session.add_all([cat, acct, Category(color="#2a78d6", user_id=user.id, name="Other")])
    session.flush()
    return cat, acct


def test_past_rows_like_offers_nothing_for_a_gateway_handle(session: Session, user: User) -> None:
    cat, acct = _fixture(session, user)
    source = _board_spend(session, user, acct, "UPI/9876543210@YBL", 1, cat.id)
    _board_spend(session, user, acct, "UPI/9123456780@YBL", 2, None)
    rows = past_rows_like(
        session,
        user_id=user.id,
        source=source,
        resolver=build_resolver([]),
        since=date(2025, 1, 1),
    )
    assert rows == []


def test_bulk_categorise_teaches_nothing_for_gateway_handles(session: Session, user: User) -> None:
    cat, acct = _fixture(session, user)
    rows = [
        _board_spend(session, user, acct, "UPI/9876543210@YBL", 1, None),
        _board_spend(session, user, acct, "UPI/9123456780@YBL", 2, None),
    ]
    outcome = bulk_categorise(
        session,
        user_id=user.id,
        rows=rows,
        category_id=cat.id,
        resolver=build_resolver([]),
    )
    session.flush()
    assert outcome.updated == 2
    assert outcome.taught == 0
    assert session.scalars(select(MerchantTagMap)).all() == []


def test_past_rows_like_matches_aliased_and_exact_merchants(session: Session, user: User) -> None:
    cat, acct = _fixture(session, user)
    # Source is an aliased merchant (Swiggy Instamart -> Swiggy)
    source = _board_spend(session, user, acct, "SWIGGY INSTAMART BLR", 10, cat.id)
    # Similar past row with another variant mapping to same canonical
    match1 = _board_spend(session, user, acct, "SWIGGY BANGALORE", 11, None)
    # Unrelated merchant that must NOT be matched
    _board_spend(session, user, acct, "UBER INDIA", 12, None)

    resolver = build_resolver([("swiggy", "swiggy"), ("swiggy instamart", "swiggy")])
    rows = past_rows_like(
        session,
        user_id=user.id,
        source=source,
        resolver=resolver,
        since=date(2025, 1, 1),
    )
    assert len(rows) == 1
    assert rows[0].id == match1.id
