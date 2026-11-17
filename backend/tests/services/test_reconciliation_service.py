"""Service-level tests for F4a-1 CC-bill auto-link (PRD §F4a-1).

Exercises ``app.services.reconciliation_service.auto_link_cc_bill`` directly
against an in-memory session — no TestClient, no router. Integration through
``POST /imports/{batch_id}/commit`` lives in ``tests/api/test_imports_review.py``.

The composite FK from migration 0005 is the only DB invariant under test
here; cross-user isolation is locked elsewhere (the FK + the query's
``user_id`` filter) and intentionally not re-tested per CLAUDE.md §2.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import structlog
from sqlalchemy import event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Account, Category, ImportBatch, Transaction, User
from app.services.reconciliation_service import (
    auto_link_cc_bill,
    card_bill_link_offered_ids,
    card_bill_transfer_gate,
    is_card_bill_debit,
    reconcile_batch,
    rows_removed_since_import,
    statement_window,
)
from app.services.transaction_queries import confirmed_only
from tests.fk_bypass import foreign_keys_off

# ---------------------------------------------------------------------------
# Local fixtures — mirror tests/services/test_tag_service.py (services tests
# do not pick up the api/conftest.py rig).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Domain helpers.
# ---------------------------------------------------------------------------

_CC_DATE = date(2026, 5, 10)
_CC_AMOUNT = 500_000  # positive on the CC side (payment received)


def _make_bank_and_cc(session: Session, user_id: UUID) -> tuple[Account, Account]:
    """Create an HDFC bank + an Axis CC linked to it. Returns (bank, cc)."""
    bank = Account(
        user_id=user_id,
        name="HDFC Bank",
        type="bank",
        issuer="hdfc",
    )
    session.add(bank)
    session.flush()
    cc = Account(
        user_id=user_id,
        name="Axis CC",
        type="credit_card",
        issuer="axis",
        last4="1234",
        parent_account_id=bank.id,
    )
    session.add(cc)
    session.commit()
    return bank, cc


def _make_cc_payment_row(
    session: Session,
    user_id: UUID,
    cc: Account,
    *,
    merchant: str = "payment received thank you",
    amount_paise: int = _CC_AMOUNT,
    txn_date: date = _CC_DATE,
    transaction_type: str = "income",
    transfer_pair_id: int | None = None,
    fingerprint: str = "fp-cc",
) -> Transaction:
    """A CC-side income/payment row, fresh out of import + confirmed."""
    row = Transaction(
        user_id=user_id,
        account_id=cc.id,
        date=txn_date,
        amount_paise=amount_paise,
        transaction_type=transaction_type,
        merchant_raw=merchant.upper(),
        merchant_normalized=merchant,
        fingerprint=fingerprint,
        source="import",
        confirmed_at=datetime.now(UTC),
        transfer_pair_id=transfer_pair_id,
    )
    session.add(row)
    session.commit()
    return row


def _make_bank_transfer_row(
    session: Session,
    user_id: UUID,
    bank: Account,
    *,
    amount_paise: int = -_CC_AMOUNT,
    txn_date: date = _CC_DATE,
    transaction_type: str = "transfer",
    transfer_pair_id: int | None = None,
    fingerprint: str = "fp-bank",
) -> Transaction:
    """A bank-side row that should be the F4a candidate."""
    row = Transaction(
        user_id=user_id,
        account_id=bank.id,
        date=txn_date,
        amount_paise=amount_paise,
        transaction_type=transaction_type,
        merchant_raw="AXIS CC BILL",
        merchant_normalized="axis cc bill",
        fingerprint=fingerprint,
        source="manual",
        confirmed_at=datetime.now(UTC),
        transfer_pair_id=transfer_pair_id,
    )
    session.add(row)
    session.commit()
    return row


# ---------------------------------------------------------------------------
# Tests.
# ---------------------------------------------------------------------------


def test_auto_link_pairs_a_card_payment_committed_as_a_transfer(
    session: Session, user: User
) -> None:
    """ADR-0002 rule 7c: an uncategorised card payment commits as an unpaired ``transfer``, and
    the auto-link still pairs it with its bank debit, from either side, keeping ``transfer`` as
    the card leg's pre-link type (rule 9)."""
    bank, cc = _make_bank_and_cc(session, user.id)
    bank_row = _make_bank_transfer_row(session, user.id, bank, transaction_type="spend")
    cc_row = _make_cc_payment_row(session, user.id, cc, transaction_type="transfer")

    auto_link_cc_bill(session, user_id=user.id, txn=cc_row)
    session.commit()
    session.expire_all()

    cc_after = session.get(Transaction, cc_row.id)
    bank_after = session.get(Transaction, bank_row.id)
    assert cc_after is not None and bank_after is not None
    assert (cc_after.transfer_pair_id, bank_after.transfer_pair_id) == (bank_after.id, cc_after.id)
    assert cc_after.pre_link_type == "transfer"


