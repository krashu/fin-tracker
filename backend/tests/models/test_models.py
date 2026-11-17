"""Round-trip + constraint tests for the persistence-layer models.

Goals:

* Verify every model can round-trip a representative row through SQLite.
* Verify the dedup, uniqueness, and FK constraints declared in the schema
  actually fire — these are the contracts the future ``import_service`` will
  rely on, so they're worth asserting at the model layer.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from app.models import (
    Account,
    Category,
    ImportBatch,
    Instrument,
    InvestmentTransaction,
    MerchantAlias,
    MerchantLabelMap,
    MerchantTagMap,
    RealisedLot,
    Transaction,
    User,
)
from app.services.merchant import MERCHANT_MAX_CHARS

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
    c = Category(color="#2a78d6", user_id=user.id, name=name)
    session.add(c)
    session.flush()
    return c


def test_every_merchant_column_is_merchant_max_chars_wide() -> None:
    """ADR-0006 rule 3b: the write boundaries refuse past MERCHANT_MAX_CHARS, so every merchant
    column must be exactly that wide (models spell the literal; they do not import services)."""
    columns = [
        Transaction.__table__.c.merchant_raw,
        Transaction.__table__.c.merchant_normalized,
        Transaction.__table__.c.origin_merchant_normalized,
        MerchantTagMap.__table__.c.merchant_normalized,
        MerchantLabelMap.__table__.c.merchant_normalized,
        MerchantAlias.__table__.c.pattern,
        MerchantAlias.__table__.c.canonical,
    ]

    assert [c.type.length for c in columns] == [MERCHANT_MAX_CHARS] * len(columns)


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


def test_a_root_category_needs_a_color(session: Session) -> None:
    """ADR-0012 rule 7: ``ck_categories_root_has_color`` refuses a colourless root, and
    leaves an inheriting subcategory (``color IS NULL``) alone."""
    user = _make_user(session)
    session.add(Category(user_id=user.id, name="X"))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()

    user = _make_user(session)
    root = Category(user_id=user.id, name="Food", color="#2a78d6")
    session.add(root)
    session.flush()
    session.add(Category(user_id=user.id, name="Coffee", parent_id=root.id))
    session.flush()


def test_category_unique_name_per_user(session: Session) -> None:
    user = _make_user(session)
    _make_category(session, user, "Food")
    session.commit()
    session.add(Category(color="#2a78d6", user_id=user.id, name="Food"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_category_same_name_different_user_ok(session: Session) -> None:
    u1 = _make_user(session, email="a@x")
    u2 = _make_user(session, email="b@x")
    session.add_all(
        [
            Category(color="#2a78d6", user_id=u1.id, name="Food"),
            Category(color="#2a78d6", user_id=u2.id, name="Food"),
        ]
    )
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
            Category(color="#2a78d6", user_id=user.id, name="Other", kind="spend"),
            Category(color="#2a78d6", user_id=user.id, name="Other", kind="income"),
        ]
    )
    session.commit()
    rows = session.scalars(select(Category).where(Category.name == "Other")).all()
    assert {r.kind for r in rows} == {"spend", "income"}


def test_category_same_name_same_kind_raises(session: Session) -> None:
    user = _make_user(session)
    session.add(Category(color="#2a78d6", user_id=user.id, name="Salary", kind="income"))
    session.commit()
    session.add(Category(color="#2a78d6", user_id=user.id, name="Salary", kind="income"))
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


def test_paired_non_transfer_rejected(session: Session) -> None:
    """ADR-0002 rule 8: CHECK ``paired_is_transfer`` — a row carrying a
    ``transfer_pair_id`` must be a ``transfer``; the type is what excludes it from sums."""
    u = _make_user(session)
    bank = _make_account(session, u, name="bank", type="bank", last4=None)
    spend = _make_transaction(session, u, bank, fingerprint=_fingerprint("s"))
    partner = _make_transaction(
        session, u, bank, fingerprint=_fingerprint("t"), amount_paise=45000, txn_type="transfer"
    )
    session.commit()
    with pytest.raises(IntegrityError, match="paired_is_transfer"):
        session.execute(
            text("UPDATE transactions SET transfer_pair_id = :t WHERE id = :s"),
            {"t": partner.id, "s": spend.id},
        )
        session.commit()


def _paired_uncategorised_leg(session: Session, u: User) -> Transaction:
    """Two paired transfer legs on one user; returns the first, uncategorised."""
    bank = _make_account(session, u, name="bank", type="bank", last4=None)
    cc = _make_account(session, u, name="cc", type="credit_card")
    a = _make_transaction(
        session, u, bank, fingerprint=_fingerprint("pa"), amount_paise=-5000, txn_type="transfer"
    )
    b = _make_transaction(
        session, u, cc, fingerprint=_fingerprint("pb"), amount_paise=5000, txn_type="transfer"
    )
    a.transfer_pair_id = b.id
    b.transfer_pair_id = a.id
    session.commit()
    return a


@pytest.mark.parametrize("shape", ["unpaired", "categorised"])
def test_pre_link_category_needs_a_linked_uncategorised_row(session: Session, shape: str) -> None:
    """ADR-0002 rule 9: CHECK ``pre_link_category_while_linked`` — a saved pre-link category
    exists only on a paired row whose own category is NULL (a category set after linking
    discards it)."""
    u = _make_user(session)
    cat = _make_category(session, u, name="Food")
    if shape == "unpaired":
        bank = _make_account(session, u, name="bank", type="bank", last4=None)
        txn = _make_transaction(session, u, bank, fingerprint=_fingerprint("u"))
    else:
        txn = _paired_uncategorised_leg(session, u)
        txn.category_id = cat.id
    session.commit()
    with pytest.raises(IntegrityError, match="pre_link_category_while_linked"):
        session.execute(
            text("UPDATE transactions SET pre_link_category_id = :c WHERE id = :t"),
            {"c": cat.id, "t": txn.id},
        )
        session.commit()
    session.rollback()


def test_pre_link_category_on_a_linked_uncategorised_row_is_allowed(session: Session) -> None:
    u = _make_user(session)
    cat = _make_category(session, u, name="Food")
    leg = _paired_uncategorised_leg(session, u)
    leg.pre_link_category_id = cat.id
    session.commit()
    assert session.get(Transaction, leg.id).pre_link_category_id == cat.id  # ty: ignore[unresolved-attribute]


def test_pre_link_category_id_cross_user_rejected(session: Session) -> None:
    """ADR-0003 rule 10: ``pre_link_category_id`` is a composite same-user FK."""
    u1 = _make_user(session, email="a@example.com")
    u2 = _make_user(session, email="b@example.com")
    theirs = _make_category(session, u2, name="Food")
    leg = _paired_uncategorised_leg(session, u1)
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        session.execute(
            text("UPDATE transactions SET pre_link_category_id = :c WHERE id = :t"),
            {"c": theirs.id, "t": leg.id},
        )
        session.commit()
    session.rollback()


def test_refund_of_id_cross_user_rejected(session: Session) -> None:
    """ADR-0009 rule 13 + ADR-0003 rule 10: the refund → original self-FK is composite
    same-user, so a refund can never point at another user's row."""
    u1 = _make_user(session, email="a@example.com")
    u2 = _make_user(session, email="b@example.com")
    refund = _make_transaction(
        session,
        u1,
        _make_account(session, u1, name="u1-acc"),
        fingerprint=_fingerprint("r"),
        amount_paise=1000,
    )
    theirs = _make_transaction(
        session, u2, _make_account(session, u2, name="u2-acc"), fingerprint=_fingerprint("o")
    )
    session.commit()
    with pytest.raises(IntegrityError):
        session.execute(
            text("UPDATE transactions SET refund_of_id = :o WHERE id = :r"),
            {"o": theirs.id, "r": refund.id},
        )
        session.commit()


