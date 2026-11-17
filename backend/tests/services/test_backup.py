"""Service tests for backup export + additive import (PRD §F10).

Coverage-gated for the slice. Exercises ``build_backup_zip`` → ``persist_backup`` end to end
against in-memory sessions (no TestClient). The load-bearing assertions:

* a full round trip rebuilds accounts/categories/transactions on a **fresh** DB, with exact
  paise, resolved categories (incl. NULL), preserved confirmation, and a re-linked transfer
  pair — and the fingerprint is **recomputed** from the resolved account id;
* pending (unconfirmed) rows are never exported;
* re-import is idempotent, and a backup row dedups against a **natively created** row (the
  finding that reshaped the plan — verbatim fingerprints could not do this);
* a matched account is never mutated, and a name clash with a different type is rejected, not
  rebound.
"""

from __future__ import annotations

import io
import re
import zipfile
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from structlog.testing import capture_logs

from app.core.config import get_settings
from app.core.db import make_engine
from app.models import Account, Base, Category, Transaction, User
from app.parsers import ParsedStatement, RawTransaction, StatementSummary
from app.parsers.backup_csv import (
    TRANSACTIONS_CSV,
    ParsedBackup,
    ParsedBackupAccount,
    ParsedBackupCategory,
    ParsedBackupTransaction,
    _parse_dt,
    parse_backup_zip,
)
from app.services import import_service
from app.services.backup_import_service import persist_backup
from app.services.category_colors import CATEGORY_PALETTE
from app.services.export_service import build_backup_zip
from app.services.fingerprint import transaction_fingerprint
from tests.backup_zip import account, edit_transactions, make_backup_zip, txn


@pytest.fixture
def user_id(session: Session) -> UUID:
    uid = get_settings().v1_user_id
    session.add(User(id=uid))
    session.flush()
    return uid


@contextmanager
def fresh_db() -> Iterator[tuple[Session, UUID]]:
    """A second, independent in-memory DB with the v1 user seeded — the restore target."""
    eng = make_engine("sqlite:///:memory:", poolclass=StaticPool)
    Base.metadata.create_all(eng)
    factory = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)
    s = factory()
    uid = get_settings().v1_user_id
    s.add(User(id=uid))
    s.flush()
    try:
        yield s, uid
    finally:
        s.close()
        Base.metadata.drop_all(eng)
        eng.dispose()


def _add_txn(
    session: Session,
    *,
    user_id: UUID,
    account_id: int,
    day: int,
    amount: int,
    txn_type: str,
    merchant_norm: str = "merchant",
    category_id: int | None = None,
    confirmed: bool = True,
    merchant_raw: str | None = None,
    occurrence: int = 0,
) -> Transaction:
    txn_date = date(2026, 7, day)
    txn = Transaction(
        user_id=user_id,
        account_id=account_id,
        date=txn_date,
        amount_paise=amount,
        transaction_type=txn_type,
        merchant_raw=merchant_raw,
        merchant_normalized=merchant_norm,
        category_id=category_id,
        fingerprint=transaction_fingerprint(
            txn_date=txn_date,
            amount_paise=amount,
            normalized_merchant=merchant_norm,
            account_id=account_id,
        ),
        occurrence=occurrence,
        source="import",
        confirmed_at=datetime(2026, 7, day, 10, 0, 0) if confirmed else None,
    )
    session.add(txn)
    return txn


def _seed_source(session: Session, user_id: UUID) -> None:
    """A small but representative spend history: two accounts, two categories, a spend, a
    refund (a `spend` row with a positive amount, ADR-0009), an income row, a linked
    transfer pair, and one pending (unconfirmed) row."""
    axis = Account(
        user_id=user_id,
        name="Axis CC",
        type="credit_card",
        issuer="axis",
        last4="1234",
        opening_balance_paise=-500000,
    )
    hdfc = Account(
        user_id=user_id, name="HDFC Bank", type="bank", issuer="hdfc", opening_balance_paise=100000
    )
    session.add_all([axis, hdfc])
    session.flush()

    food = Category(user_id=user_id, name="Food", kind="spend", color="#4f46e5")
    salary = Category(color="#2a78d6", user_id=user_id, name="Salary", kind="income")
    session.add_all([food, salary])
    session.flush()

    _add_txn(
        session,
        user_id=user_id,
        account_id=axis.id,
        day=1,
        amount=-50000,
        txn_type="spend",
        merchant_norm="swiggy",
        category_id=food.id,
        merchant_raw="SWIGGY",
    )
    _add_txn(
        session,
        user_id=user_id,
        account_id=axis.id,
        day=2,
        amount=10000,
        txn_type="spend",  # refund: spend row, positive amount (ADR-0009)
        merchant_norm="bigbasket",
        category_id=food.id,
    )
    _add_txn(
        session,
        user_id=user_id,
        account_id=hdfc.id,
        day=3,
        amount=5000000,
        txn_type="income",
        merchant_norm="acme payroll",
        category_id=salary.id,
    )
    leg_out = _add_txn(
        session,
        user_id=user_id,
        account_id=hdfc.id,
        day=4,
        amount=-100000,
        txn_type="transfer",
        merchant_norm="transfer to axis cc",
    )
    leg_in = _add_txn(
        session,
        user_id=user_id,
        account_id=axis.id,
        day=4,
        amount=100000,
        txn_type="transfer",
        merchant_norm="transfer from hdfc bank",
    )
    session.flush()
    leg_out.transfer_pair_id = leg_in.id
    leg_in.transfer_pair_id = leg_out.id

    # Pending — must NOT be exported (would otherwise be auto-confirmed on restore).
    _add_txn(
        session,
        user_id=user_id,
        account_id=axis.id,
        day=5,
        amount=-9999,
        txn_type="spend",
        merchant_norm="pending shop",
        confirmed=False,
    )
    session.flush()


def _txn_count(session: Session, user_id: UUID, **filters: object) -> int:
    stmt = select(func.count()).select_from(Transaction).where(Transaction.user_id == user_id)
    for column, value in filters.items():
        stmt = stmt.where(getattr(Transaction, column) == value)
    return session.scalar(stmt) or 0


def _seed_duplicate_pair(session: Session, user_id: UUID) -> Account:
    """One account holding two genuinely-distinct rows that share a fingerprint."""
    axis = Account(user_id=user_id, name="Axis CC", type="credit_card", issuer="axis", last4="1234")
    session.add(axis)
    session.flush()
    for occ in (0, 1):
        _add_txn(
            session,
            user_id=user_id,
            account_id=axis.id,
            day=1,
            amount=-25000,
            txn_type="spend",
            merchant_norm="uber india",
            occurrence=occ,
        )
    session.flush()
    return axis


def test_round_trip_preserves_a_duplicate_pair(session: Session, user_id: UUID) -> None:
    """ADR-0006: round-trip identity is the fingerprint MULTISET, not a set.

    ``occurrence`` is deliberately absent from the CSV (it is install-local, like
    ``id``), so the importer re-derives it. The restored occurrences need not be
    the same integers — only the per-fingerprint cardinality must match, which is
    what keeps a duplicate pair from collapsing to one row on restore.
    """
    _seed_duplicate_pair(session, user_id)
    session.commit()
    zip_bytes = build_backup_zip(session, user_id=user_id)

    with fresh_db() as (target, target_uid):
        result = persist_backup(
            target,
            user_id=target_uid,
            parsed=parse_backup_zip(zip_bytes),
            source_file_hash="hash",
        )
        target.commit()

        assert result.txns_imported == 2
        assert result.txns_skipped_dupe == 0
        restored = target.scalars(select(Transaction)).all()
        assert Counter(t.fingerprint for t in restored) == Counter(
            t.fingerprint for t in session.scalars(select(Transaction)).all()
        )
        assert sorted(t.occurrence for t in restored) == [0, 1]


def test_reimport_of_a_duplicate_pair_is_idempotent(session: Session, user_id: UUID) -> None:
    """Restoring the same backup twice must not inflate a duplicate group.

    The multiset check is exactly what makes this hold: n_file == n_db == 2 on the
    second pass, so the difference is empty.
    """
    _seed_duplicate_pair(session, user_id)
    session.commit()
    parsed = parse_backup_zip(build_backup_zip(session, user_id=user_id))

    with fresh_db() as (target, target_uid):
        persist_backup(target, user_id=target_uid, parsed=parsed, source_file_hash="hash")
        target.commit()
        again = persist_backup(target, user_id=target_uid, parsed=parsed, source_file_hash="hash")
        target.commit()

        assert again.txns_imported == 0
        assert again.txns_skipped_dupe == 2
        assert len(target.scalars(select(Transaction)).all()) == 2