@pytest.mark.parametrize(
    "gate",
    ["wrong_type", "merchant_mismatch", "no_parent", "parent_archived", "already_paired"],
)
def test_no_op_on_ineligible_inputs(
    gate: str,
    session: Session,
    user: User,
) -> None:
    """Every gate in steps 1-4 of the algorithm should silently no-op."""
    bank, cc = _make_bank_and_cc(session, user.id)
    # Bank-side candidate exists for the happy path — its presence shouldn't
    # rescue an ineligible CC row.
    bank_row = _make_bank_transfer_row(session, user.id, bank)

    if gate == "wrong_type":
        cc_row = _make_cc_payment_row(
            session, user.id, cc, transaction_type="spend", amount_paise=-_CC_AMOUNT
        )
    elif gate == "merchant_mismatch":
        cc_row = _make_cc_payment_row(session, user.id, cc, merchant="grocery store 123")
    elif gate == "no_parent":
        cc.parent_account_id = None
        session.commit()
        cc_row = _make_cc_payment_row(session, user.id, cc)
    elif gate == "parent_archived":
        bank.archived_at = datetime.now(UTC)
        session.commit()
        cc_row = _make_cc_payment_row(session, user.id, cc)
    elif gate == "already_paired":
        # Already-paired: use a separate dummy partner so the FK is satisfied;
        # we expect step-1 short-circuit to return BEFORE any further checks.
        # A paired row is a transfer (ADR-0002 rule 8, CHECK paired_is_transfer),
        # so since 0038 step 1 is redundant with the step-2 type gate; it stays as
        # the cheap idempotence check it was written as.
        partner = _make_bank_transfer_row(session, user.id, bank, fingerprint="fp-partner")
        cc_row = _make_cc_payment_row(
            session, user.id, cc, transaction_type="transfer", transfer_pair_id=partner.id
        )
        # Mirror-link the partner so the FK invariant from PR-B isn't violated
        # by a stray test fixture.
        partner.transfer_pair_id = cc_row.id
        session.commit()
    else:
        raise AssertionError(f"unknown gate: {gate}")

    auto_link_cc_bill(session, user_id=user.id, txn=cc_row)
    session.expire_all()

    # Bank-side candidate stays unpaired in every gate except already_paired
    # (where the link came from the fixture, not from the service).
    refreshed_bank = session.get(Transaction, bank_row.id)
    assert refreshed_bank is not None
    if gate == "already_paired":
        # Bank-side dummy candidate was untouched; the CC row's pre-existing
        # pair points at a different bank row.
        assert refreshed_bank.transfer_pair_id is None
    else:
        assert refreshed_bank.transfer_pair_id is None
        # Bank-side fixture defaults to type="transfer"; ineligible CC row
        # means no flip, so the bank-side type stays as the fixture set it.
        assert refreshed_bank.transaction_type == "transfer"
        # The CC row was never flipped.
        refreshed_cc = session.get(Transaction, cc_row.id)
        assert refreshed_cc is not None
        if gate == "wrong_type":
            assert refreshed_cc.transaction_type == "spend"
        else:
            assert refreshed_cc.transaction_type == "income"


def test_links_single_matching_bank_row(
    session: Session,
    user: User,
) -> None:
    """Happy path. Symmetric pair_ids + both rows flipped to transfer."""
    bank, cc = _make_bank_and_cc(session, user.id)
    bank_row = _make_bank_transfer_row(session, user.id, bank)
    cc_row = _make_cc_payment_row(session, user.id, cc)

    auto_link_cc_bill(session, user_id=user.id, txn=cc_row)
    session.commit()
    session.expire_all()

    cc_after = session.get(Transaction, cc_row.id)
    bank_after = session.get(Transaction, bank_row.id)
    assert cc_after is not None and bank_after is not None

    # Symmetry — both directions of the pair.
    assert cc_after.transfer_pair_id == bank_after.id
    assert bank_after.transfer_pair_id == cc_after.id
    # Both flipped to transfer.
    assert cc_after.transaction_type == "transfer"
    assert bank_after.transaction_type == "transfer"


@pytest.mark.parametrize("bank_type", ["spend", "transfer"])
def test_auto_link_stores_each_legs_pre_link_type(
    bank_type: str,
    session: Session,
    user: User,
) -> None:
    """ADR-0002 rule 9: linking existing rows records each leg's own pre-link type, so
    unlink can restore it. The bank candidate may already be a ``transfer``; that is
    stored too (non-NULL means "an existing row was linked")."""
    bank, cc = _make_bank_and_cc(session, user.id)
    bank_row = _make_bank_transfer_row(session, user.id, bank, transaction_type=bank_type)
    cc_row = _make_cc_payment_row(session, user.id, cc)

    auto_link_cc_bill(session, user_id=user.id, txn=cc_row)
    session.commit()
    session.expire_all()

    cc_after = session.get(Transaction, cc_row.id)
    bank_after = session.get(Transaction, bank_row.id)
    assert cc_after is not None and bank_after is not None
    assert cc_after.transfer_pair_id == bank_after.id
    assert cc_after.pre_link_type == "income"
    assert bank_after.pre_link_type == bank_type


