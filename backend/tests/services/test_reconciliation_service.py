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
from sqlalchemy import event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Account, ImportBatch, Transaction, User
from app.services.reconciliation_service import (
    auto_link_cc_bill,
    reconcile_batch,
    rows_removed_since_import,
)
from app.services.transaction_queries import confirmed_only

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
    ["no_candidate", "outside_date_window", "already_paired", "positive_refund_wrong_sign"],
)
def test_no_op_on_no_or_bad_candidate(
    candidate_state: str,
    session: Session,
    user: User,
) -> None:
    """No link when the bank-side candidate is missing or filtered out.

    ``positive_refund_wrong_sign`` is the T2 mis-pair guard (ADR-0009): since a
    refund is a ``spend`` row with a positive amount, the type filter alone no
    longer excludes one, so ``auto_link_cc_bill`` needs an explicit
    ``amount_paise < 0`` predicate on the bank side. That guard is normally
    *derivable* rather than stated — ``txn`` (CC-side) is an ``income`` row, and
    ``sign_error`` pins ``income > 0``, which alone would make the bank target
    negative and a positive refund unreachable. But ``POST /backup/import``
    persists type + amount verbatim without ``sign_error`` (a hand-edited zip
    is its declared threat model), so a NEGATIVE ``income`` row is reachable —
    flipping the target positive and making a same-magnitude positive refund a
    live mis-pair candidate without the guard.
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

    # `positive_refund_wrong_sign` needs the CC row itself negative — the state
    # `sign_error` blocks at the API but `backup_csv` does not enforce on
    # restore. Every other branch keeps the ordinary positive-income CC row.
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


def test_window_fallback_uses_min_max_row_date_when_period_absent(
    session: Session,
    user: User,
) -> None:
    """Balances present, period absent (Phase 3 test 5): reconcile_batch falls
    back to min/max row date over the batch's own rows, STAMPS that fallback
    onto batch.period_start/period_end (so a later read sees a window either
    way), and the check still runs to a real delta."""
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

    delta = reconcile_batch(session, user_id=user.id, batch=batch)
    session.commit()

    assert delta == 0  # actual -1500 == expected (-2500) - (-1000) == -1500
    assert batch.period_start == date(2026, 3, 5)
    assert batch.period_end == date(2026, 3, 20)


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
