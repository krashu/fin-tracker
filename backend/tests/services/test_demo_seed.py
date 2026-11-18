"""Service-level tests for the direct-DB demo seeder (app.services.demo_seed).

Exercises :func:`seed_demo_data` against an in-memory session — no TestClient,
no lifespan. The lifespan wiring itself is covered separately in
``tests/test_startup_seed.py``.

Locks the behaviours the frontend/demo depend on: correct row counts, labels
written from the shared dataset, PRD §F4 fingerprints unique across seeded rows,
F3 rule rows written for spend/refund only (not income), and — the point of
the rolling-window redesign — that calling the seeder again with a later
``clock.today()`` rolls the transaction window forward instead of accumulating.
``clock.today`` is frozen for every test in this file so the generated dataset
(and its expected counts) are deterministic.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import clock
from app.core.config import get_settings
from app.core.demo_data import (
    ACCOUNTS,
    INSTRUMENTS,
    build_demo_card_bills,
    build_demo_dataset,
    build_demo_transfers,
)
from app.models import (
    Account,
    Category,
    FxRateQuote,
    Instrument,
    InvestmentTransaction,
    Label,
    MerchantLabelMap,
    MerchantTagMap,
    RealisedLot,
    Transaction,
    User,
)
from app.services.category_service import CASHBACK_CATEGORY_NAME
from app.services.demo_seed import seed_demo_data
from app.services.fx_service import latest_rate
from app.services.holdings_service import compute_holdings
from app.services.merchant_labels import LABEL_PREFILL_MIN
from app.services.provisioning import provision_default_categories
from app.services.reconciliation_service import (
    card_bill_link_offered_ids,
    is_card_bill_debit,
    is_cc_payment,
    link_candidates,
    pair_existing_rows,
)
from app.services.refund_link import refund_link_problem

# Fixed anchor so the generated dataset (and its expected counts) is
# deterministic regardless of when the suite runs. Day 20 is comfortably mid-
# month so the current-month truncation includes most of that slot's rows.
_ANCHOR = date(2026, 8, 20)
_SPENDS, _REFUNDS, _INCOME = build_demo_dataset(_ANCHOR)
_BILLS = build_demo_card_bills(_ANCHOR)
_TXN_TOTAL = (
    len(_SPENDS)
    + len(_REFUNDS)
    + len(_INCOME)
    + 2 * len(build_demo_transfers(_ANCHOR))
    + 2 * len(_BILLS)
)
_INV_TXN_TOTAL = sum(len(spec["txns"]) for spec in INSTRUMENTS)


@pytest.fixture(autouse=True)
def _frozen_anchor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Freeze ``clock.today()`` to ``_ANCHOR`` for every test in this module —
    ``seed_demo_data`` calls it internally, and must agree with the expected
    dataset computed above."""
    monkeypatch.setattr(clock, "today", lambda: _ANCHOR)


@pytest.fixture
def seeded_user(session: Session) -> User:
    """User + default categories — the runtime state right after migrations on a
    fresh DB (categories come from the same provisioning source the seeder reads)."""
    user = User(id=get_settings().v1_user_id)
    session.add(user)
    session.flush()  # user row must exist before its categories' FK
    provision_default_categories(session, user.id)
    session.commit()
    return user


def test_seed_creates_expected_counts(session: Session, seeded_user: User) -> None:
    counts = seed_demo_data(session, user_id=seeded_user.id)

    assert counts.accounts == len(ACCOUNTS)
    assert counts.transactions == _TXN_TOTAL
    assert counts.instruments == len(INSTRUMENTS)
    assert counts.investment_transactions == _INV_TXN_TOTAL

    assert session.scalar(select(func.count()).select_from(Account)) == len(ACCOUNTS)
    assert session.scalar(select(func.count()).select_from(Transaction)) == _TXN_TOTAL
    assert session.scalar(select(func.count()).select_from(Instrument)) == len(INSTRUMENTS)
    assert session.scalar(select(func.count()).select_from(InvestmentTransaction)) == _INV_TXN_TOTAL