def test_round_trip_rebuilds_on_a_fresh_db(session: Session, user_id: UUID) -> None:
    _seed_source(session, user_id)
    session.commit()
    zip_bytes = build_backup_zip(session, user_id=user_id)

    with fresh_db() as (target, target_uid):
        result = persist_backup(
            target,
            user_id=target_uid,
            parsed=parse_backup_zip(zip_bytes),
            source_file_hash="hash",
        )
        target.commit()

        assert result.accounts_new == 2
        assert result.categories_new == 2
        assert result.txns_imported == 5  # 5 confirmed; the pending row is excluded
        assert result.txns_skipped_dupe == 0

        accounts = {
            a.name: a for a in target.scalars(select(Account).where(Account.user_id == target_uid))
        }
        assert set(accounts) == {"Axis CC", "HDFC Bank"}
        assert accounts["Axis CC"].type == "credit_card"
        assert accounts["Axis CC"].opening_balance_paise == -500000

        txns = list(target.scalars(select(Transaction).where(Transaction.user_id == target_uid)))
        assert len(txns) == 5
        assert all(t.confirmed_at is not None for t in txns)
        assert all(t.merchant_normalized != "pending shop" for t in txns)

        by_norm = {t.merchant_normalized: t for t in txns}
        assert by_norm["swiggy"].amount_paise == -50000  # exact int, no scaling

        food = target.scalar(
            select(Category).where(Category.user_id == target_uid, Category.name == "Food")
        )
        assert food is not None
        assert by_norm["swiggy"].category_id == food.id

        # Fingerprint recomputed against the RESOLVED account id (never carried from the file).
        expected_fp = transaction_fingerprint(
            txn_date=date(2026, 7, 1),
            amount_paise=-50000,
            normalized_merchant="swiggy",
            account_id=accounts["Axis CC"].id,
        )
        assert by_norm["swiggy"].fingerprint == expected_fp

        # Transfer pair re-linked, symmetric, NULL category on both legs.
        transfer_legs = [t for t in txns if t.transaction_type == "transfer"]
        assert result.transfers_relinked == 2
        assert len(transfer_legs) == 2
        leg_ids = {t.id for t in transfer_legs}
        for leg in transfer_legs:
            assert leg.category_id is None
            assert leg.transfer_pair_id in leg_ids
            assert leg.transfer_pair_id != leg.id


def _by_merchant(session: Session, user_id: UUID) -> dict[str, Transaction]:
    return {
        t.merchant_normalized: t
        for t in session.scalars(select(Transaction).where(Transaction.user_id == user_id))
    }


def _link_refund_and_mark_legs(session: Session, user_id: UUID) -> None:
    """On ``_seed_source``'s rows: link the bigbasket refund to the swiggy spend
    (ADR-0009 rule 13) and give the transfer legs pre-link types (ADR-0002 rule 9)."""
    rows = _by_merchant(session, user_id)
    rows["bigbasket"].refund_of_id = rows["swiggy"].id
    rows["transfer to axis cc"].pre_link_type = "spend"
    rows["transfer from hdfc bank"].pre_link_type = "income"
    session.commit()


def test_round_trip_preserves_refund_link_and_pre_link_type(
    session: Session, user_id: UUID
) -> None:
    _seed_source(session, user_id)
    session.commit()
    _link_refund_and_mark_legs(session, user_id)
    zip_bytes = build_backup_zip(session, user_id=user_id)

    with fresh_db() as (target, target_uid):
        result = persist_backup(
            target, user_id=target_uid, parsed=parse_backup_zip(zip_bytes), source_file_hash="h"
        )
        target.commit()

        assert result.warnings == []
        rows = _by_merchant(target, target_uid)
        assert rows["bigbasket"].refund_of_id == rows["swiggy"].id
        assert rows["swiggy"].refund_of_id is None
        assert rows["transfer to axis cc"].pre_link_type == "spend"
        assert rows["transfer from hdfc bank"].pre_link_type == "income"
        assert rows["acme payroll"].pre_link_type is None


def _save_pre_link_categories(session: Session, user_id: UUID) -> None:
    """Move the transfer legs' category aside as a link would (ADR-0002 rule 9): the outgoing
    leg saves Food, the incoming leg saves Salary. Run after ``_link_refund_and_mark_legs``."""
    rows = _by_merchant(session, user_id)
    cats = {c.name: c for c in session.scalars(select(Category).where(Category.user_id == user_id))}
    rows["transfer to axis cc"].pre_link_category_id = cats["Food"].id
    rows["transfer from hdfc bank"].pre_link_category_id = cats["Salary"].id
    session.commit()


def test_round_trip_preserves_pre_link_category(session: Session, user_id: UUID) -> None:
    """ADR-0002 rule 9: the pair is relinked and each leg's saved category resolves by name and
    kind to the target's own category; the legs stay uncategorised while linked."""
    _seed_source(session, user_id)
    session.commit()
    _link_refund_and_mark_legs(session, user_id)
    _save_pre_link_categories(session, user_id)
    zip_bytes = build_backup_zip(session, user_id=user_id)

    with fresh_db() as (target, target_uid):
        result = persist_backup(
            target, user_id=target_uid, parsed=parse_backup_zip(zip_bytes), source_file_hash="h"
        )
        target.commit()

        assert result.warnings == []
        rows = _by_merchant(target, target_uid)
        out, inn = rows["transfer to axis cc"], rows["transfer from hdfc bank"]
        assert out.transfer_pair_id == inn.id and inn.transfer_pair_id == out.id
        assert (out.category_id, inn.category_id) == (None, None)
        cats = {
            c.name: c.id
            for c in target.scalars(select(Category).where(Category.user_id == target_uid))
        }
        assert (out.pre_link_category_id, inn.pre_link_category_id) == (
            cats["Food"],
            cats["Salary"],
        )


def test_restore_lone_leg_gets_its_pre_link_category_back(session: Session, user_id: UUID) -> None:
    """ADR-0002 rule 9 on restore: a leg left unpaired is released like a partner-delete, so
    its saved category goes back onto the row."""
    _seed_source(session, user_id)
    session.commit()
    _link_refund_and_mark_legs(session, user_id)
    _save_pre_link_categories(session, user_id)
    zip_bytes = build_backup_zip(session, user_id=user_id)

    with fresh_db() as (target, target_uid):
        _lone_leg_target(target, target_uid)
        persist_backup(
            target, user_id=target_uid, parsed=parse_backup_zip(zip_bytes), source_file_hash="h"
        )
        target.commit()
        lone = _by_merchant(target, target_uid)["transfer from hdfc bank"]
        salary = target.scalars(
            select(Category).where(Category.user_id == target_uid, Category.name == "Salary")
        ).one()
        assert lone.transfer_pair_id is None
        assert (lone.category_id, lone.pre_link_category_id) == (salary.id, None)


def test_restore_keeps_a_category_over_a_pre_link_category(session: Session, user_id: UUID) -> None:
    """A row carrying both (only a hand-edited zip can) keeps its own category and drops the
    saved one, so CHECK ``pre_link_category_while_linked`` holds."""
    _seed_source(session, user_id)
    session.commit()
    _link_refund_and_mark_legs(session, user_id)
    _save_pre_link_categories(session, user_id)

    def categorise(rows: list[dict[str, str]]) -> list[dict[str, str]]:
        for row in rows:
            if row["merchant_normalized"] == "transfer from hdfc bank":
                row["category_name"], row["category_kind"] = "Food", "spend"
        return rows

    edited = edit_transactions(build_backup_zip(session, user_id=user_id), categorise)

    with fresh_db() as (target, target_uid):
        result = persist_backup(
            target, user_id=target_uid, parsed=parse_backup_zip(edited), source_file_hash="h"
        )
        target.commit()

        assert result.transfers_relinked == 2
        rows = _by_merchant(target, target_uid)
        inn = rows["transfer from hdfc bank"]
        food = target.scalars(
            select(Category).where(Category.user_id == target_uid, Category.name == "Food")
        ).one()
        assert (inn.category_id, inn.pre_link_category_id) == (food.id, None)
        assert rows["transfer to axis cc"].pre_link_category_id == food.id


