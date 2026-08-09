"""Round-trip + constraint tests for the persistence-layer models.

Goals:

* Verify every model can round-trip a representative row through SQLite.
* Verify the dedup, uniqueness, and FK constraints declared in the schema
  actually fire — these are the contracts the future ``import_service`` will
  rely on, so they're worth asserting at the model layer.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from app.models import (
    Account,
    Category,
    ImportBatch,
    MerchantTagMap,
    Transaction,
    User,
)

# ---------- Small helpers used across tests --------------------------------


def _make_user(session: Session, email: str | None = None) -> User:
    u = User(email=email)
    session.add(u)
    session.flush()
    return u


def _make_account(session: Session, user: User, **overrides: object) -> Account:
    defaults: dict[str, object] = {
        "user_id": user.id,
        "name": "Axis Cashback CC",
        "type": "credit_card",
        "issuer": "axis",
        "last4": "1234",
    }
    defaults.update(overrides)
    a = Account(**defaults)
    session.add(a)
    session.flush()
    return a


def _make_category(session: Session, user: User, name: str = "Food") -> Category:
    c = Category(user_id=user.id, name=name)
    session.add(c)
    session.flush()
    return c


# ---------- User / Account / Category --------------------------------------


def test_user_round_trip(session: Session) -> None:
    u = _make_user(session, email=None)
    session.commit()
    fetched = session.scalars(select(User)).one()
    assert fetched.id == u.id
    assert fetched.email is None
    assert isinstance(fetched.created_at, datetime)


def test_account_round_trip_with_all_fields(session: Session) -> None:
    user = _make_user(session)
    parent = _make_account(session, user, name="HDFC Savings", type="bank", last4=None)
    child = Account(
        user_id=user.id,
        name="HDFC Regalia CC",
        type="credit_card",
        issuer="hdfc",
        last4="9876",
        opening_balance_paise=-15_000_00,  # ₹15,000 outstanding
        currency="INR",
        parent_account_id=parent.id,
    )
    session.add(child)
    session.commit()
    fetched = session.scalars(select(Account).where(Account.name == "HDFC Regalia CC")).one()
    assert fetched.opening_balance_paise == -1_500_000
    assert fetched.parent_account_id == parent.id


def test_account_type_enum_rejects_unknown(session: Session) -> None:
    user = _make_user(session)
    session.add(Account(user_id=user.id, name="X", type="cryptowallet", currency="INR"))
    # validate_strings=True surfaces the rejection as StatementError-wrapping-
    # LookupError at the Python layer before the SQL hits SQLite. The DB-level
    # CHECK constraint behind it would raise IntegrityError; either is fine.
    with pytest.raises(StatementError):
        session.commit()


def test_category_unique_name_per_user(session: Session) -> None:
    user = _make_user(session)
    _make_category(session, user, "Food")
    session.commit()
    session.add(Category(user_id=user.id, name="Food"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_category_same_name_different_user_ok(session: Session) -> None:
    u1 = _make_user(session, email="a@x")
    u2 = _make_user(session, email="b@x")
    session.add_all([Category(user_id=u1.id, name="Food"), Category(user_id=u2.id, name="Food")])
    session.commit()
    assert session.scalars(select(Category)).all()  # both rows persisted


def test_category_default_kind_is_spend(session: Session) -> None:
    """The Python-side default populates kind on ORM inserts that omit it."""
    user = _make_user(session)
    c = _make_category(session, user, "Food")
    session.commit()
    assert c.kind == "spend"


def test_category_same_name_different_kind_ok(session: Session) -> None:
    """The active-name unique index includes kind, so "Other" can exist once
    per scope (spend + income). This is the load-bearing new invariant."""
    user = _make_user(session)
    session.add_all(
        [
            Category(user_id=user.id, name="Other", kind="spend"),
            Category(user_id=user.id, name="Other", kind="income"),
        ]
    )
    session.commit()
    rows = session.scalars(select(Category).where(Category.name == "Other")).all()
    assert {r.kind for r in rows} == {"spend", "income"}


def test_category_same_name_same_kind_raises(session: Session) -> None:
    user = _make_user(session)
    session.add(Category(user_id=user.id, name="Salary", kind="income"))
    session.commit()
    session.add(Category(user_id=user.id, name="Salary", kind="income"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_category_archive_then_recreate_same_name_ok(session: Session) -> None:
    """Soft-delete a category; the same name can be reused.

    The partial unique index excludes archived rows so a user can recreate
    a category they previously archived without colliding on the constraint.
    """
    user = _make_user(session)
    original = _make_category(session, user, "Food")
    original.archived_at = datetime.now(tz=UTC).replace(tzinfo=None)
    session.commit()
    _make_category(session, user, "Food")
    session.commit()
    rows = session.scalars(select(Category).where(Category.name == "Food")).all()
    assert len(rows) == 2
    assert sum(1 for r in rows if r.archived_at is None) == 1


def test_category_unarchive_when_active_dup_exists_raises(session: Session) -> None:
    """Un-archiving must fail if it would produce two active rows with the same name.

    Race shape: archive A('Food'), then create active B('Food'), then try to
    un-archive A. The partial unique index catches the second active 'Food'
    on flush.
    """
    user = _make_user(session)
    a = _make_category(session, user, "Food")
    a.archived_at = datetime.now(tz=UTC).replace(tzinfo=None)
    session.commit()
    _make_category(session, user, "Food")
    session.commit()
    a.archived_at = None
    with pytest.raises(IntegrityError):
        session.commit()


# ---------- Transaction ----------------------------------------------------


def _fingerprint(suffix: str = "a") -> str:
    return ("0" * 63) + suffix  # 64-char hex-looking string, content irrelevant for tests


def _make_transaction(
    session: Session,
    user: User,
    account: Account,
    category: Category | None = None,
    *,
    fingerprint: str,
    amount_paise: int = -45000,
    txn_type: str = "spend",
) -> Transaction:
    t = Transaction(
        user_id=user.id,
        account_id=account.id,
        date=date(2026, 3, 15),
        amount_paise=amount_paise,
        transaction_type=txn_type,
        merchant_raw="SENTINEL CAFE BLR",
        merchant_normalized="SENTINEL CAFE",
        category_id=category.id if category else None,
        fingerprint=fingerprint,
        source="import",
    )
    session.add(t)
    session.flush()
    return t


def test_transaction_round_trip(session: Session) -> None:
    u = _make_user(session)
    a = _make_account(session, u)
    cat = _make_category(session, u)
    _make_transaction(session, u, a, cat, fingerprint=_fingerprint())
    session.commit()
    fetched = session.scalars(select(Transaction)).one()
    assert fetched.amount_paise == -45000
    assert fetched.transaction_type == "spend"
    assert fetched.merchant_normalized == "SENTINEL CAFE"
    assert fetched.created_at <= datetime.now(tz=UTC).replace(tzinfo=None)


def test_transaction_fingerprint_unique_within_account(session: Session) -> None:
    u = _make_user(session)
    a = _make_account(session, u)
    _make_transaction(session, u, a, fingerprint=_fingerprint("a"))
    session.commit()
    # Don't go through the _make_transaction helper (which flushes) — we want
    # the IntegrityError surfaced by commit() inside the `pytest.raises` block.
    session.add(
        Transaction(
            user_id=u.id,
            account_id=a.id,
            date=date(2026, 3, 15),
            amount_paise=-100,
            transaction_type="spend",
            merchant_raw="X",
            merchant_normalized="X",
            fingerprint=_fingerprint("a"),
            source="import",
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_same_fingerprint_allowed_at_a_different_occurrence(session: Session) -> None:
    """The DB-level statement of the ADR-0006 fix.

    Two rows, one fingerprint, distinct ``occurrence`` — the widened constraint
    admits both. This is what makes two same-fare auto rides on one day storable.
    Note the sibling test above still passes: both its rows default to
    occurrence 0, so the double-submit 409 guard is untouched.
    """
    u = _make_user(session)
    a = _make_account(session, u)
    _make_transaction(session, u, a, fingerprint=_fingerprint("a"))
    session.add(
        Transaction(
            user_id=u.id,
            account_id=a.id,
            date=date(2026, 3, 15),
            amount_paise=-45000,
            transaction_type="spend",
            merchant_raw="SENTINEL CAFE BLR",
            merchant_normalized="SENTINEL CAFE",
            fingerprint=_fingerprint("a"),
            occurrence=1,
            source="import",
        )
    )
    session.commit()  # no IntegrityError — occurrence differs
    rows = session.scalars(select(Transaction)).all()
    assert len(rows) == 2
    assert {t.occurrence for t in rows} == {0, 1}


def test_transaction_fingerprint_can_repeat_across_accounts(session: Session) -> None:
    """Different accounts → the (user_id, account_id, fingerprint) tuple differs."""
    u = _make_user(session)
    a1 = _make_account(session, u, name="acc1", last4="1111")
    a2 = _make_account(session, u, name="acc2", last4="2222")
    _make_transaction(session, u, a1, fingerprint=_fingerprint("a"))
    _make_transaction(session, u, a2, fingerprint=_fingerprint("a"))
    session.commit()  # no IntegrityError — different account_id
    assert len(session.scalars(select(Transaction)).all()) == 2


def test_transaction_large_amount_paise_survives(session: Session) -> None:
    """A ₹10-crore transaction = 10^9 paise; well within int64 but past int32 range."""
    u = _make_user(session)
    a = _make_account(session, u)
    _make_transaction(
        session, u, a, fingerprint=_fingerprint("b"), amount_paise=10_000_000_000_000
    )  # ₹10,000 cr
    session.commit()
    fetched = session.scalars(select(Transaction)).one()
    assert fetched.amount_paise == 10_000_000_000_000


def test_transaction_fk_to_account_enforced(session: Session) -> None:
    u = _make_user(session)
    bad = Transaction(
        user_id=u.id,
        account_id=99999,
        date=date(2026, 3, 15),
        amount_paise=-100,
        transaction_type="spend",
        merchant_raw="X",
        merchant_normalized="X",
        fingerprint=_fingerprint("c"),
        source="manual",
    )
    session.add(bad)
    with pytest.raises(IntegrityError):
        session.commit()


def test_transaction_transfer_pair_self_reference_rejected(session: Session) -> None:
    """ADR-0002: ck_transactions_no_self_pair forbids `t.transfer_pair_id = t.id`.

    Goes through raw SQL because SQLAlchemy attribute assignment + flush
    surfaces the same CHECK failure as an IntegrityError; using ``text()``
    keeps the assertion at the DB layer where the invariant lives.
    """
    u = _make_user(session)
    a = _make_account(session, u)
    t = _make_transaction(session, u, a, fingerprint=_fingerprint("f"), txn_type="transfer")
    session.commit()
    with pytest.raises(IntegrityError):
        session.execute(
            text("UPDATE transactions SET transfer_pair_id = :id WHERE id = :id"),
            {"id": t.id},
        )
        session.commit()


def test_transfer_pair_id_same_user_pair_allowed(session: Session) -> None:
    """Bidirectional A↔B link succeeds when both rows share user_id."""
    u = _make_user(session)
    bank = _make_account(session, u, name="bank", type="bank", last4=None)
    cc = _make_account(session, u, name="cc", type="credit_card")
    debit = _make_transaction(
        session,
        u,
        bank,
        fingerprint=_fingerprint("d"),
        amount_paise=-1_000_000,
        txn_type="transfer",
    )
    credit = _make_transaction(
        session,
        u,
        cc,
        fingerprint=_fingerprint("e"),
        amount_paise=1_000_000,
        txn_type="transfer",
    )
    debit.transfer_pair_id = credit.id
    credit.transfer_pair_id = debit.id
    session.commit()
    fetched_debit = session.get(Transaction, debit.id)
    assert fetched_debit is not None
    assert fetched_debit.transfer_pair_id == credit.id


def test_transfer_pair_id_cross_user_rejected(session: Session) -> None:
    """ADR-0002: composite FK forbids linking transactions across users."""
    u1 = _make_user(session, email="a@example.com")
    u2 = _make_user(session, email="b@example.com")
    a1 = _make_account(session, u1, name="u1-acc")
    a2 = _make_account(session, u2, name="u2-acc")
    t1 = _make_transaction(session, u1, a1, fingerprint=_fingerprint("g"))
    t2 = _make_transaction(session, u2, a2, fingerprint=_fingerprint("h"))
    session.commit()
    # Try to cross-link via raw SQL — Python-level assignment would
    # succeed (composite FK is checked at write time, not at attribute set).
    with pytest.raises(IntegrityError):
        session.execute(
            text("UPDATE transactions SET transfer_pair_id = :other WHERE id = :self_id"),
            {"other": t2.id, "self_id": t1.id},
        )
        session.commit()


# ---------- MerchantTagMap ------------------------------------------------


def test_merchant_tag_map_unique_triple(session: Session) -> None:
    u = _make_user(session)
    food = _make_category(session, u, "Food")
    session.add(MerchantTagMap(user_id=u.id, merchant_normalized="SWIGGY", category_id=food.id))
    session.commit()
    session.add(MerchantTagMap(user_id=u.id, merchant_normalized="SWIGGY", category_id=food.id))
    with pytest.raises(IntegrityError):
        session.commit()


def test_merchant_tag_map_multiple_categories_per_merchant_allowed(session: Session) -> None:
    u = _make_user(session)
    food = _make_category(session, u, "Food")
    gift = _make_category(session, u, "Gift")
    session.add_all(
        [
            MerchantTagMap(user_id=u.id, merchant_normalized="SWIGGY", category_id=food.id),
            MerchantTagMap(user_id=u.id, merchant_normalized="SWIGGY", category_id=gift.id),
        ]
    )
    session.commit()
    rows = session.scalars(
        select(MerchantTagMap).where(MerchantTagMap.merchant_normalized == "SWIGGY")
    ).all()
    assert len(rows) == 2


# ---------- ImportBatch ---------------------------------------------------


def test_import_batch_round_trip_with_default_status(session: Session) -> None:
    u = _make_user(session)
    a = _make_account(session, u)
    batch = ImportBatch(
        user_id=u.id,
        account_id=a.id,
        source_file_hash="a" * 64,
        parser_name="axis_cc",
    )
    session.add(batch)
    session.commit()
    fetched = session.scalars(select(ImportBatch)).one()
    assert fetched.status == "pending"
    assert fetched.imported_count == 0
    assert fetched.skipped_count == 0