def test_seed_writes_labels(session: Session, seeded_user: User) -> None:
    seed_demo_data(session, user_id=seeded_user.id)

    # A spend row with labels from the dataset (MakeMyTrip → #travel #goa).
    trip = session.scalar(select(Transaction).where(Transaction.merchant_raw == "MakeMyTrip"))
    assert trip is not None
    assert {lab.name for lab in trip.labels} == {"travel", "goa"}

    # A refund row carries its labels too.
    refund = session.scalar(select(Transaction).where(Transaction.merchant_raw == "Myntra refund"))
    assert refund is not None
    assert {lab.name for lab in refund.labels} == {"festive"}

    # An unlabeled row has no links.
    metro = session.scalar(select(Transaction).where(Transaction.merchant_raw == "Metro"))
    assert metro is not None
    assert list(metro.labels) == []

    # Investment txn note still round-trips (investments keep their free-text note).
    inv_note = session.scalar(
        select(InvestmentTransaction.note)
        .join(Instrument, Instrument.id == InvestmentTransaction.instrument_id)
        .where(Instrument.symbol == "INFY", InvestmentTransaction.transaction_type == "sell")
    )
    assert inv_note == "Booked partial profit"


def test_seed_cashback_posts_to_the_card_not_the_bank(session: Session, seeded_user: User) -> None:
    """Card cashback is credited on the card statement itself (real Axis
    Flipkart behaviour), not the linked bank account."""
    seed_demo_data(session, user_id=seeded_user.id)

    cashback = session.scalars(
        select(Transaction).where(Transaction.merchant_raw == "Card Cashback")
    ).all()
    assert cashback  # dataset seeds at least one
    card = session.scalar(select(Account).where(Account.name == "Axis Flipkart"))
    assert card is not None
    assert all(txn.account_id == card.id for txn in cashback)


def test_seed_cashback_is_positive_spend_in_the_cashback_category(
    session: Session, seeded_user: User
) -> None:
    """Demo cashback is a rebate: a positive ``spend`` in the spend "Cashback"
    category, never income (ADR-0013)."""
    seed_demo_data(session, user_id=seeded_user.id)

    cashback_cat = session.scalar(
        select(Category).where(
            Category.user_id == seeded_user.id,
            Category.name == CASHBACK_CATEGORY_NAME,
            Category.kind == "spend",
        )
    )
    assert cashback_cat is not None
    cashback = session.scalars(
        select(Transaction).where(Transaction.merchant_raw == "Card Cashback")
    ).all()
    assert cashback
    assert all(txn.transaction_type == "spend" for txn in cashback)
    assert all(txn.amount_paise > 0 for txn in cashback)
    assert all(txn.category_id == cashback_cat.id for txn in cashback)


def test_seed_fingerprints_unique_and_present(session: Session, seeded_user: User) -> None:
    seed_demo_data(session, user_id=seeded_user.id)

    fingerprints = list(session.scalars(select(Transaction.fingerprint)))
    assert len(fingerprints) == _TXN_TOTAL
    assert all(fp is not None for fp in fingerprints)
    assert len(set(fingerprints)) == _TXN_TOTAL  # no collisions across the dataset


def test_seed_records_tags_for_spend_refund_only(session: Session, seeded_user: User) -> None:
    seed_demo_data(session, user_id=seeded_user.id)

    tagged = set(session.scalars(select(MerchantTagMap.merchant_normalized)))
    assert tagged  # F3 learning populated from spend/refund rows
    # Income merchants must NOT feed the spend→category map (AUTO_TAGGABLE_TYPES).
    assert not any("acme payroll" in m for m in tagged)


def test_seed_records_labels_for_prefill(session: Session, seeded_user: User) -> None:
    """demo_seed must learn merchant→label (not just link labels), else the shipped
    F3a Phase-2 prefill never fires in the 'Try the demo' account. Swiggy is seeded
    with #online many times (≥ LABEL_PREFILL_MIN) across the rolling window, so its
    map row must clear the prefill bar."""
    seed_demo_data(session, user_id=seeded_user.id)

    online = session.scalar(select(Label).where(Label.name == "online"))
    assert online is not None
    rows = list(
        session.scalars(select(MerchantLabelMap).where(MerchantLabelMap.label_id == online.id))
    )
    assert rows, "merchant→label map not populated by the seeder"
    assert max(r.hit_count for r in rows) >= LABEL_PREFILL_MIN