def test_restore_warns_when_a_refund_link_cannot_be_restored(
    session: Session, user_id: UUID
) -> None:
    """The original is not among the rows this restore inserted (here: a dangling ref in a
    hand-edited zip). The refund lands unlinked, with a line-number-only warning that does
    not count as a rejected row."""
    _seed_source(session, user_id)
    session.commit()
    _link_refund_and_mark_legs(session, user_id)
    original_ref = str(_by_merchant(session, user_id)["swiggy"].id)

    def dangle(rows: list[dict[str, str]]) -> list[dict[str, str]]:
        for row in rows:
            if row["refund_of_ref"] == original_ref:
                row["refund_of_ref"] = "9999"
        return rows

    edited = edit_transactions(build_backup_zip(session, user_id=user_id), dangle)

    with fresh_db() as (target, target_uid):
        parsed = parse_backup_zip(edited)
        result = persist_backup(target, user_id=target_uid, parsed=parsed, source_file_hash="h")
        target.commit()

        refund_line = next(t.line_no for t in parsed.transactions if t.refund_of_ref == "9999")
        assert result.warnings == [
            f"{TRANSACTIONS_CSV} row {refund_line}: refund link not restored"
        ]
        assert result.rows_rejected == 0
        assert result.links_not_restored == 1
        assert result.txns_imported == 5
        assert _by_merchant(target, target_uid)["bigbasket"].refund_of_id is None


def test_reimport_is_idempotent(session: Session, user_id: UUID) -> None:
    _seed_source(session, user_id)
    session.commit()
    zip_bytes = build_backup_zip(session, user_id=user_id)

    with fresh_db() as (target, target_uid):
        persist_backup(
            target, user_id=target_uid, parsed=parse_backup_zip(zip_bytes), source_file_hash="h"
        )
        target.commit()
        second = persist_backup(
            target, user_id=target_uid, parsed=parse_backup_zip(zip_bytes), source_file_hash="h"
        )
        target.commit()

        assert second.txns_imported == 0
        assert second.txns_skipped_dupe == 5
        assert second.accounts_new == 0
        assert second.accounts_matched == 2
        assert _txn_count(target, target_uid) == 5


def test_dedups_against_a_natively_created_row(session: Session, user_id: UUID) -> None:
    _seed_source(session, user_id)
    session.commit()
    zip_bytes = build_backup_zip(session, user_id=user_id)

    with fresh_db() as (target, target_uid):
        # A native Axis CC + a native txn identical to the backup's swiggy spend.
        axis = Account(
            user_id=target_uid,
            name="Axis CC",
            type="credit_card",
            issuer="axis",
            last4="1234",
            opening_balance_paise=-500000,
        )
        target.add(axis)
        target.flush()
        _add_txn(
            target,
            user_id=target_uid,
            account_id=axis.id,
            day=1,
            amount=-50000,
            txn_type="spend",
            merchant_norm="swiggy",
            merchant_raw="SWIGGY",
        )
        target.commit()

        result = persist_backup(
            target, user_id=target_uid, parsed=parse_backup_zip(zip_bytes), source_file_hash="h"
        )
        target.commit()

        assert result.accounts_matched >= 1  # Axis CC matched, not duplicated
        assert result.txns_skipped_dupe >= 1  # the swiggy spend deduped by recomputed fingerprint
        # Exactly one swiggy -50000 row survives.
        assert (
            _txn_count(target, target_uid, merchant_normalized="swiggy", amount_paise=-50000) == 1
        )


def test_matched_account_is_not_mutated(session: Session, user_id: UUID) -> None:
    _seed_source(session, user_id)
    session.commit()
    zip_bytes = build_backup_zip(session, user_id=user_id)  # backup Axis CC opening = -500000

    with fresh_db() as (target, target_uid):
        axis = Account(
            user_id=target_uid,
            name="Axis CC",
            type="credit_card",
            issuer="axis",
            last4="9999",
            opening_balance_paise=-111,
        )
        target.add(axis)
        target.commit()

        result = persist_backup(
            target, user_id=target_uid, parsed=parse_backup_zip(zip_bytes), source_file_hash="h"
        )
        target.commit()
        target.refresh(axis)

        assert result.accounts_matched >= 1
        assert axis.opening_balance_paise == -111  # create-locked field left untouched
        assert axis.last4 == "9999"


def test_name_clash_with_different_type_is_rejected(session: Session, user_id: UUID) -> None:
    _seed_source(session, user_id)
    session.commit()
    zip_bytes = build_backup_zip(session, user_id=user_id)  # backup "Axis CC" is a credit_card

    with fresh_db() as (target, target_uid):
        axis_bank = Account(
            user_id=target_uid, name="Axis CC", type="bank", opening_balance_paise=0
        )
        target.add(axis_bank)
        target.commit()

        result = persist_backup(
            target, user_id=target_uid, parsed=parse_backup_zip(zip_bytes), source_file_hash="h"
        )
        target.commit()

        assert any("different" in w for w in result.warnings)
        # The mismatched account is never rebound, so its transactions do not land on it.
        assert _txn_count(target, target_uid, account_id=axis_bank.id) == 0


# --------------------------------------------------------------------------------------
# The hand-edited datetime boundary (B#59). `_parse_dt` is the only place in the app where
# a user-authored UTC offset can enter, and the export gives no hint that UTC is required.
# --------------------------------------------------------------------------------------


def test_offset_bearing_datetime_normalizes_to_the_same_instant_as_its_utc_spelling() -> None:
    """``+05:30`` and its UTC spelling must parse to one instant.

    Without normalization the aware value reached the ORM and SQLite dropped the offset,
    storing the wall clock — 5h30m wrong, permanently, with no error — while Postgres would
    have converted. ``2026-07-30T10:00:00+05:30`` is the natural thing to type in India.
    """
    ist = _parse_dt("2026-07-30T10:00:00+05:30")
    utc = _parse_dt("2026-07-30T04:30:00")

    assert ist == utc == datetime(2026, 7, 30, 4, 30, 0)
    assert ist is not None and ist.tzinfo is None  # naive UTC on the way to the ORM


def test_naive_datetime_round_trips_unshifted() -> None:
    """A cell WITHOUT an offset is already UTC and must not move.

    This is the common path, not the rare one: ``export_service`` writes ``isoformat()`` of a
    value SQLite hands back naive, so every cell the app itself produces is offset-free.
    Normalizing unconditionally — ``astimezone(UTC).replace(tzinfo=None)`` with no guard, as
    the remediation plan originally prescribed — would read this as the HOST's local time and
    shift it by the host offset, corrupting every ordinary restore on any non-UTC machine.
    On an IST host that turns 10:00 into 04:30.
    """
    assert _parse_dt("2026-07-30T10:00:00") == datetime(2026, 7, 30, 10, 0, 0)
    assert _parse_dt("2026-07-30 10:00:00") == datetime(2026, 7, 30, 10, 0, 0)
    # A trailing Z is an explicit offset, so it normalizes rather than passing through.
    assert _parse_dt("2026-07-30T10:00:00Z") == datetime(2026, 7, 30, 10, 0, 0)


def test_unparseable_and_empty_datetime_cells_stay_none() -> None:
    """Unchanged by the normalization: a junk cell is skipped, not an import-wide failure."""
    assert _parse_dt("") is None
    assert _parse_dt(None) is None
    assert _parse_dt("not-a-date") is None


