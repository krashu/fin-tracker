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
from app.parsers.backup_csv import (
    ACCOUNTS_CSV,
    CATEGORIES_CSV,
    METADATA_JSON,
    TRANSACTIONS_CSV,
    ParsedBackup,
    ParsedBackupCategory,
    _parse_dt,
    parse_backup_zip,
)
from app.services.backup_import_service import persist_backup
from app.services.export_service import build_backup_zip
from app.services.fingerprint import transaction_fingerprint


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
    salary = Category(user_id=user_id, name="Salary", kind="income")
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


# --- ADR-0007 rule 9: the coalesced dedup key on the backup path ----------------------


def test_restored_rows_keep_a_null_origin_fingerprint(session: Session, user_id: UUID) -> None:
    """Rule 9: a backup zip is a snapshot of our own data, not an external artifact.

    ``source`` round-trips through the CSV, so a restored row can read ``"import"`` —
    but provenance must still be NULL, so the row keys on its own current assertion.
    Stamping here would freeze a hash the file never promised.
    """
    _seed_source(session, user_id)
    session.commit()
    zip_bytes = build_backup_zip(session, user_id=user_id)

    with fresh_db() as (target, target_uid):
        persist_backup(
            target, user_id=target_uid, parsed=parse_backup_zip(zip_bytes), source_file_hash="h"
        )
        target.commit()

        txns = target.scalars(select(Transaction).where(Transaction.user_id == target_uid)).all()
        assert txns
        assert all(t.origin_fingerprint is None for t in txns)
        # At least one restored row carries source="import" — i.e. NULL origin is a
        # deliberate decision about the code path, not a side effect of `source`.
        assert any(t.source == "import" for t in txns)


def test_reimport_of_a_stale_backup_restages_an_edited_row(session: Session, user_id: UUID) -> None:
    """Pins ADR-0007 rule 9's documented residual gap, so a later change can't move it.

    A restored (or manual) row has a NULL origin, so its dedup key is its own *current*
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

        # The user re-files it onto the other card (ADR-0007 rule 6 + rule 3).
        native.account_id = other.id
        native.fingerprint = transaction_fingerprint(
            txn_date=native.date,
            amount_paise=native.amount_paise,
            normalized_merchant=native.merchant_normalized,
            account_id=other.id,
        )
        native.occurrence = 0
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


# --- T4: the read-only legacy `refund` alias (ADR-0009) --------------------------------


def _legacy_refund_zip() -> bytes:
    """A hand-built zip standing in for a real backup exported BEFORE ADR-0009 —
    ``transaction_type=refund``. Deliberately NOT a round trip through
    ``build_backup_zip``: export dumps the column verbatim and never emits
    ``refund`` any more, so there is no way to produce this shape from the
    current export path. Mirrors the header/row shape ``tests/parsers/
    test_backup_csv.py`` uses for the parser-level half of this test.
    """
    accounts = (
        "name,type,issuer,last4,opening_balance_paise,currency,archived_at\n"
        "Axis CC,credit_card,axis,1234,-50000,INR,\n"
    )
    categories = "name,kind,color,archived_at\nFood,spend,#4f46e5,\n"
    transactions = (
        "date,account_name,amount_paise,transaction_type,merchant_raw,"
        "merchant_normalized,category_name,category_kind,labels,source,confirmed_at,"
        "transfer_group\n"
        "2026-07-01,Axis CC,50000,refund,SWIGGY,swiggy,Food,spend,,import,"
        "2026-07-01T10:00:00,\n"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(ACCOUNTS_CSV, accounts)
        zf.writestr(CATEGORIES_CSV, categories)
        zf.writestr(TRANSACTIONS_CSV, transactions)
        zf.writestr(METADATA_JSON, "{}")
    return buffer.getvalue()


def test_restore_of_a_legacy_refund_typed_row_stores_it_as_spend(
    session: Session, user_id: UUID
) -> None:
    """T4, end to end. ``tests/parsers/test_backup_csv.py`` proves the parser's
    legacy alias maps ``refund`` → ``spend`` on the parsed row; this proves that
    reaches the actual STORED transaction, not just the parsed intermediate —
    ``_persist_transactions`` writes ``row.transaction_type`` verbatim
    (``app/services/backup_import_service.py``), so the alias is the whole fix
    and there is no second place a stale value could leak through.
    """
    result = persist_backup(
        session,
        user_id=user_id,
        parsed=parse_backup_zip(_legacy_refund_zip()),
        source_file_hash="legacy-refund-hash",
    )
    session.commit()
    assert result.txns_imported == 1
    assert not result.warnings

    txn = session.scalar(select(Transaction).where(Transaction.user_id == user_id))
    assert txn is not None
    assert txn.transaction_type == "spend"
    assert txn.amount_paise == 50000  # unchanged — the alias never re-signs.


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