@pytest.mark.parametrize(
    "candidate_state",
    ["no_candidate", "outside_date_window", "already_paired", "positive_refund_wrong_sign"],
)
def test_no_op_on_no_or_bad_candidate(
    candidate_state: str,
    session: Session,
    user: User,
) -> None:
    """No link when the bank-side candidate is missing or filtered out.

    ``positive_refund_wrong_sign`` is the mis-pair guard of ADR-0009 rule 6: since a
    refund is a ``spend`` row with a positive amount, the type filter alone no
    longer excludes one, so ``auto_link_cc_bill`` states ``amount_paise < 0`` on
    the bank side explicitly. The sign cannot be derived from the CC-side type: a
    NEGATIVE ``income`` row (an income reversal, ADR-0009 rule 2) is an ordinary
    API write, and it flips the target positive — making a same-magnitude positive
    refund a live mis-pair candidate without the guard.
    """
    bank, cc = _make_bank_and_cc(session, user.id)

    if candidate_state == "no_candidate":
        pass  # Nothing on the bank side.
    elif candidate_state == "outside_date_window":
        _make_bank_transfer_row(session, user.id, bank, txn_date=_CC_DATE - timedelta(days=3))
    elif candidate_state == "already_paired":
        # Bank candidate is already linked to some other (dummy) row.
        partner = _make_cc_payment_row(
            session,
            user.id,
            cc,
            fingerprint="fp-dummy-cc",
            merchant="some other payment received",
            # Paired below, and a paired row is a transfer (ADR-0002 rule 8).
            transaction_type="transfer",
        )
        bank_row = _make_bank_transfer_row(session, user.id, bank, transfer_pair_id=partner.id)
        partner.transfer_pair_id = bank_row.id
        session.commit()
    elif candidate_state == "positive_refund_wrong_sign":
        # Bank-side refund (a `spend` row, positive) of the magnitude that
        # WOULD match a NEGATIVE `income` row on the CC side — see the
        # docstring above. Constructed below via a negative-amount CC row.
        _make_bank_transfer_row(
            session,
            user.id,
            bank,
            transaction_type="spend",
            amount_paise=_CC_AMOUNT,
        )
    else:
        raise AssertionError(f"unknown candidate_state: {candidate_state}")

    # `positive_refund_wrong_sign` needs the CC row itself negative — an income
    # reversal, which any write path accepts. Every other branch keeps the
    # ordinary positive-income CC row.
    cc_amount = -_CC_AMOUNT if candidate_state == "positive_refund_wrong_sign" else _CC_AMOUNT
    cc_row = _make_cc_payment_row(session, user.id, cc, amount_paise=cc_amount)
    auto_link_cc_bill(session, user_id=user.id, txn=cc_row)
    session.expire_all()

    cc_after = session.get(Transaction, cc_row.id)
    assert cc_after is not None
    assert cc_after.transfer_pair_id is None
    assert cc_after.transaction_type == "income"


def test_skips_on_ambiguity_logs_info(
    session: Session,
    user: User,
) -> None:
    """Two bank candidates → no link, single info log with ids only."""
    bank, cc = _make_bank_and_cc(session, user.id)
    bank_a = _make_bank_transfer_row(session, user.id, bank, fingerprint="fp-bank-a")
    bank_b = _make_bank_transfer_row(
        session,
        user.id,
        bank,
        fingerprint="fp-bank-b",
        txn_date=_CC_DATE - timedelta(days=1),
    )
    cc_row = _make_cc_payment_row(session, user.id, cc)

    with structlog.testing.capture_logs() as logs:
        auto_link_cc_bill(session, user_id=user.id, txn=cc_row)

    # No pair was created.
    session.expire_all()
    for r in (bank_a, bank_b, cc_row):
        refreshed = session.get(Transaction, r.id)
        assert refreshed is not None
        assert refreshed.transfer_pair_id is None
        assert refreshed.transaction_type in ("income", "transfer")  # original

    # Exactly one info log, no PII.
    f4a_logs = [entry for entry in logs if entry.get("event") == "f4a_skip_ambiguous_candidates"]
    assert len(f4a_logs) == 1
    entry = f4a_logs[0]
    assert entry["log_level"] == "info"
    assert entry["cc_transaction_id"] == cc_row.id
    assert entry["candidate_count"] == 2
    assert entry["parent_account_id"] == bank.id
    # Defense-in-depth — no PII fields leaked.
    assert "merchant_normalized" not in entry
    assert "amount_paise" not in entry


@pytest.mark.parametrize(
    ("days_offset", "expect_link"),
    [
        (-2, True),
        (2, True),
        (-3, False),
        (3, False),
    ],
)
def test_date_window_boundary_inclusive(
    days_offset: int,
    expect_link: bool,
    session: Session,
    user: User,
) -> None:
    """±2 days inclusive; ±3 days excluded. SQLAlchemy `between(a, b)` is inclusive."""
    bank, cc = _make_bank_and_cc(session, user.id)
    _make_bank_transfer_row(
        session,
        user.id,
        bank,
        txn_date=_CC_DATE + timedelta(days=days_offset),
    )
    cc_row = _make_cc_payment_row(session, user.id, cc)

    auto_link_cc_bill(session, user_id=user.id, txn=cc_row)
    session.commit()
    session.expire_all()

    cc_after = session.get(Transaction, cc_row.id)
    assert cc_after is not None
    if expect_link:
        assert cc_after.transfer_pair_id is not None
        assert cc_after.transaction_type == "transfer"
    else:
        assert cc_after.transfer_pair_id is None
        assert cc_after.transaction_type == "income"


def test_auto_link_moves_each_legs_category_to_pre_link(
    session: Session,
    user: User,
) -> None:
    """ADR-0002 rule 9: the link clears each leg's category into ``pre_link_category_id``
    (a pair belongs to no category)."""
    bank, cc = _make_bank_and_cc(session, user.id)
    spend_cat = Category(color="#2a78d6", user_id=user.id, name="Bills", kind="spend")
    income_cat = Category(color="#2a78d6", user_id=user.id, name="Salary", kind="income")
    session.add_all([spend_cat, income_cat])
    session.commit()
    bank_row = _make_bank_transfer_row(session, user.id, bank, transaction_type="spend")
    cc_row = _make_cc_payment_row(session, user.id, cc)
    bank_row.category_id, cc_row.category_id = spend_cat.id, income_cat.id
    session.commit()

    auto_link_cc_bill(session, user_id=user.id, txn=cc_row)
    session.commit()

    session.refresh(bank_row)
    session.refresh(cc_row)
    assert (bank_row.category_id, cc_row.category_id) == (None, None)
    assert (bank_row.pre_link_category_id, cc_row.pre_link_category_id) == (
        spend_cat.id,
        income_cat.id,
    )