# --- import telemetry (PRD §Production-grade essentials) ----------------------
def test_restore_emits_import_telemetry(session: Session, user_id: UUID) -> None:
    """B9.4: the PRD promises EVERY import logs import_batch_id / parser / rows_in /
    rows_imported / rows_skipped. The restore path logged nothing — this module did not
    even import get_logger — so a "my restore silently dropped rows" report left only the
    middleware's request_completed line (status + duration).

    The arithmetic invariant is the point of the counts: every row in
    ``_persist_transactions`` leaves the loop as exactly one of imported / skipped_dupe /
    one txn warning. rows_rejected is therefore the TRANSACTION rejects only, not
    ``BackupImportResult.rows_rejected``, which also folds in the three parsers' own skips
    and the account-upsert warnings — neither of which is a transaction row, and folding
    them in would silently break the invariant a reader checks the event against.
    """
    _seed_source(session, user_id)
    session.commit()
    zip_bytes = build_backup_zip(session, user_id=user_id)

    with fresh_db() as (target, target_uid):
        with capture_logs() as logs:
            result = persist_backup(
                target,
                user_id=target_uid,
                parsed=parse_backup_zip(zip_bytes),
                source_file_hash="hash",
            )
        target.commit()

    events = [e for e in logs if e.get("event") == "import_completed"]
    assert len(events) == 1
    ev = events[0]
    assert ev["parser"] == "backup_csv"
    assert ev["import_batch_id"] == result.batch_id
    assert ev["rows_in"] == 5  # 5 confirmed; the pending row was never exported
    assert ev["rows_imported"] == result.txns_imported == 5
    assert ev["rows_skipped"] == result.txns_skipped_dupe == 0
    assert ev["rows_rejected"] == 0
    assert ev["status"] == "completed"
    assert ev["rows_in"] == ev["rows_imported"] + ev["rows_skipped"] + ev["rows_rejected"]

    # PII: the event carries counts and ids only — no merchant, PAN, account number or
    # card last-4. This asserts CALL-SITE HYGIENE, not masking: capture_logs swaps the
    # whole processor chain, so mask_pii never runs here (test_http_logging.py unit-tests
    # it directly for that reason). The seeded rows carry a real merchant and a last4, so
    # a call site that folded row content into the event would trip this.
    rendered = repr(ev).lower()
    for leaked in ("swiggy", "bigbasket", "acme payroll", "1234", "axis cc"):
        assert leaked not in rendered, f"{leaked!r} leaked into the import telemetry event"


def test_reimport_telemetry_reports_every_row_skipped(session: Session, user_id: UUID) -> None:
    """A second restore of the same zip imports nothing — and says so in the log, rather
    than emitting counts that look like a fresh import."""
    _seed_source(session, user_id)
    session.commit()
    zip_bytes = build_backup_zip(session, user_id=user_id)
    parsed = parse_backup_zip(zip_bytes)

    with fresh_db() as (target, target_uid):
        persist_backup(target, user_id=target_uid, parsed=parsed, source_file_hash="hash")
        target.commit()
        with capture_logs() as logs:
            persist_backup(
                target,
                user_id=target_uid,
                parsed=parse_backup_zip(zip_bytes),
                source_file_hash="hash",
            )
        target.commit()

    ev = next(e for e in logs if e.get("event") == "import_completed")
    assert ev["rows_in"] == 5
    assert ev["rows_imported"] == 0
    assert ev["rows_skipped"] == 5
    assert ev["rows_in"] == ev["rows_imported"] + ev["rows_skipped"] + ev["rows_rejected"]


# --- ADR-0006 rule 17: the coalesced dedup key on the backup path ----------------------


def test_reimport_of_a_stale_backup_restages_an_edited_row(session: Session, user_id: UUID) -> None:
    """Pins the residual gap for rows WITHOUT provenance, so a later change can't move it.

    A manual row has a NULL origin (rows with one carry it through backup, see
    ``test_round_trip_preserves_origin_fingerprint``), so its dedup key is its own *current*
    fingerprint. Edit it, then re-import a backup exported BEFORE the edit, and the
    stale line no longer matches anything and re-stages. Explicitly out of scope in the
    ADR: a backup file is a snapshot of our own data, not an immutable external artifact.
    Every unedited row still dedups, which is what keeps the gap narrow.
    """
    _seed_source(session, user_id)
    session.commit()
    zip_bytes = build_backup_zip(session, user_id=user_id)

    with fresh_db() as (target, target_uid):
        persist_backup(
            target, user_id=target_uid, parsed=parse_backup_zip(zip_bytes), source_file_hash="h"
        )
        target.commit()
        before = _txn_count(target, target_uid)

        swiggy = target.scalars(
            select(Transaction).where(
                Transaction.user_id == target_uid,
                Transaction.merchant_normalized == "swiggy",
            )
        ).one()
        swiggy.amount_paise = -55000
        swiggy.fingerprint = transaction_fingerprint(
            txn_date=swiggy.date,
            amount_paise=-55000,
            normalized_merchant=swiggy.merchant_normalized,
            account_id=swiggy.account_id,
        )
        swiggy.occurrence = 0
        target.commit()

        second = persist_backup(
            target, user_id=target_uid, parsed=parse_backup_zip(zip_bytes), source_file_hash="h"
        )
        target.commit()

        assert second.txns_imported == 1
        assert second.txns_skipped_dupe == before - 1
        assert _txn_count(target, target_uid) == before + 1


def test_dedups_against_an_imported_row_moved_to_another_account(
    session: Session, user_id: UUID
) -> None:
    """The backup dedup key is the bare coalesced fingerprint, not ``(account_id, fp)``.

    A natively-imported row carries ``origin_fingerprint``. Move it to another account
    and rule 3 recomputes ``fingerprint`` for the new account, so the stored
    ``account_id`` no longer agrees with the account its provenance was computed for.
    Keying on the pair would look the backup's line up under the OLD account and miss
    the moved row entirely — restoring a duplicate of a transaction the user only
    re-filed. The hash already encodes ``account_id``, so the pair bought nothing.
    """
    _seed_source(session, user_id)
    session.commit()
    zip_bytes = build_backup_zip(session, user_id=user_id)

    with fresh_db() as (target, target_uid):
        axis = Account(
            user_id=target_uid,
            name="Axis CC",
            type="credit_card",
            issuer="axis",
            last4="1234",
            opening_balance_paise=-500000,
        )
        other = Account(
            user_id=target_uid,
            name="Spare Card",
            type="credit_card",
            issuer="axis",
            last4="4321",
        )
        target.add_all([axis, other])
        target.flush()

        # A natively IMPORTED twin of the backup's swiggy spend: origin stamped at stage
        # time, equal to fingerprint at birth (import_service's contract).
        native = _add_txn(
            target,
            user_id=target_uid,
            account_id=axis.id,
            day=1,
            amount=-50000,
            txn_type="spend",
            merchant_norm="swiggy",
            merchant_raw="SWIGGY",
        )
        native.source = "import"
        native.origin_fingerprint = native.fingerprint
        target.commit()

        # The user re-files it onto the other card (ADR-0006 rules 3 and 17): the first
        # identity edit freezes the origin inputs, as PATCH does.
        _edit_like_patch(native, account_id=other.id)
        target.commit()
        assert native.origin_fingerprint != native.fingerprint

        result = persist_backup(
            target, user_id=target_uid, parsed=parse_backup_zip(zip_bytes), source_file_hash="h"
        )
        target.commit()

        # The backup's swiggy line is accounted for by the moved row's provenance, so it
        # does not restore a second copy.
        assert result.txns_skipped_dupe >= 1
        assert (
            _txn_count(target, target_uid, merchant_normalized="swiggy", amount_paise=-50000) == 1
        )


