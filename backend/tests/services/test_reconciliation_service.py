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
from uuid import UUID

import pytest
import structlog
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Account, Transaction, User
from app.services.reconciliation_service import auto_link_cc_bill

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
        partner = _make_bank_transfer_row(session, user.id, bank, fingerprint="fp-partner")
        cc_row = _make_cc_payment_row(session, user.id, cc, transfer_pair_id=partner.id)
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


@pytest.mark.parametrize(
    "candidate_state",
    ["no_candidate", "outside_date_window", "already_paired", "refund_type"],
)
def test_no_op_on_no_or_bad_candidate(
    candidate_state: str,
    session: Session,
    user: User,
) -> None:
    """No link when the bank-side candidate is missing or filtered out."""
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
        )
        bank_row = _make_bank_transfer_row(session, user.id, bank, transfer_pair_id=partner.id)
        partner.transfer_pair_id = bank_row.id
        session.commit()
    elif candidate_state == "refund_type":
        # Bank-side refund of equal magnitude should NOT pair.
        _make_bank_transfer_row(
            session,
            user.id,
            bank,
            transaction_type="refund",
            amount_paise=_CC_AMOUNT,  # refunds are positive; magnitude matches
        )
    else:
        raise AssertionError(f"unknown candidate_state: {candidate_state}")

    cc_row = _make_cc_payment_row(session, user.id, cc)
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


def test_a_constraint_failure_on_the_pair_write_is_not_swallowed(
    session: Session,
    user: User,
) -> None:
    """The pair write does not catch IntegrityError — it propagates to the caller.

    Fault injection by necessity: the branch is unreachable through the public
    surface (both rows come from one ``user_id``-scoped SELECT, and the no-self-pair
    CHECK cannot fire because the CC row is ``income`` while candidates must be
    ``spend``/``transfer``). The route-level consequence — the whole batch commit
    aborts with nothing confirmed — is locked by
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