def test_auto_link_skips_a_refund_original(session: Session, user: User) -> None:
    """ADR-0002 rule 7 / ADR-0009 rule 13: a bank debit that a refund points at is not a
    candidate, or the link would turn the refund's original into a transfer."""
    bank, cc = _make_bank_and_cc(session, user.id)
    bank_row = _make_bank_transfer_row(session, user.id, bank, transaction_type="spend")
    refund = _make_bank_transfer_row(
        session,
        user.id,
        bank,
        amount_paise=1000,
        transaction_type="spend",
        fingerprint="fp-refund",
    )
    refund.refund_of_id = bank_row.id
    session.commit()
    cc_row = _make_cc_payment_row(session, user.id, cc)

    auto_link_cc_bill(session, user_id=user.id, txn=cc_row)
    session.commit()

    session.refresh(bank_row)
    session.refresh(cc_row)
    session.refresh(refund)
    assert bank_row.transfer_pair_id is None and bank_row.transaction_type == "spend"
    assert cc_row.transfer_pair_id is None and cc_row.transaction_type == "income"
    assert refund.refund_of_id == bank_row.id


def _stage_bank_batch_row(
    session: Session, user_id: UUID, bank: Account, *, amount_paise: int = -_CC_AMOUNT
) -> Transaction:
    """A bank-statement row as the review queue holds it right before the batch commit:
    attached to a bank batch, with ``confirmed_at`` just stamped (pass 1 of the commit)."""
    batch = _make_batch(
        session,
        user_id,
        bank.id,
        opening_balance_paise=None,
        closing_balance_paise=None,
        period_start=None,
        period_end=None,
    )
    row = Transaction(
        user_id=user_id,
        account_id=bank.id,
        date=_CC_DATE + timedelta(days=1),
        amount_paise=amount_paise,
        transaction_type="spend" if amount_paise < 0 else "income",
        merchant_raw="AXIS CARD BILL",
        merchant_normalized="axis card bill",
        fingerprint="fp-bank-batch",
        source="import",
        import_batch_id=batch.id,
        confirmed_at=None,
    )
    session.add(row)
    session.commit()
    row.confirmed_at = datetime.now(UTC)
    session.flush()
    return row


def test_auto_link_from_bank_batch_commit(session: Session, user: User) -> None:
    """ADR-0002 rule 7 / PRD §F4a-1: the card statement was committed first, and the link
    forms when the bank statement carrying the bill debit commits."""
    bank, cc = _make_bank_and_cc(session, user.id)
    cc_row = _make_cc_payment_row(session, user.id, cc)
    bank_row = _stage_bank_batch_row(session, user.id, bank)

    auto_link_cc_bill(session, user_id=user.id, txn=bank_row)
    session.commit()

    session.refresh(bank_row)
    session.refresh(cc_row)
    assert (bank_row.transfer_pair_id, cc_row.transfer_pair_id) == (cc_row.id, bank_row.id)
    assert (bank_row.transaction_type, cc_row.transaction_type) == ("transfer", "transfer")
    assert (bank_row.pre_link_type, cc_row.pre_link_type) == ("spend", "income")


@pytest.mark.parametrize(
    "case",
    [
        "no_child_card",
        "card_archived",
        "bank_archived",
        "payment_pending",
        "merchant_mismatch",
        "positive_bank_row",
    ],
)
def test_bank_side_no_op_on_ineligible_inputs(session: Session, user: User, case: str) -> None:
    bank, cc = _make_bank_and_cc(session, user.id)
    cc_row = _make_cc_payment_row(
        session,
        user.id,
        cc,
        merchant="some shop" if case == "merchant_mismatch" else "payment received thank you",
    )
    bank_row = _stage_bank_batch_row(
        session,
        user.id,
        bank,
        amount_paise=_CC_AMOUNT if case == "positive_bank_row" else -_CC_AMOUNT,
    )
    if case == "no_child_card":
        cc.parent_account_id = None
    elif case == "card_archived":
        cc.archived_at = datetime.now(UTC)
    elif case == "bank_archived":
        bank.archived_at = datetime.now(UTC)
    elif case == "payment_pending":
        cc_row.confirmed_at = None
    session.flush()

    auto_link_cc_bill(session, user_id=user.id, txn=bank_row)
    session.commit()

    session.refresh(bank_row)
    session.refresh(cc_row)
    assert bank_row.transfer_pair_id is None and cc_row.transfer_pair_id is None
    assert bank_row.transaction_type == ("income" if case == "positive_bank_row" else "spend")
    assert cc_row.transaction_type == "income"