def test_hard_deleting_a_parent_category_with_children_is_refused(session: Session) -> None:
    """ADR-0012 rules 1 & 11: ``parent_id`` is ``ON DELETE NO ACTION``, never CASCADE.

    A hard parent delete must fail rather than silently take its subcategories with it;
    whole-user teardown is the only hard delete, and it removes children first.
    """
    u = _make_user(session)
    parent = _make_category(session, u, name="Food")
    child = Category(user_id=u.id, name="Groceries", parent_id=parent.id)
    session.add(child)
    session.commit()

    with pytest.raises(IntegrityError):
        session.execute(text("DELETE FROM categories WHERE id = :id"), {"id": parent.id})
        session.commit()
    session.rollback()


# ---------- InvestmentTransaction -------------------------------------------


def test_investment_fx_rate_has_no_default(session: Session) -> None:
    """ADR-0016 rule 2: ``fx_rate_to_inr`` is server-stamped at the transaction date and
    never defaulted — a writer that forgets to stamp it fails loudly instead of storing 1."""
    column = InvestmentTransaction.__table__.c.fx_rate_to_inr
    assert column.default is None
    assert column.server_default is None

    u = _make_user(session)
    inst = Instrument(
        user_id=u.id,
        symbol="NIFTYBEES",
        name="Nifty BeES",
        asset_class="indian_mf",
        exchange="NSE",
    )
    session.add(inst)
    session.flush()
    session.add(
        InvestmentTransaction(
            user_id=u.id,
            instrument_id=inst.id,
            date=date(2026, 3, 15),
            transaction_type="buy",
            units=Decimal("1"),
            price_per_unit_native=Decimal("100"),
            amount_native_paise=10000,
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()


def _lot_fixture(session: Session) -> tuple[User, InvestmentTransaction, InvestmentTransaction]:
    u = _make_user(session)
    inst = Instrument(
        user_id=u.id, symbol="NIFTYBEES", name="Nifty BeES", asset_class="indian_mf", exchange="NSE"
    )
    session.add(inst)
    session.flush()
    txns = [
        InvestmentTransaction(
            user_id=u.id,
            instrument_id=inst.id,
            date=date(2026, 3, d),
            transaction_type=kind,
            units=Decimal("1"),
            price_per_unit_native=Decimal("100"),
            amount_native_paise=10000,
            fx_rate_to_inr=Decimal("1"),
        )
        for kind, d in (("buy", 1), ("sell", 2))
    ]
    session.add_all(txns)
    session.flush()
    return u, txns[0], txns[1]


def _lot(
    u: User, buy: InvestmentTransaction, sell: InvestmentTransaction, **over: object
) -> RealisedLot:
    fields: dict[str, object] = {
        "user_id": u.id,
        "sell_txn_id": sell.id,
        "buy_txn_id": buy.id,
        "units": Decimal("1"),
        "cost_native_paise": 10000,
        "proceeds_native_paise": 12000,
        "sell_fees_native_paise": 0,
        "buy_date": buy.date,
        "sell_date": sell.date,
        "kind": "fifo",
    }
    fields.update(over)
    return RealisedLot(**fields)


@pytest.mark.parametrize("which", ["sell", "buy"])
def test_realised_lot_cascades_with_its_sell_and_buy(session: Session, which: str) -> None:
    u, buy, sell = _lot_fixture(session)
    session.add(_lot(u, buy, sell))
    session.flush()
    session.delete(sell if which == "sell" else buy)
    session.flush()
    session.expire_all()
    assert session.scalars(select(RealisedLot)).all() == []


@pytest.mark.parametrize(
    "bad",
    [
        {"units": Decimal("0")},
        {"cost_native_paise": -1},
        {"proceeds_native_paise": -1},
        {"sell_fees_native_paise": -1},
        {"buy_date": date(2026, 3, 3)},
        {"kind": "intraday"},  # the fixture's dates differ, so it is not a same-day match
    ],
    ids=["units", "cost", "proceeds", "sell_fees", "dates", "intraday_dates"],
)
def test_realised_lot_checks_reject_bad_rows(session: Session, bad: dict[str, object]) -> None:
    u, buy, sell = _lot_fixture(session)
    session.add(_lot(u, buy, sell, **bad))
    with pytest.raises(IntegrityError):
        session.flush()


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


def test_origin_inputs_are_all_or_none(session: Session) -> None:
    """ADR-0006 rule 17: CHECK ``origin_inputs_all_or_none`` — the four origin inputs are all
    set (with a stored origin hash) or all NULL; ``origin_account_id`` is a same-user FK."""
    u1 = _make_user(session, email="a@example.com")
    u2 = _make_user(session, email="b@example.com")
    bank = _make_account(session, u1, name="bank", type="bank", last4=None)
    theirs = _make_account(session, u2, name="theirs", type="bank", last4=None)
    txn = _make_transaction(session, u1, bank, fingerprint=_fingerprint("a"))
    session.commit()

    all_set = {
        "d": "2026-03-01",
        "p": -100,
        "m": "shop",
        "a": bank.id,
    }

    def _set(sql_set: str, **params: object) -> None:
        session.execute(
            text(f"UPDATE transactions SET {sql_set} WHERE id = :id"), {"id": txn.id, **params}
        )
        session.commit()

    # A partial set of inputs.
    with pytest.raises(IntegrityError, match="origin_inputs_all_or_none"):
        _set("origin_date = :d, origin_fingerprint = 'x'", d=all_set["d"])
    session.rollback()
    # All four inputs, but no stored origin hash.
    with pytest.raises(IntegrityError, match="origin_inputs_all_or_none"):
        _set(
            "origin_date = :d, origin_amount_paise = :p, origin_merchant_normalized = :m, "
            "origin_account_id = :a",
            **all_set,
        )
    session.rollback()
    # Another user's account as the origin account.
    with pytest.raises(IntegrityError):
        _set(
            "origin_fingerprint = 'x', origin_date = :d, origin_amount_paise = :p, "
            "origin_merchant_normalized = :m, origin_account_id = :a",
            **{**all_set, "a": theirs.id},
        )
    session.rollback()
    # The valid shape: all four plus the hash.
    _set(
        "origin_fingerprint = 'x', origin_date = :d, origin_amount_paise = :p, "
        "origin_merchant_normalized = :m, origin_account_id = :a",
        **all_set,
    )


@pytest.mark.parametrize(
    ("asset_class", "currency", "check"),
    [
        ("us_equity", "INR", "usd_only_asset_class"),
        ("us_etf", "INR", "usd_only_asset_class"),
        ("indian_equity", "USD", "inr_only_asset_class"),
        ("indian_mf", "USD", "inr_only_asset_class"),
        ("nps", "USD", "inr_only_asset_class"),
    ],
)
def test_asset_class_currency_check_rejects_a_mismatch(
    session: Session, asset_class: str, currency: str, check: str
) -> None:
    """PRD §F7: us_* are USD and indian_*/nps are INR, held by CHECKs on ``instruments``."""
    u = _make_user(session)
    session.add(
        Instrument(
            user_id=u.id,
            symbol="X",
            name="X",
            asset_class=asset_class,
            currency=currency,
            exchange="OTHER",
        )
    )
    with pytest.raises(IntegrityError, match=check):
        session.flush()
    session.rollback()


@pytest.mark.parametrize("asset_class", ["fd", "bond", "gold", "other"])
def test_unpinned_asset_classes_take_either_currency(session: Session, asset_class: str) -> None:
    u = _make_user(session)
    session.add(
        Instrument(
            user_id=u.id,
            symbol="X",
            name="X",
            asset_class=asset_class,
            currency="USD",
            exchange="OTHER",
        )
    )
    session.flush()