def test_backup_restore_hierarchical_categories_roundtrip(session: Session, user_id: UUID) -> None:
    """Two-level category hierarchy preserves parent-child relationships
    through export and import.

    Explicitly the export_service.py parent pull-in case (:130-142, pinned directly at
    ``test_export_pulls_in_a_parent_category_with_no_direct_transactions`` below): the only
    transaction here is tagged to ``sub_cat``, and ``parent_cat`` carries none of its own —
    so this also proves the pulled-in parent survives end to end, not just onto the wire.
    """
    axis = Account(
        user_id=user_id,
        name="Axis Bank",
        type="bank",
        opening_balance_paise=100000,
    )
    parent_cat = Category(
        user_id=user_id,
        name="Food & Dining",
        kind="spend",
        color="#4f46e5",
    )
    session.add_all([axis, parent_cat])
    session.flush()

    sub_cat = Category(
        user_id=user_id,
        name="Groceries",
        kind="spend",
        parent_id=parent_cat.id,
    )
    session.add(sub_cat)
    session.flush()

    _add_txn(
        session,
        user_id=user_id,
        account_id=axis.id,
        day=10,
        amount=-35000,
        txn_type="spend",
        merchant_norm="blinkit",
        category_id=sub_cat.id,
    )
    session.commit()

    zip_bytes = build_backup_zip(session, user_id=user_id)

    # Restore into fresh DB
    with fresh_db() as (target_session, target_uid):
        parsed = parse_backup_zip(zip_bytes)
        result = persist_backup(
            target_session,
            user_id=target_uid,
            parsed=parsed,
            source_file_hash="hierarchy-hash",
        )
        target_session.commit()

        assert result.categories_new == 2
        assert result.txns_imported == 1
        assert not result.warnings

        restored_parent = target_session.scalar(
            select(Category).where(Category.user_id == target_uid, Category.name == "Food & Dining")
        )
        restored_sub = target_session.scalar(
            select(Category).where(Category.user_id == target_uid, Category.name == "Groceries")
        )
        assert restored_parent is not None
        assert restored_sub is not None
        assert restored_parent.parent_id is None
        assert restored_sub.parent_id == restored_parent.id

        restored_txn = target_session.scalar(
            select(Transaction).where(Transaction.user_id == target_uid)
        )
        assert restored_txn is not None
        assert restored_txn.category_id == restored_sub.id


def test_export_pulls_in_a_parent_category_with_no_direct_transactions(
    session: Session, user_id: UUID
) -> None:
    """4.3: pin ``export_service.py``'s parent pull-in (:130-142) directly at the wire,
    independent of the full restore round trip above. Only the CHILD is referenced by any
    transaction; the parent has none of its own and must still land in ``categories.csv``
    with a resolvable ``parent_name``, or the child would restore as an orphaned root.
    """
    axis = Account(user_id=user_id, name="Axis Bank", type="bank", opening_balance_paise=0)
    parent = Category(user_id=user_id, name="Food & Dining", kind="spend", color="#4f46e5")
    session.add_all([axis, parent])
    session.flush()
    child = Category(user_id=user_id, name="Groceries", kind="spend", parent_id=parent.id)
    session.add(child)
    session.flush()
    _add_txn(
        session,
        user_id=user_id,
        account_id=axis.id,
        day=10,
        amount=-1000,
        txn_type="spend",
        merchant_norm="blinkit",
        category_id=child.id,  # only the child is referenced — parent has no direct txn
    )
    session.commit()

    zip_bytes = build_backup_zip(session, user_id=user_id)
    parsed_categories = parse_backup_zip(zip_bytes).categories

    assert {c.name for c in parsed_categories} == {"Food & Dining", "Groceries"}
    by_name = {c.name: c for c in parsed_categories}
    assert by_name["Food & Dining"].parent_name is None
    assert by_name["Groceries"].parent_name == "Food & Dining"


def test_backup_with_a_3_level_category_chain_flattens_the_grandchild_with_a_warning(
    session: Session, user_id: UUID
) -> None:
    """4.2: ADR-0012 caps depth at 2. A backup row whose ``parent_name`` names a row that
    is ITSELF a subcategory — a hand-edited CSV's declared threat model, or an ordering
    accident in a partial backup — must not be linked three deep. It flattens to a root,
    and says so in ``warnings``, rather than silently deepening the tree.

    Built via a hand-constructed ``ParsedBackup`` (bypassing the CSV/zip round trip):
    ``build_backup_zip`` itself only ever pulls in ONE hop (test above), so there is no
    way to *produce* a 3-level chain through the real export path — this drives the
    importer directly, the way a hand-edited backup would reach it.
    """
    parsed = ParsedBackup(
        accounts=[],
        categories=[
            ParsedBackupCategory(
                line_no=2, name="Food & Dining", kind="spend", color=None, archived_at=None
            ),
            ParsedBackupCategory(
                line_no=3,
                name="Groceries",
                kind="spend",
                color=None,
                archived_at=None,
                parent_name="Food & Dining",
            ),
            ParsedBackupCategory(
                line_no=4,
                name="Organic Groceries",
                kind="spend",
                color=None,
                archived_at=None,
                parent_name="Groceries",  # Groceries is itself a subcategory
            ),
        ],
        transactions=[],
        warnings=[],
    )

    result = persist_backup(session, user_id=user_id, parsed=parsed, source_file_hash="hash")
    session.commit()

    assert result.categories_new == 3
    # Exact string: the warning has to name BOTH the flattened row and the parent it was
    # refused, or a user reading it can't tell which row moved. The previous substring
    # triple ("Organic Groceries" not in w and "Groceries" in w and "subcategory" in w)
    # was a way to prove the message named the PARENT and not the child — but "Groceries"
    # is a substring of "Organic Groceries", so it pinned the child's absence as contract
    # and would have passed on an otherwise garbled message.
    assert result.warnings == [
        "categories.csv row 4: category 'Organic Groceries' — parent category 'Groceries' "
        "is itself a subcategory, restored as a root category (depth is capped at 2)"
    ]

    by_name = {
        c.name: c for c in session.scalars(select(Category).where(Category.user_id == user_id))
    }
    assert by_name["Groceries"].parent_id == by_name["Food & Dining"].id  # untouched, 2 levels
    assert by_name["Organic Groceries"].parent_id is None  # flattened, NOT nested 3 deep


def test_restore_gives_every_root_without_a_color_a_palette_color(
    session: Session, user_id: UUID
) -> None:
    """ADR-0012 rule 7: a restored root with no colour gets the least-used palette slot of its
    kind; a subcategory flattened to a root counts as one, and each kind has its own pool."""
    parsed = ParsedBackup(
        accounts=[],
        categories=[
            ParsedBackupCategory(
                line_no=2, name="Food & Dining", kind="spend", color=None, archived_at=None
            ),
            ParsedBackupCategory(
                line_no=3, name="Salary", kind="income", color=None, archived_at=None
            ),
            ParsedBackupCategory(
                line_no=4,
                name="Groceries",
                kind="spend",
                color=None,
                archived_at=None,
                parent_name="Absent",
            ),
        ],
        transactions=[],
        warnings=[],
    )

    persist_backup(session, user_id=user_id, parsed=parsed, source_file_hash="hash")
    session.commit()

    by_name = {
        c.name: c for c in session.scalars(select(Category).where(Category.user_id == user_id))
    }
    assert by_name["Food & Dining"].color == CATEGORY_PALETTE[0]
    assert by_name["Groceries"].parent_id is None
    assert by_name["Groceries"].color == CATEGORY_PALETTE[1]
    assert by_name["Salary"].color == CATEGORY_PALETTE[0]


def test_backup_with_an_unresolvable_parent_name_restores_a_root_with_a_warning(
    session: Session, user_id: UUID
) -> None:
    """ADR-0012 rule 13: a ``parent_name`` that names no category in the backup (or the
    user's existing set) flattens the row to a root and says so in ``warnings``, rather
    than dropping the row or failing the restore."""
    parsed = ParsedBackup(
        accounts=[],
        categories=[
            ParsedBackupCategory(
                line_no=2,
                name="Groceries",
                kind="spend",
                color=None,
                archived_at=None,
                parent_name="Food & Dining",  # absent from the backup
            ),
        ],
        transactions=[],
        warnings=[],
    )

    result = persist_backup(session, user_id=user_id, parsed=parsed, source_file_hash="hash")
    session.commit()

    assert result.categories_new == 1
    assert result.warnings == [
        "categories.csv row 2: category 'Groceries' — parent category 'Food & Dining' "
        "not found, restored as a root category"
    ]
    restored = session.scalar(
        select(Category).where(Category.user_id == user_id, Category.name == "Groceries")
    )
    assert restored is not None
    assert restored.parent_id is None


# --- A2 / A17: restore pairs only valid transfer legs; every other leg is kept unpaired ------


def _parsed_account(line_no: int, name: str, type_: str = "bank") -> ParsedBackupAccount:
    return ParsedBackupAccount(
        line_no=line_no,
        name=name,
        type=type_,  # ty: ignore[invalid-argument-type]
        issuer=None,
        last4=None,
        opening_balance_paise=0,
        currency="INR",
        archived_at=None,
    )