def test_bank_side_skips_two_card_payments_logs_info(session: Session, user: User) -> None:
    """Ambiguity on the card side means no link, and the log carries ids and a count only."""
    bank, cc = _make_bank_and_cc(session, user.id)
    second_cc = Account(
        user_id=user.id,
        name="Axis CC 2",
        type="credit_card",
        issuer="axis",
        last4="5678",
        parent_account_id=bank.id,
    )
    session.add(second_cc)
    session.commit()
    first = _make_cc_payment_row(session, user.id, cc, fingerprint="fp-cc-1")
    second = _make_cc_payment_row(session, user.id, second_cc, fingerprint="fp-cc-2")
    bank_row = _stage_bank_batch_row(session, user.id, bank)

    with structlog.testing.capture_logs() as logs:
        auto_link_cc_bill(session, user_id=user.id, txn=bank_row)

    assert bank_row.transfer_pair_id is None
    assert first.transfer_pair_id is None and second.transfer_pair_id is None
    skipped = [entry for entry in logs if entry["event"] == "f4a_skip_ambiguous_card_candidates"]
    assert len(skipped) == 1
    assert skipped[0]["candidate_count"] == 2
    assert not {"merchant", "merchant_raw", "amount_paise"} & set(skipped[0])


def test_bank_side_skips_when_the_payment_has_two_bank_debits(session: Session, user: User) -> None:
    """The card-side search finds two equal bank debits, so the pair is ambiguous."""
    bank, cc = _make_bank_and_cc(session, user.id)
    cc_row = _make_cc_payment_row(session, user.id, cc)
    twin = _make_bank_transfer_row(session, user.id, bank, transaction_type="spend")
    bank_row = _stage_bank_batch_row(session, user.id, bank)

    auto_link_cc_bill(session, user_id=user.id, txn=bank_row)

    assert bank_row.transfer_pair_id is None and twin.transfer_pair_id is None
    assert cc_row.transfer_pair_id is None


def test_a_constraint_failure_on_the_pair_write_is_not_swallowed(
    session: Session,
    user: User,
) -> None:
    """The pair write does not catch IntegrityError — it propagates to the caller.

    Fault injection by necessity: the branch is unreachable through the public
    surface (both rows come from one ``user_id``-scoped SELECT, and the no-self-pair
    CHECK cannot fire because the CC row is positive on a card while candidates are negative
    on its parent bank, so they are different rows on different accounts). The route-level
    consequence — the whole batch commit aborts with nothing confirmed — is locked by
    ``test_imports_review.test_commit_f4a_pair_write_conflict_aborts_the_whole_batch``.

    The predicate is load-bearing: nothing is dirty with a non-null
    ``transfer_pair_id`` until the pair write sets one, so the injected error can
    only land there.
    """
    bank, cc = _make_bank_and_cc(session, user.id)
    _make_bank_transfer_row(session, user.id, bank)
    cc_row = _make_cc_payment_row(session, user.id, cc)

    def _fail_the_pair_write(sess: Session, _flush_context: object, _instances: object) -> None:
        if any(
            isinstance(obj, Transaction) and obj.transfer_pair_id is not None for obj in sess.dirty
        ):
            raise IntegrityError("simulated pair conflict", None, Exception("forced"))

    event.listen(session, "before_flush", _fail_the_pair_write)
    try:
        with pytest.raises(IntegrityError):
            auto_link_cc_bill(session, user_id=user.id, txn=cc_row)
    finally:
        event.remove(session, "before_flush", _fail_the_pair_write)


# ---------------------------------------------------------------------------
# Balance reconciliation (PRD §F1/§F4a) — reconcile_batch, the window
# fallback, the anti-drift pin against /overview, and rows_removed_since_import.
#
# reconcile_batch's happy-path arithmetic and the (corrected) short-statement
# sign case are covered end-to-end through import_statement in
# tests/services/test_import_service.py, matching the plan's own file split.
# These tests exercise reconcile_batch directly for behaviour that needs
# hand-built ImportBatch / Transaction state import_statement can't drive:
# the window fallback and the anti-drift pin.
# ---------------------------------------------------------------------------


def _make_cc_account(session: Session, user_id: UUID, *, opening_balance_paise: int = 0) -> Account:
    account = Account(
        user_id=user_id,
        name="Axis CC",
        type="credit_card",
        issuer="axis",
        last4="1234",
        opening_balance_paise=opening_balance_paise,
    )
    session.add(account)
    session.commit()
    return account


def _make_batch(
    session: Session,
    user_id: UUID,
    account_id: int | None,
    *,
    opening_balance_paise: int | None,
    closing_balance_paise: int | None,
    period_start: date | None,
    period_end: date | None,
    imported_count: int = 0,
    source_file_hash: str = "hash",
) -> ImportBatch:
    batch = ImportBatch(
        user_id=user_id,
        account_id=account_id,
        source_file_hash=source_file_hash,
        parser_name="AxisCC",
        status="completed",
        imported_count=imported_count,
        statement_opening_balance_paise=opening_balance_paise,
        statement_closing_balance_paise=closing_balance_paise,
        period_start=period_start,
        period_end=period_end,
    )
    session.add(batch)
    session.commit()
    return batch


def _make_row(
    session: Session,
    user_id: UUID,
    account: Account,
    *,
    amount_paise: int,
    txn_date: date,
    fingerprint: str,
    confirmed: bool,
    import_batch_id: int | None = None,
) -> Transaction:
    row = Transaction(
        user_id=user_id,
        account_id=account.id,
        date=txn_date,
        amount_paise=amount_paise,
        transaction_type="spend" if amount_paise < 0 else "income",
        merchant_raw="SENTINEL MERCHANT",
        merchant_normalized="sentinel merchant",
        fingerprint=fingerprint,
        source="import",
        confirmed_at=datetime.now(UTC) if confirmed else None,
        import_batch_id=import_batch_id,
    )
    session.add(row)
    session.commit()
    return row