def test_seed_is_find_or_create_for_accounts(session: Session, seeded_user: User) -> None:
    """Accounts are matched by name so a pre-existing demo account isn't duplicated."""
    session.add(
        Account(
            user_id=seeded_user.id,
            name="Axis Flipkart",
            type="credit_card",
            issuer="axis",
            last4="4321",
            currency="INR",
        )
    )
    session.commit()

    counts = seed_demo_data(session, user_id=seeded_user.id)

    assert counts.accounts == len(ACCOUNTS)  # reused Axis Flipkart, created the rest
    axis_rows = session.scalar(
        select(func.count()).select_from(Account).where(Account.name == "Axis Flipkart")
    )
    assert axis_rows == 1


def test_seed_same_day_rerun_does_not_duplicate(session: Session, seeded_user: User) -> None:
    """Calling the seeder twice with an unchanged ``clock.today()`` must land on
    the same transaction count, not double it — the wipe-and-regenerate in
    ``seed_demo_data`` is what replaces the old empty-DB-only gate."""
    first = seed_demo_data(session, user_id=seeded_user.id)
    second = seed_demo_data(session, user_id=seeded_user.id)

    assert second.accounts == first.accounts == len(ACCOUNTS)
    assert second.transactions == first.transactions == _TXN_TOTAL
    assert session.scalar(select(func.count()).select_from(Transaction)) == _TXN_TOTAL