def _parsed_txn(
    line_no: int,
    *,
    amount: int,
    txn_type: str = "transfer",
    account: str = "HDFC Bank",
    group: str | None = None,
    pre_link: str | None = None,
    pre_link_category: tuple[str, str] | None = None,
    merchant: str | None = None,
    day: int = 4,
    confirmed: bool = True,
) -> ParsedBackupTransaction:
    return ParsedBackupTransaction(
        line_no=line_no,
        date=date(2026, 7, day),
        account_name=account,
        amount_paise=amount,
        transaction_type=txn_type,  # ty: ignore[invalid-argument-type]
        merchant_raw=None,
        merchant_normalized=merchant or f"leg {line_no}",
        category_name=None,
        category_kind=None,
        labels=(),
        source="manual",
        confirmed_at=datetime(2026, 7, day, 10, 0, 0) if confirmed else None,
        transfer_group=group,
        pre_link_type=pre_link,  # ty: ignore[invalid-argument-type]
        pre_link_category_name=pre_link_category[0] if pre_link_category else None,
        pre_link_category_kind=pre_link_category[1] if pre_link_category else None,  # ty: ignore[invalid-argument-type]
        row_ref=None,
        refund_of_ref=None,
        origin=None,
    )


_PAIR_WARNING = (
    "transactions.csv row {n}: transfer pair not restored — legs must be two confirmed transfers "
    "of equal and opposite amount on different accounts"
)


def _invalid_group_cases() -> dict[str, tuple[list[ParsedBackupTransaction], list[int]]]:
    """(legs of the invalid group, line numbers that must warn). Lines 2-3 are the valid pair."""
    return {
        "non_transfer": (
            [
                _parsed_txn(4, amount=-4000000, txn_type="spend", group="1", pre_link="spend"),
                _parsed_txn(5, amount=4000000, txn_type="income", account="Axis", group="1"),
            ],
            [4, 5],
        ),
        "unequal_amounts": (
            [
                _parsed_txn(4, amount=-100000, group="1"),
                _parsed_txn(5, amount=99999, account="Axis", group="1"),
            ],
            [4, 5],
        ),
        "same_account": (
            [
                _parsed_txn(4, amount=-100000, group="1"),
                _parsed_txn(5, amount=100000, group="1"),
            ],
            [4, 5],
        ),
        "three_legs": (
            [
                _parsed_txn(4, amount=-100000, group="1"),
                _parsed_txn(5, amount=100000, account="Axis", group="1"),
                _parsed_txn(6, amount=1, group="1"),
            ],
            [4, 5, 6],
        ),
    }


@pytest.mark.parametrize("case", list(_invalid_group_cases()))
def test_restore_never_pairs_a_non_transfer_leg(session: Session, user_id: UUID, case: str) -> None:
    """ADR-0002 rules 7 and 8: only two inserted transfer legs of equal and opposite amount on
    different accounts are paired. Anything else lands unpaired (it used to trip
    ``ck_transactions_paired_is_transfer`` at flush — a 500 with nothing written), keeps its
    file type, is released from any pre-link type, and warns once per leg."""
    bad_legs, warned_lines = _invalid_group_cases()[case]
    valid_pair = [
        _parsed_txn(2, amount=-100000, group="2", merchant="good out"),
        _parsed_txn(3, amount=100000, account="Axis", group="2", merchant="good in"),
    ]
    parsed = ParsedBackup(
        accounts=[_parsed_account(2, "HDFC Bank"), _parsed_account(3, "Axis", "credit_card")],
        categories=[],
        transactions=[*valid_pair, *bad_legs],
        warnings=[],
    )

    result = persist_backup(session, user_id=user_id, parsed=parsed, source_file_hash="h")
    session.commit()  # must not raise

    rows = {t.merchant_normalized: t for t in session.scalars(select(Transaction))}
    assert rows["good out"].transfer_pair_id == rows["good in"].id
    for leg in bad_legs:
        row = rows[leg.merchant_normalized]
        assert row.transfer_pair_id is None
        assert row.transaction_type == leg.transaction_type
        assert row.pre_link_type is None
    assert result.transfers_relinked == 2
    assert result.rows_rejected == 0
    assert result.links_not_restored == len(bad_legs)
    assert result.warnings == [_PAIR_WARNING.format(n=n) for n in warned_lines]


@pytest.mark.parametrize("case", ["first_pending", "both_pending"])
def test_restore_never_pairs_a_pending_leg(session: Session, user_id: UUID, case: str) -> None:
    """ADR-0002 rule 7: a leg restored with a blank ``confirmed_at`` is pending, and import cancel
    deletes pending rows, so a pair holding one would point a survivor at a deleted row. Both legs
    land unpaired, the ``pre_link`` leg gets its type back, and each warns once."""
    pending_both = case == "both_pending"
    out_leg = _parsed_txn(
        4, amount=-100000, group="1", pre_link="spend", merchant="pending out", confirmed=False
    )
    in_leg = _parsed_txn(
        5,
        amount=100000,
        account="Axis",
        group="1",
        merchant="pending in",
        confirmed=not pending_both,
    )
    parsed = ParsedBackup(
        accounts=[_parsed_account(2, "HDFC Bank"), _parsed_account(3, "Axis", "credit_card")],
        categories=[],
        transactions=[
            _parsed_txn(2, amount=-100000, group="2", merchant="good out"),
            _parsed_txn(3, amount=100000, account="Axis", group="2", merchant="good in"),
            out_leg,
            in_leg,
        ],
        warnings=[],
    )

    result = persist_backup(session, user_id=user_id, parsed=parsed, source_file_hash="h")
    session.commit()

    rows = {t.merchant_normalized: t for t in session.scalars(select(Transaction))}
    assert rows["good out"].transfer_pair_id == rows["good in"].id
    assert rows["pending out"].transfer_pair_id is None
    assert rows["pending in"].transfer_pair_id is None
    assert rows["pending out"].transaction_type == "spend"
    assert rows["pending out"].pre_link_type is None
    assert result.transfers_relinked == 2
    assert result.links_not_restored == 2
    assert result.warnings == [_PAIR_WARNING.format(n=n) for n in (4, 5)]


def test_restore_of_a_fully_deduped_pair_is_silent(session: Session, user_id: UUID) -> None:
    """Both legs deduped (an idempotent re-import) → no insert, no warning."""
    _seed_source(session, user_id)
    session.commit()
    zip_bytes = build_backup_zip(session, user_id=user_id)

    again = persist_backup(
        session, user_id=user_id, parsed=parse_backup_zip(zip_bytes), source_file_hash="h"
    )

    assert again.txns_imported == 0
    assert again.warnings == []
    assert again.links_not_restored == 0


def _lone_leg_target(target: Session, target_uid: UUID) -> None:
    """A target holding a native twin of ``_seed_source``'s outgoing transfer leg on a matching
    HDFC Bank, so that leg dedups and its partner lands alone."""
    hdfc = Account(
        user_id=target_uid,
        name="HDFC Bank",
        type="bank",
        issuer="hdfc",
        opening_balance_paise=100000,
    )
    target.add(hdfc)
    target.flush()
    _add_txn(
        target,
        user_id=target_uid,
        account_id=hdfc.id,
        day=4,
        amount=-100000,
        txn_type="transfer",
        merchant_norm="transfer to axis cc",
    )
    target.commit()


def test_restore_lone_leg_warns(session: Session, user_id: UUID) -> None:
    """ADR-0002 rule 7: a leg whose partner deduped against an existing row is kept unpaired
    and reported, not silently left half-restored."""
    _seed_source(session, user_id)
    session.commit()
    zip_bytes = build_backup_zip(session, user_id=user_id)

    with fresh_db() as (target, target_uid):
        _lone_leg_target(target, target_uid)
        parsed = parse_backup_zip(zip_bytes)
        result = persist_backup(target, user_id=target_uid, parsed=parsed, source_file_hash="h")
        target.commit()

        line = next(
            t.line_no
            for t in parsed.transactions
            if t.merchant_normalized == "transfer from hdfc bank"
        )
        assert result.txns_skipped_dupe == 1
        assert result.transfers_relinked == 0
        assert result.rows_rejected == 0
        assert result.links_not_restored == 1
        assert result.warnings == [
            f"transactions.csv row {line}: transfer partner not restored — left unpaired"
        ]
        assert _by_merchant(target, target_uid)["transfer from hdfc bank"].transfer_pair_id is None