def test_window_fallback_is_not_stored(
    session: Session,
    user: User,
) -> None:
    """ADR-0010 rule 8: balances present, period absent — reconcile_batch
    computes the window as min/max row date over the batch's own rows and
    the check runs to a real delta, but ``period_*`` keeps only what the
    statement printed (here nothing), so a re-check recomputes the same
    window. A confirmed row outside that window must not count."""
    account = _make_cc_account(session, user.id)
    batch = _make_batch(
        session,
        user.id,
        account.id,
        opening_balance_paise=-1000,
        closing_balance_paise=-2500,
        period_start=None,
        period_end=None,
    )
    _make_row(
        session,
        user.id,
        account,
        amount_paise=-1500,
        txn_date=date(2026, 3, 5),
        fingerprint="fp-1",
        confirmed=False,
        import_batch_id=batch.id,
    )
    _make_row(
        session,
        user.id,
        account,
        amount_paise=0,  # ignored for the delta; still counts toward the date range
        txn_date=date(2026, 3, 20),
        fingerprint="fp-2",
        confirmed=False,
        import_batch_id=batch.id,
    )
    _make_row(  # same account, confirmed, after the computed window: excluded
        session,
        user.id,
        account,
        amount_paise=-999,
        txn_date=date(2026, 4, 10),
        fingerprint="fp-later",
        confirmed=True,
    )

    delta = reconcile_batch(session, user_id=user.id, batch=batch)
    session.commit()
    session.refresh(batch)

    assert delta == 0  # actual -1500 == expected (-2500) - (-1000) == -1500
    assert batch.period_start is None
    assert batch.period_end is None
    assert reconcile_batch(session, user_id=user.id, batch=batch) == 0


def test_bank_statement_reconciles_with_positive_balances(
    session: Session,
    user: User,
) -> None:
    """A44 / ADR-0010 rule 7: a bank prints money held, so its balances are positive and a
    deposit adds to them. ``opening + Σ rows = closing`` holds with the same arithmetic as a
    card (whose balances are negative), so the delta needs no per-account-type sign flip."""
    account = Account(user_id=user.id, name="HDFC Savings", type="bank", issuer="hdfc")
    session.add(account)
    session.commit()
    batch = _make_batch(
        session,
        user.id,
        account.id,
        opening_balance_paise=20_000_000,
        closing_balance_paise=25_942_933,
        period_start=date(2026, 4, 1),
        period_end=date(2026, 4, 30),
    )
    batch.parser_name = "HdfcBank"
    amounts = [-125_000, 7_500_000, -49_900, 49_900, -1_234_567, -200_000, 2_500]
    for i, amount in enumerate(amounts):
        _make_row(
            session,
            user.id,
            account,
            amount_paise=amount,
            txn_date=date(2026, 4, 1 + i),
            fingerprint=f"bank-fp-{i}",
            confirmed=False,
            import_batch_id=batch.id,
        )

    assert reconcile_batch(session, user_id=user.id, batch=batch) == 0


def test_window_fallback_ignores_another_users_row_on_the_batch(
    session: Session,
    user: User,
) -> None:
    """ADR-0003 rule 1: the fallback min/max read restates ``user_id``. The
    composite same-user FK makes another user's row on this batch unstorable
    (rule 10), so it is planted with FK checks off: rule 1 is the independent
    defence. Without the predicate its earlier date would widen the window
    and pull this user's confirmed February row into ``actual``."""
    account = _make_cc_account(session, user.id)
    batch = _make_batch(
        session,
        user.id,
        account.id,
        opening_balance_paise=-1000,
        closing_balance_paise=-2500,
        period_start=None,
        period_end=None,
    )
    _make_row(
        session,
        user.id,
        account,
        amount_paise=-1500,
        txn_date=date(2026, 3, 5),
        fingerprint="fp-1",
        confirmed=False,
        import_batch_id=batch.id,
    )
    _make_row(  # this user's own board row, before the batch's true window
        session,
        user.id,
        account,
        amount_paise=-999,
        txn_date=date(2026, 2, 10),
        fingerprint="fp-feb",
        confirmed=True,
    )
    other = User(id=uuid4())
    session.add(other)
    session.commit()
    their_account = _make_cc_account(session, other.id)
    with foreign_keys_off(session):
        _make_row(
            session,
            other.id,
            their_account,
            amount_paise=-5,
            txn_date=date(2026, 2, 1),
            fingerprint="fp-theirs",
            confirmed=False,
            import_batch_id=batch.id,
        )

    assert reconcile_batch(session, user_id=user.id, batch=batch) == 0


def test_window_fallback_returns_none_when_batch_has_no_rows(
    session: Session,
    user: User,
) -> None:
    """Both balances present, no period, and no rows on the batch to fall
    back to — min/max is (None, None), so the check has no usable window."""
    account = _make_cc_account(session, user.id)
    batch = _make_batch(
        session,
        user.id,
        account.id,
        opening_balance_paise=-1000,
        closing_balance_paise=-2500,
        period_start=None,
        period_end=None,
    )

    assert reconcile_batch(session, user_id=user.id, batch=batch) is None
    assert batch.period_start is None
    assert batch.period_end is None