def test_seed_rolls_the_window_forward(
    session: Session, seeded_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A boot a month later must replace the window, not accumulate on top of
    it: the oldest transaction moves forward and the total matches a fresh
    ``build_demo_dataset`` at the new anchor, not the old total plus the new one."""
    seed_demo_data(session, user_id=seeded_user.id)
    oldest_before = session.scalar(select(func.min(Transaction.date)))
    assert oldest_before is not None

    later_anchor = date(2026, 9, 20)
    monkeypatch.setattr(clock, "today", lambda: later_anchor)
    expected_spends, expected_refunds, expected_income = build_demo_dataset(later_anchor)
    expected_total = (
        len(expected_spends)
        + len(expected_refunds)
        + len(expected_income)
        + 2 * len(build_demo_transfers(later_anchor))
        + 2 * len(build_demo_card_bills(later_anchor))
    )

    counts = seed_demo_data(session, user_id=seeded_user.id)

    assert counts.transactions == expected_total
    assert session.scalar(select(func.count()).select_from(Transaction)) == expected_total
    oldest_after = session.scalar(select(func.min(Transaction.date)))
    assert oldest_after is not None
    assert oldest_after > oldest_before  # window rolled forward, not accumulated


def test_reseed_clears_a_refund_link_into_the_wiped_rows(
    session: Session, seeded_user: User
) -> None:
    """ADR-0009 rule 13: the self-FK ``refund_of_id`` is ON DELETE NO ACTION, so the
    wipe nulls any refund pointing into the wiped rows first — here one the user keeps
    on an account the seeder never touches. The refund survives, unlinked."""
    seed_demo_data(session, user_id=seeded_user.id)
    original = session.scalars(
        select(Transaction).where(
            Transaction.user_id == seeded_user.id,
            Transaction.transaction_type == "spend",
            Transaction.amount_paise < 0,
        )
    ).first()
    assert original is not None
    mine = Account(user_id=seeded_user.id, name="Mine", type="bank", currency="INR")
    session.add(mine)
    session.flush()
    refund = Transaction(
        user_id=seeded_user.id,
        account_id=mine.id,
        date=_ANCHOR,
        amount_paise=1000,
        transaction_type="spend",
        merchant_normalized="shop",
        fingerprint="f" * 64,
        source="manual",
        refund_of_id=original.id,
    )
    session.add(refund)
    session.commit()

    seed_demo_data(session, user_id=seeded_user.id)

    session.expire_all()
    survivor = session.get(Transaction, refund.id)
    assert survivor is not None
    assert survivor.refund_of_id is None


def test_reseed_wipes_a_linked_pair_with_a_pre_link_category(
    session: Session, seeded_user: User
) -> None:
    """ADR-0002 rule 5: a pair inside the wiped set goes in the shared delete's one statement, so
    neither leg is released and nothing trips CHECK ``pre_link_category_while_linked``."""
    seed_demo_data(session, user_id=seeded_user.id)
    a, b = session.scalars(
        select(Transaction)
        .where(Transaction.user_id == seeded_user.id, Transaction.category_id.is_not(None))
        .order_by(Transaction.id)
    ).all()[:2]
    saved = a.category_id
    a.transaction_type = b.transaction_type = "transfer"
    a.transfer_pair_id, b.transfer_pair_id = b.id, a.id
    a.pre_link_category_id, a.category_id = saved, None
    session.commit()

    seed_demo_data(session, user_id=seeded_user.id)

    # Ids may be reused by the regenerated rows, so assert on the link columns instead:
    # the only linked rows left are the seeded ATM and card-bill pairs, none carrying a
    # pre-link category.
    linked = session.scalars(
        select(Transaction).where(
            Transaction.user_id == seeded_user.id, Transaction.transfer_pair_id.is_not(None)
        )
    ).all()
    linked_bills = [bill for bill in _BILLS if bill.linked]
    assert len(linked) == 2 * (len(build_demo_transfers(_ANCHOR)) + len(linked_bills))
    assert all(t.pre_link_category_id is None for t in linked)


def test_reseed_releases_a_partner_outside_the_wiped_rows(
    session: Session, seeded_user: User
) -> None:
    """ADR-0002 rules 5 and 9: a kept row on an account the seeder never touches, linked to a
    demo row, gets its pre-link type and category back when the demo row is wiped."""
    seed_demo_data(session, user_id=seeded_user.id)
    demo = session.scalars(
        select(Transaction)
        .where(
            Transaction.user_id == seeded_user.id,
            Transaction.transaction_type == "spend",
            Transaction.amount_paise < 0,
            Transaction.category_id.is_not(None),
        )
        .order_by(Transaction.id)
    ).first()
    assert demo is not None
    income_cat = session.scalar(
        select(Category.id).where(Category.user_id == seeded_user.id, Category.kind == "income")
    )
    assert income_cat is not None
    mine = Account(user_id=seeded_user.id, name="Mine", type="bank", currency="INR")
    session.add(mine)
    session.flush()
    kept = Transaction(
        user_id=seeded_user.id,
        account_id=mine.id,
        date=_ANCHOR,
        amount_paise=-demo.amount_paise,
        transaction_type="income",
        category_id=income_cat,
        merchant_normalized="employer",
        fingerprint="k" * 64,
        source="manual",
        confirmed_at=clock.naive_utcnow(),
    )
    session.add(kept)
    session.flush()
    pair_existing_rows(session, kept, demo)
    session.commit()

    seed_demo_data(session, user_id=seeded_user.id)
    session.expire_all()

    assert (
        kept.transfer_pair_id,
        kept.transaction_type,
        kept.category_id,
        kept.pre_link_type,
        kept.pre_link_category_id,
    ) == (None, "income", income_cat, None, None)


def test_seeder_never_calls_learn_merchant_memory(
    session: Session, seeded_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0004 rule 12: seeding writes rule rows directly and never teaches."""
    from app.services import demo_seed, merchant_labels, tag_service

    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("the seeder must not teach")

    monkeypatch.setattr(merchant_labels, "learn_merchant_memory", boom)
    monkeypatch.setattr(merchant_labels, "record_label", boom)
    monkeypatch.setattr(merchant_labels, "record_tag", boom)
    monkeypatch.setattr(tag_service, "record_tag", boom)

    seed_demo_data(session, user_id=seeded_user.id)

    assert not hasattr(demo_seed, "learn_merchant_memory")
    tagged = set(session.scalars(select(MerchantTagMap.merchant_normalized)))
    assert tagged
    assert not any("acme payroll" in m for m in tagged)
    online = session.scalar(select(Label).where(Label.name == "online"))
    assert online is not None
    label_rows = session.scalars(
        select(MerchantLabelMap).where(MerchantLabelMap.label_id == online.id)
    ).all()
    assert max(r.hit_count for r in label_rows) >= LABEL_PREFILL_MIN


def _rule_snapshot(
    session: Session,
) -> tuple[dict[tuple[str, int], int], dict[tuple[str, int], int]]:
    session.expire_all()
    tags = {
        (r.merchant_normalized, r.category_id): r.hit_count
        for r in session.scalars(select(MerchantTagMap))
    }
    labels = {
        (r.merchant_normalized, r.label_id): r.hit_count
        for r in session.scalars(select(MerchantLabelMap))
    }
    return tags, labels


def test_reseed_does_not_inflate_rule_hit_counts(session: Session, seeded_user: User) -> None:
    seed_demo_data(session, user_id=seeded_user.id)
    session.commit()
    first = _rule_snapshot(session)
    assert first[0] and first[1]

    seed_demo_data(session, user_id=seeded_user.id)
    session.commit()

    assert _rule_snapshot(session) == first


def test_seed_never_lowers_or_unpins_an_existing_rule(session: Session, seeded_user: User) -> None:
    """A pre-existing row above the dataset count keeps its count; pinned stays pinned."""
    seed_demo_data(session, user_id=seeded_user.id)
    session.commit()
    row = session.scalars(select(MerchantTagMap).order_by(MerchantTagMap.id)).first()
    assert row is not None
    row.hit_count += 40
    row.pinned = True
    session.commit()
    row_id, bumped = row.id, row.hit_count

    seed_demo_data(session, user_id=seeded_user.id)
    session.commit()

    session.expire_all()
    after = session.get(MerchantTagMap, row_id)
    assert after is not None
    assert after.hit_count == bumped
    assert after.pinned is True


def test_every_dataset_category_exists_for_the_user(session: Session, seeded_user: User) -> None:
    """The seeder silently files an unknown category name under "Other"; a typo in
    app.core.demo_data would hide there, so check every name against the provisioned tree."""
    spends, refunds, income = build_demo_dataset(_ANCHOR)
    names = {
        (c.kind, c.name)
        for c in session.scalars(
            select(Category).where(
                Category.user_id == seeded_user.id, Category.archived_at.is_(None)
            )
        )
    }
    for row in (*spends, *refunds):
        assert ("spend", row.category) in names, row
    for row in income:
        assert ("income", row.category) in names, row


def test_cards_are_paid_from_the_bank(session: Session, seeded_user: User) -> None:
    seed_demo_data(session, user_id=seeded_user.id)

    by_name = {a.name: a for a in session.scalars(select(Account))}
    bank = by_name["HDFC Savings"]
    for card in ("Axis Flipkart", "ICICI Amazon Pay"):
        assert by_name[card].parent_account_id == bank.id
    assert by_name["Cash wallet"].parent_account_id is None


def test_reseed_links_an_existing_card_to_the_bank(session: Session, seeded_user: User) -> None:
    """A DB seeded before cards had a paying bank picks the link up on the next boot."""
    session.add(
        Account(user_id=seeded_user.id, name="Axis Flipkart", type="credit_card", currency="INR")
    )
    session.commit()

    seed_demo_data(session, user_id=seeded_user.id)

    axis = session.scalar(select(Account).where(Account.name == "Axis Flipkart"))
    bank = session.scalar(select(Account).where(Account.name == "HDFC Savings"))
    assert axis is not None and bank is not None
    assert axis.parent_account_id == bank.id


def test_atm_withdrawals_are_linked_transfer_pairs(session: Session, seeded_user: User) -> None:
    """ADR-0002: each withdrawal is two ``transfer`` rows, no category, equal and
    opposite, each pointing at the other."""
    seed_demo_data(session, user_id=seeded_user.id)

    legs = {
        t.id: t
        for t in session.scalars(
            select(Transaction).where(
                Transaction.transaction_type == "transfer",
                Transaction.merchant_raw.startswith("Transfer "),
            )
        )
    }
    assert len(legs) == 2 * len(build_demo_transfers(_ANCHOR))
    by_name = {a.name: a.id for a in session.scalars(select(Account))}
    for leg in legs.values():
        partner = legs[leg.transfer_pair_id]
        assert partner.transfer_pair_id == leg.id
        assert leg.amount_paise == -partner.amount_paise
        assert leg.date == partner.date
        assert leg.category_id is None
        if leg.amount_paise < 0:
            assert (leg.account_id, partner.account_id) == (
                by_name["HDFC Savings"],
                by_name["Cash wallet"],
            )
            assert leg.merchant_raw == "Transfer to Cash wallet"


def _bill_legs(session: Session) -> list[Transaction]:
    return list(
        session.scalars(
            select(Transaction).where(
                Transaction.merchant_raw.startswith("Credit Card Payment")
                | (Transaction.merchant_raw == "Payment Received - Thank You")
            )
        )
    )


def test_card_bills_are_linked_pairs_from_the_bank(session: Session, seeded_user: User) -> None:
    """ADR-0002: each paid bill is the bank's card-bill debit and the card's payment
    credit, both ``transfer``, uncategorised, equal and opposite, each pointing at the
    other, the bank leg on the card's "Paid from" account. Each leg matches the F4a
    vocabulary for its side, and remembers it was a ``transfer`` before the link (the
    shape a statement import commits, rules 7b and 7c), so an unlink keeps both out of
    spend and income."""
    seed_demo_data(session, user_id=seeded_user.id)

    legs = {t.id: t for t in _bill_legs(session)}
    linked = [t for t in legs.values() if t.transfer_pair_id is not None]
    assert len(linked) == 2 * sum(bill.linked for bill in _BILLS)
    cards = {a.id: a for a in session.scalars(select(Account).where(Account.type == "credit_card"))}
    for leg in linked:
        partner = legs[leg.transfer_pair_id]
        assert partner.transfer_pair_id == leg.id
        assert (leg.transaction_type, leg.pre_link_type) == ("transfer", "transfer")
        assert leg.category_id is None and leg.pre_link_category_id is None
        assert leg.amount_paise == -partner.amount_paise
        assert leg.date == partner.date
        if leg.amount_paise > 0:
            assert is_cc_payment(leg.merchant_normalized)
            assert partner.account_id == cards[leg.account_id].parent_account_id
            assert is_card_bill_debit(partner.merchant_normalized)


def test_card_bill_amounts_are_the_previous_months_card_spend(
    session: Session, seeded_user: User
) -> None:
    """A card's balance returns to just its current month's spend after each bill: the
    bill pays exactly the previous calendar month, refunds and cashback netted off."""
    seed_demo_data(session, user_id=seeded_user.id)

    cards = session.scalars(select(Account).where(Account.type == "credit_card")).all()
    for card in cards:
        for credit in session.scalars(
            select(Transaction).where(
                Transaction.account_id == card.id,
                Transaction.merchant_raw == "Payment Received - Thank You",
            )
        ):
            month_start = credit.date.replace(day=1)
            prev_start = (month_start - timedelta(days=1)).replace(day=1)
            spent = session.scalar(
                select(func.sum(Transaction.amount_paise)).where(
                    Transaction.account_id == card.id,
                    Transaction.transaction_type == "spend",
                    Transaction.date >= prev_start,
                    Transaction.date < month_start,
                )
            )
            assert credit.amount_paise == -spent


def test_the_second_cards_latest_bill_is_left_to_link_by_hand(
    session: Session, seeded_user: User
) -> None:
    """ADR-0002 rule 11b: one bill is seeded unlinked, so the manual link has something
    to show. The dialog offers the link on its card leg, and the bank leg is the one
    candidate."""
    seed_demo_data(session, user_id=seeded_user.id)

    unlinked = [t for t in _bill_legs(session) if t.transfer_pair_id is None]
    assert len(unlinked) == 2
    credit, debit = sorted(unlinked, key=lambda t: -t.amount_paise)
    card = session.scalar(select(Account).where(Account.name == "ICICI Amazon Pay"))
    assert card is not None
    assert credit.account_id == card.id
    assert credit.date == max(t.date for t in _bill_legs(session) if t.account_id == card.id)
    assert card_bill_link_offered_ids(session, user_id=seeded_user.id, txns=[credit]) == {credit.id}
    assert link_candidates(session, user_id=seeded_user.id, txn=credit) == [debit]


def test_card_bill_legs_feed_no_tag_rule(session: Session, seeded_user: User) -> None:
    """A transfer is never learned from (``should_learn_tag``), so no rule ever
    auto-categorises a later card payment."""
    seed_demo_data(session, user_id=seeded_user.id)

    learned = set(session.scalars(select(MerchantTagMap.merchant_normalized)))
    assert not any(is_card_bill_debit(m) or is_cc_payment(m) for m in learned)


def test_refunds_link_to_the_spend_they_reverse(session: Session, seeded_user: User) -> None:
    """ADR-0009 rule 13: every dataset refund points at a valid original, an earlier
    spend on the same card in the same category; cashback reverses nothing."""
    seed_demo_data(session, user_id=seeded_user.id)

    refunds = session.scalars(
        select(Transaction).where(
            Transaction.transaction_type == "spend", Transaction.amount_paise > 0
        )
    ).all()
    linked = [r for r in refunds if r.refund_of_id is not None]
    assert len(linked) == sum(row.refund_of is not None for row in _REFUNDS) > 0
    for refund in linked:
        original = session.get(Transaction, refund.refund_of_id)
        assert original is not None
        assert refund_link_problem(refund, original) is None
        assert original.account_id == refund.account_id
        assert original.category_id == refund.category_id
        assert original.date <= refund.date


def _usd_specs() -> list[dict]:
    return [spec for spec in INSTRUMENTS if spec.get("currency") == "USD"]


def test_usd_instruments_stamp_each_rows_literal_rate(session: Session, seeded_user: User) -> None:
    """ADR-0016: each USD row carries the rate for its own date, from the dataset; INR
    rows stay exactly 1."""
    seed_demo_data(session, user_id=seeded_user.id)

    assert {spec["asset_class"] for spec in _usd_specs()} == {"us_equity", "us_etf"}
    for spec in INSTRUMENTS:
        instrument = session.scalar(select(Instrument).where(Instrument.symbol == spec["symbol"]))
        assert instrument is not None
        assert instrument.currency == spec.get("currency", "INR")
        stamps = [
            t.fx_rate_to_inr
            for t in session.scalars(
                select(InvestmentTransaction)
                .where(InvestmentTransaction.instrument_id == instrument.id)
                .order_by(InvestmentTransaction.id)
            )
        ]
        assert stamps == [Decimal(str(t.get("fx", "1"))) for t in spec["txns"]]


def test_reseed_finds_the_usd_instruments_instead_of_recreating_them(
    session: Session, seeded_user: User
) -> None:
    """The instrument lookup matches the spec's currency, so a second boot finds AAPL
    rather than creating it again beside the first."""
    seed_demo_data(session, user_id=seeded_user.id)
    session.commit()

    again = seed_demo_data(session, user_id=seeded_user.id)
    session.commit()

    assert (again.instruments, again.investment_transactions) == (0, 0)
    assert session.scalar(select(func.count()).select_from(Instrument)) == len(INSTRUMENTS)


def test_partial_sells_book_fifo_lots_in_both_currencies(
    session: Session, seeded_user: User
) -> None:
    """ADR-0015: HDFC Bank's sell and Apple's each span two buy lots."""
    seed_demo_data(session, user_id=seeded_user.id)

    rows = session.execute(
        select(Instrument.symbol, func.count(RealisedLot.id))
        .select_from(RealisedLot)
        .join(InvestmentTransaction, InvestmentTransaction.id == RealisedLot.sell_txn_id)
        .join(Instrument, Instrument.id == InvestmentTransaction.instrument_id)
        .group_by(Instrument.symbol)
    ).all()
    lots_by_symbol = {symbol: count for symbol, count in rows}
    assert lots_by_symbol["HDFCBANK"] == 2
    assert lots_by_symbol["AAPL"] == 2  # 3 units from the first buy, 1 from the second


def test_seed_writes_fx_rates(session: Session, seeded_user: User) -> None:
    """Demo seed populates fx_rates for today and demo USD transactions, so USD holdings
    value in INR and have %Alloc and XIRR computed in demo mode."""
    seed_demo_data(session, user_id=seeded_user.id)

    assert session.scalar(select(func.count()).select_from(FxRateQuote)) > 0
    usd_rate = latest_rate(session)
    assert usd_rate is not None
    usd = [
        h
        for h in compute_holdings(session, user_id=seeded_user.id, usd_inr_rate=usd_rate)
        if h.currency == "USD"
    ]
    assert {h.symbol for h in usd} == {spec["symbol"] for spec in _usd_specs()}
    assert all(h.current_value_inr_paise is not None for h in usd)


def test_usd_holdings_value_in_inr_once_a_rate_is_cached(
    session: Session, seeded_user: User
) -> None:
    session.add(
        FxRateQuote(
            date=date(2026, 1, 2),
            from_currency="USD",
            to_currency="INR",
            rate=Decimal("90.12"),
            source="frankfurter",
        )
    )
    session.commit()

    seed_demo_data(session, user_id=seeded_user.id)

    holdings = compute_holdings(session, user_id=seeded_user.id, usd_inr_rate=latest_rate(session))
    usd = [h for h in holdings if h.currency == "USD"]
    assert usd
    assert all(h.current_value_inr_paise is not None for h in usd)


def test_seed_reads_today_once(
    session: Session, seeded_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A seed spanning midnight at a month's end must build its spends, transfers, bills
    and instruments for one window, so the date is read once."""
    calls: list[date] = []

    def today() -> date:
        calls.append(_ANCHOR)
        return _ANCHOR

    monkeypatch.setattr(clock, "today", today)
    seed_demo_data(session, user_id=seeded_user.id)
    assert len(calls) == 1