def test_restore_lone_leg_gets_its_pre_link_type_back(session: Session, user_id: UUID) -> None:
    """ADR-0002 rule 9 on restore: a leg left unpaired is released like a partner-delete —
    a link-born leg returns to its pre-link type, an F2-born one stays a transfer."""
    _seed_source(session, user_id)
    session.commit()
    _link_refund_and_mark_legs(session, user_id)  # in-leg's pre_link_type = income
    zip_bytes = build_backup_zip(session, user_id=user_id)

    with fresh_db() as (target, target_uid):
        _lone_leg_target(target, target_uid)
        persist_backup(
            target, user_id=target_uid, parsed=parse_backup_zip(zip_bytes), source_file_hash="h"
        )
        target.commit()
        lone = _by_merchant(target, target_uid)["transfer from hdfc bank"]
        assert lone.transaction_type == "income"
        assert lone.pre_link_type is None

    # An F2-style leg (no pre-link type) stays a transfer.
    parsed = ParsedBackup(
        accounts=[_parsed_account(2, "HDFC Bank")],
        categories=[],
        transactions=[_parsed_txn(2, amount=-100000, group="9", merchant="lonely")],
        warnings=[],
    )
    result = persist_backup(session, user_id=user_id, parsed=parsed, source_file_hash="h")
    assert result.links_not_restored == 1
    assert _by_merchant(session, user_id)["lonely"].transaction_type == "transfer"


def test_restore_drops_a_pre_link_type_from_a_row_with_no_group(
    session: Session, user_id: UUID
) -> None:
    """Non-null ``pre_link_type`` implies a pair leg; a hand-edited row with no group never
    carries one."""
    parsed = ParsedBackup(
        accounts=[_parsed_account(2, "HDFC Bank")],
        categories=[],
        transactions=[_parsed_txn(2, amount=-5, txn_type="spend", pre_link="income")],
        warnings=[],
    )
    persist_backup(session, user_id=user_id, parsed=parsed, source_file_hash="h")
    assert session.scalars(select(Transaction)).one().pre_link_type is None


def test_restore_skips_a_zero_amount_row_with_a_warning(session: Session, user_id: UUID) -> None:
    """ADR-0009 rule 4: restore runs the same sign rule as every other write path, and a
    zero amount is a counted rejection like an unknown type."""
    zip_bytes = make_backup_zip(
        accounts=[account()],
        transactions=[txn(), txn(date="2026-07-02", amount_paise=0)],
    )

    result = persist_backup(
        session,
        user_id=user_id,
        parsed=parse_backup_zip(zip_bytes),
        source_file_hash="h",
    )
    session.commit()

    assert result.txns_imported == 1
    assert result.rows_rejected == 1
    assert result.warnings == ["transactions.csv row 3: amount_paise must be non-zero"]
    assert _txn_count(session, user_id, amount_paise=0) == 0


def test_restore_skips_a_row_whose_merchant_normalizes_past_512_chars(
    session: Session, user_id: UUID
) -> None:
    """ADR-0006 rule 3b: a merchant cell that normalizes past the String(512) columns is a
    counted rejection, never truncated. A blank ``merchant_raw`` (None) is not too long."""
    long_merchant = "\ufb2c" * 171
    zip_bytes = make_backup_zip(
        accounts=[account()],
        transactions=[
            txn(date="2026-07-01", merchant_raw=long_merchant, merchant_normalized=long_merchant),
            txn(date="2026-07-02"),
            txn(date="2026-07-03", merchant_raw="", merchant_normalized="cash"),
        ],
    )

    result = persist_backup(
        session,
        user_id=user_id,
        parsed=parse_backup_zip(zip_bytes),
        source_file_hash="h",
    )
    session.commit()

    assert result.txns_imported == 2
    assert result.rows_rejected == 1
    assert len(result.warnings) == 1
    assert result.warnings[0].endswith("merchant longer than 512 characters — skipped")
    assert _txn_count(session, user_id, date=date(2026, 7, 1)) == 0
    assert _txn_count(session, user_id, date=date(2026, 7, 2)) == 1
    assert _txn_count(session, user_id, date=date(2026, 7, 3)) == 1


# --- R6: a restored refund relinks to an original that deduped (ADR-0009 rule 13) ------------


def _axis_with_native_swiggy(target: Session, target_uid: UUID, *, copies: int = 1) -> list[int]:
    """A target holding a matching Axis CC and ``copies`` native twins of the swiggy original."""
    axis = Account(
        user_id=target_uid,
        name="Axis CC",
        type="credit_card",
        issuer="axis",
        last4="1234",
        opening_balance_paise=-500000,
    )
    target.add(axis)
    target.flush()
    rows = [
        _add_txn(
            target,
            user_id=target_uid,
            account_id=axis.id,
            day=1,
            amount=-50000,
            txn_type="spend",
            merchant_norm="swiggy",
            merchant_raw="SWIGGY",
            occurrence=occ,
        )
        for occ in range(copies)
    ]
    target.commit()
    return [r.id for r in rows]


def _linked_refund_zip(session: Session, user_id: UUID) -> bytes:
    _seed_source(session, user_id)
    session.commit()
    _link_refund_and_mark_legs(session, user_id)
    return build_backup_zip(session, user_id=user_id)


def test_restore_links_a_new_refund_to_a_deduped_original(session: Session, user_id: UUID) -> None:
    """The original already exists here, so it dedups; the refund that lands is linked to that
    existing row. Only the new refund is written — the existing original is untouched."""
    zip_bytes = _linked_refund_zip(session, user_id)

    with fresh_db() as (target, target_uid):
        (native_id,) = _axis_with_native_swiggy(target, target_uid)
        native = target.get(Transaction, native_id)
        assert native is not None
        updated_before = native.updated_at

        result = persist_backup(
            target, user_id=target_uid, parsed=parse_backup_zip(zip_bytes), source_file_hash="h"
        )
        target.commit()
        target.refresh(native)

        rows = _by_merchant(target, target_uid)
        assert result.txns_skipped_dupe == 1  # swiggy
        assert rows["bigbasket"].refund_of_id == native_id
        assert result.links_not_restored == 0
        assert result.warnings == []
        assert native.refund_of_id is None
        assert native.updated_at == updated_before


def test_restore_warns_when_the_deduped_original_is_ambiguous(
    session: Session, user_id: UUID
) -> None:
    """Two identical rows here and one in the file: which existing row is "the" original is a
    guess, so the refund lands unlinked with a warning and neither row is touched."""
    zip_bytes = _linked_refund_zip(session, user_id)

    with fresh_db() as (target, target_uid):
        native_ids = _axis_with_native_swiggy(target, target_uid, copies=2)

        result = persist_backup(
            target, user_id=target_uid, parsed=parse_backup_zip(zip_bytes), source_file_hash="h"
        )
        target.commit()

        assert _by_merchant(target, target_uid)["bigbasket"].refund_of_id is None
        assert result.links_not_restored == 1
        assert result.rows_rejected == 0
        assert [w.endswith("refund link not restored") for w in result.warnings] == [True]
        assert {
            t.refund_of_id
            for t in target.scalars(select(Transaction).where(Transaction.id.in_(native_ids)))
        } == {None}


def test_restore_warns_when_a_pre_existing_refund_is_unlinked(
    session: Session, user_id: UUID
) -> None:
    """The refund row itself deduped and has no link: writing one would mutate a pre-existing
    row, which restore never does, so it warns."""
    zip_bytes = _linked_refund_zip(session, user_id)

    with fresh_db() as (target, target_uid):
        _axis_with_native_swiggy(target, target_uid, copies=0)  # the restore inserts the original
        axis = target.scalars(select(Account)).one()
        refund = _add_txn(
            target,
            user_id=target_uid,
            account_id=axis.id,
            day=2,
            amount=10000,
            txn_type="spend",
            merchant_norm="bigbasket",
        )
        target.commit()

        result = persist_backup(
            target, user_id=target_uid, parsed=parse_backup_zip(zip_bytes), source_file_hash="h"
        )
        target.commit()
        target.refresh(refund)

        assert refund.refund_of_id is None
        assert result.links_not_restored == 1
        assert result.txns_skipped_dupe == 1