def test_returns_none_for_an_account_less_batch(
    session: Session,
    user: User,
) -> None:
    """Investment / backup-restore batches have no account, hence no window
    to check — reconcile_batch must no-op rather than raise. Not reachable
    through import_statement (spend-only, always account-scoped) today, but
    will be once Phase 4's GET /imports/{id}/reconciliation calls this for
    every batch shape."""
    batch = _make_batch(
        session,
        user.id,
        None,
        opening_balance_paise=-1000,
        closing_balance_paise=-2500,
        period_start=date(2026, 3, 1),
        period_end=date(2026, 3, 31),
    )

    assert reconcile_batch(session, user_id=user.id, batch=batch) is None


def test_anti_drift_window_check_agrees_with_overview_balance(
    session: Session,
    user: User,
) -> None:
    """Phase 3 test 7 — the anti-drift pin. For an account whose ENTIRE
    history is one statement, and whose opening_balance_paise equals that
    statement's own opening balance, reconcile_batch's window check and the
    /overview absolute balance (api/v1/dashboards.py:
    ``a.opening_balance_paise + Σ(confirmed rows, all time)``, reused here
    via the same confirmed_only predicate the route imports — not
    reimplemented) must agree. If either definition moves, this goes red.
    """
    account = _make_cc_account(session, user.id, opening_balance_paise=-1000)
    batch = _make_batch(
        session,
        user.id,
        account.id,
        opening_balance_paise=-1000,
        closing_balance_paise=-2500,
        period_start=date(2026, 3, 1),
        period_end=date(2026, 3, 31),
    )
    # Confirmed — this account's entire history, board-visible like /overview reads.
    _make_row(
        session,
        user.id,
        account,
        amount_paise=-1500,
        txn_date=date(2026, 3, 5),
        fingerprint="fp-1",
        confirmed=True,
        import_batch_id=batch.id,
    )

    delta = reconcile_batch(session, user_id=user.id, batch=batch)
    session.commit()
    assert delta == 0

    overview_sum_stmt = confirmed_only(
        select(func.sum(Transaction.amount_paise)).where(
            Transaction.user_id == user.id, Transaction.account_id == account.id
        )
    )
    overview_balance_paise = account.opening_balance_paise + (
        session.scalar(overview_sum_stmt) or 0
    )
    assert overview_balance_paise == batch.statement_closing_balance_paise


def test_rows_removed_since_import_counts_hard_deletes(
    session: Session,
    user: User,
) -> None:
    """The discard-noise qualifier (approved fix, option c): imported_count is
    frozen at first import; a live count below it means rows were hard-deleted
    since (e.g. an investment-transfer row discarded at review). No amount
    trace survives the delete, so this is a count, not a paise correction."""
    account = _make_cc_account(session, user.id)
    batch = _make_batch(
        session,
        user.id,
        account.id,
        opening_balance_paise=None,
        closing_balance_paise=None,
        period_start=None,
        period_end=None,
        imported_count=3,
    )
    # Only 2 of the original 3 rows still exist for this batch.
    _make_row(
        session,
        user.id,
        account,
        amount_paise=-100,
        txn_date=date(2026, 3, 5),
        fingerprint="fp-1",
        confirmed=True,
        import_batch_id=batch.id,
    )
    _make_row(
        session,
        user.id,
        account,
        amount_paise=-200,
        txn_date=date(2026, 3, 6),
        fingerprint="fp-2",
        confirmed=True,
        import_batch_id=batch.id,
    )

    assert rows_removed_since_import(session, batch=batch) == 1


def test_rows_removed_since_import_floors_at_zero_on_reupload_growth(
    session: Session,
    user: User,
) -> None:
    """A re-upload can re-stage rows onto the SAME batch_id, pushing the live
    count above imported_count — that's rows coming back, not more being
    removed, so this floors at zero rather than going negative."""
    account = _make_cc_account(session, user.id)
    batch = _make_batch(
        session,
        user.id,
        account.id,
        opening_balance_paise=None,
        closing_balance_paise=None,
        period_start=None,
        period_end=None,
        imported_count=1,
    )
    _make_row(
        session,
        user.id,
        account,
        amount_paise=-100,
        txn_date=date(2026, 3, 5),
        fingerprint="fp-1",
        confirmed=True,
        import_batch_id=batch.id,
    )
    _make_row(
        session,
        user.id,
        account,
        amount_paise=-200,
        txn_date=date(2026, 3, 6),
        fingerprint="fp-2",
        confirmed=False,
        import_batch_id=batch.id,
    )

    assert rows_removed_since_import(session, batch=batch) == 0


def test_rows_removed_ignores_another_users_row_on_the_batch(
    session: Session,
    user: User,
) -> None:
    """ADR-0003 rule 7: the live count restates ``user_id``. Another user's row
    on this batch is unstorable under the composite same-user FK (rule 10), so
    it is planted with FK checks off. Without the predicate it would count as
    one of this batch's live rows and hide the one this user deleted."""
    account = _make_cc_account(session, user.id)
    batch = _make_batch(
        session,
        user.id,
        account.id,
        opening_balance_paise=None,
        closing_balance_paise=None,
        period_start=None,
        period_end=None,
        imported_count=2,
    )
    _make_row(
        session,
        user.id,
        account,
        amount_paise=-100,
        txn_date=date(2026, 3, 5),
        fingerprint="fp-1",
        confirmed=True,
        import_batch_id=batch.id,
    )
    other = User(id=uuid4())
    session.add(other)
    session.commit()
    their_account = _make_cc_account(session, other.id)
    with foreign_keys_off(session):
        _make_row(
            session,
            other.id,
            their_account,
            amount_paise=-5,
            txn_date=date(2026, 3, 6),
            fingerprint="fp-theirs",
            confirmed=True,
            import_batch_id=batch.id,
        )

    assert rows_removed_since_import(session, batch=batch) == 1