def test_reimport_of_linked_refunds_is_silent(session: Session, user_id: UUID) -> None:
    """Restoring the same zip twice: the second pass dedups every row, and a deduped refund
    that already carries its link needs nothing — no warning (PRD Verification 7)."""
    zip_bytes = _linked_refund_zip(session, user_id)

    with fresh_db() as (target, target_uid):
        persist_backup(
            target, user_id=target_uid, parsed=parse_backup_zip(zip_bytes), source_file_hash="h"
        )
        target.commit()
        again = persist_backup(
            target, user_id=target_uid, parsed=parse_backup_zip(zip_bytes), source_file_hash="h"
        )
        target.commit()

        assert again.txns_imported == 0
        assert again.warnings == []
        assert again.links_not_restored == 0


# --- A14: backups carry provenance as inputs; restore recomputes it (ADR-0006 rule 17) -------


def _edit_like_patch(
    txn: Transaction, *, amount: int | None = None, account_id: int | None = None
) -> None:
    """What ``PATCH /transactions`` does to an imported row: freeze the origin inputs on the
    first identity edit, then change the identity and recompute the fingerprint (occurrence 0)."""
    if txn.origin_date is None:
        txn.origin_date, txn.origin_amount_paise = txn.date, txn.amount_paise
        txn.origin_merchant_normalized, txn.origin_account_id = (
            txn.merchant_normalized,
            txn.account_id,
        )
    if amount is not None:
        txn.amount_paise = amount
    if account_id is not None:
        txn.account_id = account_id
    txn.fingerprint = transaction_fingerprint(
        txn_date=txn.date,
        amount_paise=txn.amount_paise,
        normalized_merchant=txn.merchant_normalized,
        account_id=txn.account_id,
    )
    txn.occurrence = 0


def _seed_origin_source(session: Session, user_id: UUID) -> tuple[Account, Account]:
    """Axis CC (carrying no current row) and HDFC Bank with three rows: R1 an unedited import,
    R2 an import edited in amount AND moved Axis CC -> HDFC Bank, R3 a manual row."""
    axis = Account(user_id=user_id, name="Axis CC", type="credit_card", issuer="axis")
    hdfc = Account(user_id=user_id, name="HDFC Bank", type="bank", issuer="hdfc")
    session.add_all([axis, hdfc])
    session.flush()
    r1 = _add_txn(
        session, user_id=user_id, account_id=hdfc.id, day=1, amount=-50000, txn_type="spend",
        merchant_norm="swiggy",
    )  # fmt: skip
    r1.origin_fingerprint = r1.fingerprint
    r2 = _add_txn(
        session, user_id=user_id, account_id=axis.id, day=2, amount=-20000, txn_type="spend",
        merchant_norm="uber",
    )  # fmt: skip
    r2.origin_fingerprint = r2.fingerprint
    _add_txn(
        session, user_id=user_id, account_id=hdfc.id, day=3, amount=-1000, txn_type="spend",
        merchant_norm="manual shop",
    )  # fmt: skip
    session.flush()
    _edit_like_patch(r2, amount=-25000, account_id=hdfc.id)
    session.commit()
    return axis, hdfc


def _h(day: int, amount: int, merchant: str, account_id: int) -> str:
    return transaction_fingerprint(
        txn_date=date(2026, 7, day),
        amount_paise=amount,
        normalized_merchant=merchant,
        account_id=account_id,
    )


def test_round_trip_preserves_origin_fingerprint(session: Session, user_id: UUID) -> None:
    """ADR-0006 rule 17: provenance survives backup and restore as recomputable inputs, on an
    install whose account ids differ — never as a copied hash (which bakes in the source's
    account id), so the zip carries no hash at all."""
    _seed_origin_source(session, user_id)
    zip_bytes = build_backup_zip(session, user_id=user_id)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for name in zf.namelist():
            assert not re.search(r"[0-9a-f]{64}", zf.read(name).decode()), name

    with fresh_db() as (target, target_uid):
        target.add(Account(user_id=target_uid, name="Spare", type="bank"))  # shift the ids
        target.flush()
        result = persist_backup(
            target, user_id=target_uid, parsed=parse_backup_zip(zip_bytes), source_file_hash="h"
        )
        target.commit()

        assert result.warnings == []
        accounts = {a.name: a.id for a in target.scalars(select(Account))}
        assert {"Axis CC", "HDFC Bank"} <= accounts.keys()  # Axis restored: an origin needs it
        rows = _by_merchant(target, target_uid)

        r1 = rows["swiggy"]
        expected_r1 = _h(1, -50000, "swiggy", accounts["HDFC Bank"])
        assert r1.fingerprint == r1.origin_fingerprint == expected_r1
        assert (r1.origin_date, r1.origin_account_id) == (None, None)

        r2 = rows["uber"]
        assert r2.fingerprint == _h(2, -25000, "uber", accounts["HDFC Bank"])
        assert r2.origin_fingerprint == _h(2, -20000, "uber", accounts["Axis CC"])
        assert (
            r2.origin_date,
            r2.origin_amount_paise,
            r2.origin_merchant_normalized,
            r2.origin_account_id,
        ) == (date(2026, 7, 2), -20000, "uber", accounts["Axis CC"])

        r3 = rows["manual shop"]
        assert (r3.origin_fingerprint, r3.origin_date, r3.origin_account_id) == (None, None, None)


def test_reimport_after_an_identity_edit_is_idempotent(session: Session, user_id: UUID) -> None:
    """Re-importing a backup on the SAME install used to duplicate every imported row whose
    identity had been edited: the incoming key was the recomputed current fingerprint while the
    stored row is counted under its origin. PRD Verification 7: load the same zip back → 0 new."""
    _seed_origin_source(session, user_id)
    before = _txn_count(session, user_id)
    zip_bytes = build_backup_zip(session, user_id=user_id)

    again = persist_backup(
        session, user_id=user_id, parsed=parse_backup_zip(zip_bytes), source_file_hash="h"
    )
    session.commit()

    assert again.txns_imported == 0
    assert again.txns_skipped_dupe == before
    assert again.warnings == []
    assert _txn_count(session, user_id) == before


def test_restored_edited_row_does_not_restage_on_statement_reimport(
    session: Session, user_id: UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restore the edited row into a misaligned install, then upload the statement its origin
    line came from: that line is accounted for by the recomputed origin, so nothing re-stages."""
    _seed_origin_source(session, user_id)
    zip_bytes = build_backup_zip(session, user_id=user_id)

    class Stub:
        @classmethod
        def parse(cls, pdf_bytes: bytes, password: str | None) -> ParsedStatement:
            line = RawTransaction(
                date=date(2026, 7, 2), amount_paise=-20000, merchant_raw="uber", txn_type="purchase"
            )
            return ParsedStatement(rows=[line], summary=StatementSummary())

    monkeypatch.setitem(import_service.PARSERS, ("axis", "credit_card"), Stub)

    with fresh_db() as (target, target_uid):
        target.add(Account(user_id=target_uid, name="Spare", type="bank"))
        target.flush()
        persist_backup(
            target, user_id=target_uid, parsed=parse_backup_zip(zip_bytes), source_file_hash="h"
        )
        target.commit()
        axis = target.scalars(select(Account).where(Account.name == "Axis CC")).one()

        result = import_service.import_statement(
            user_id=target_uid,
            account_id=axis.id,
            file_bytes=b"file",
            password=None,
            session=target,
        )
        target.commit()

        assert result.imported == 0
        assert result.skipped == 1


def test_restore_rejects_a_row_whose_origin_account_is_missing(
    session: Session, user_id: UUID
) -> None:
    """An origin that cannot resolve would restore the row without provenance — exactly the
    re-stage defect A14 fixes — so the row is a counted rejection instead."""
    zip_bytes = make_backup_zip(
        accounts=[account()],
        transactions=[
            txn(
                origin_date="2026-07-01",
                origin_amount_paise=-1,
                origin_merchant_normalized="swiggy",
                origin_account_name="Ghost Card",
            )
        ],
    )

    result = persist_backup(
        session, user_id=user_id, parsed=parse_backup_zip(zip_bytes), source_file_hash="h"
    )

    assert result.txns_imported == 0
    assert result.rows_rejected == 1
    assert result.warnings == ["transactions.csv row 2: origin account not found — skipped"]