@pytest.mark.parametrize(
    ("merchant_normalized", "expected"),
    [
        ("cc 000000xxxxxx0000 autopay si-tad", True),
        ("cc payment", True),
        ("credit card pmt 4321", True),
        ("credit card payment", True),
        ("credit card bill", True),
        ("hdfc cc 000000xxxxxx0000", True),
        ("icici cc", True),
        ("axis cc", True),
        ("sbi cc autopay", True),
        ("card bill sentinel", True),
        ("cred club", True),
        ("cred.club", True),
        ("bbps electricity", False),
        ("ib billpay sentinel", False),
        ("sentinel cafe", False),
        ("upi-sentinel grocer-sentinelgrocer@example", False),
        ("ncc payment", False),
        ("payment received thank you", False),
    ],
)
def test_is_card_bill_debit(merchant_normalized: str, expected: bool) -> None:
    """The bank-side review aid (PRD §F4a-1): a debit naming a card-bill payment. Bare BBPS and
    bill-pay wording is too general to flag, and the card-side "payment received" is not it."""
    assert is_card_bill_debit(merchant_normalized) is expected


@pytest.mark.parametrize(
    ("period_start", "period_end", "rows_first", "rows_last", "expected"),
    [
        (
            date(2026, 3, 1),
            date(2026, 3, 31),
            date(2026, 3, 5),
            date(2026, 3, 9),
            (date(2026, 3, 1), date(2026, 3, 31)),
        ),
        (
            date(2026, 3, 1),
            None,
            date(2026, 3, 5),
            date(2026, 3, 9),
            (date(2026, 3, 5), date(2026, 3, 9)),
        ),
        (None, None, None, None, None),
    ],
    ids=["printed", "half_printed_falls_back_to_rows", "nothing"],
)
def test_statement_window(
    period_start: date | None,
    period_end: date | None,
    rows_first: date | None,
    rows_last: date | None,
    expected: tuple[date, date] | None,
) -> None:
    """ADR-0010 rule 8: the printed period, else the first and last row dates, else none."""
    assert (
        statement_window(
            period_start=period_start,
            period_end=period_end,
            rows_first=rows_first,
            rows_last=rows_last,
        )
        == expected
    )


def test_card_bill_gate_reads_the_card_statements_in_one_query(
    engine: Engine,
    session: Session,
    user: User,
) -> None:
    """ADR-0002 rule 7b: the gate loads the card statements once per batch, never per row."""
    bank, cc = _make_bank_and_cc(session, user.id)
    for i, month in enumerate((1, 2, 3)):
        batch = _make_batch(
            session,
            user.id,
            cc.id,
            opening_balance_paise=None,
            closing_balance_paise=None,
            period_start=date(2026, month, 1),
            period_end=date(2026, month, 28),
            source_file_hash=f"card-{i}",
        )
        row = _make_cc_payment_row(
            session,
            user.id,
            cc,
            fingerprint=f"fp-card-{i}",
            txn_date=date(2026, month, 10),
        )
        row.import_batch_id = batch.id
        session.commit()
    bank_batch = _make_batch(
        session,
        user.id,
        bank.id,
        opening_balance_paise=None,
        closing_balance_paise=None,
        period_start=None,
        period_end=None,
        source_file_hash="bank",
    )

    statements: list[str] = []

    def capture(conn: object, cursor: object, statement: str, *rest: object) -> None:
        statements.append(statement)

    def debit(when: date, amount_paise: int = -1) -> Transaction:
        return Transaction(
            account_id=bank.id,
            date=when,
            amount_paise=amount_paise,
            transaction_type="spend",
            merchant_normalized="axis card bill",
        )

    event.listen(engine, "before_cursor_execute", capture)
    try:
        gate = card_bill_transfer_gate(session, user_id=user.id, batch=bank_batch)
        assert len(statements) == 2
        assert [gate(debit(date(2026, m, 15))) for m in (1, 2, 3)] == [True, True, True]
        assert gate(debit(date(2026, 4, 15))) is False
        assert gate(debit(date(2026, 1, 15), 1)) is False
        assert len(statements) == 2
    finally:
        event.remove(engine, "before_cursor_execute", capture)


def test_card_bill_link_offered_reads_the_accounts_in_one_query(
    engine: Engine,
    session: Session,
    user: User,
) -> None:
    """ADR-0002 rule 11b: the offered ids come from one accounts read per response, never one
    per row, and from none when no row could be offered."""
    bank, cc = _make_bank_and_cc(session, user.id)
    payments = [
        _make_cc_payment_row(session, user.id, cc, fingerprint=f"fp-offer-{i}") for i in range(3)
    ]
    debit = _make_bank_transfer_row(session, user.id, bank)
    for row in (*payments, debit):
        session.refresh(row)

    statements: list[str] = []

    def capture(conn: object, cursor: object, statement: str, *rest: object) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        offered = card_bill_link_offered_ids(session, user_id=user.id, txns=[*payments, debit])
        assert offered == {p.id for p in payments}
        assert len(statements) == 1
        statements.clear()
        assert card_bill_link_offered_ids(session, user_id=user.id, txns=[debit]) == set()
        assert statements == []
    finally:
        event.remove(engine, "before_cursor_execute", capture)
