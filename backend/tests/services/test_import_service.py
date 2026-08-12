"""Unit tests for :mod:`app.services.import_service`.

The e2e test in :mod:`tests.api.test_imports` exercises the happy path,
batch short-circuit, row-level dedup, and multi-card scoping via the
HTTP layer. These tests cover branches the StubParser fixture there
does not naturally hit: the full ``_map_type`` table, ``LookupError``
on missing account or unknown parser, and zero-paise row skipping.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Account, Category, MerchantAlias, MerchantTagMap, Transaction, User
from app.parsers import ParsedStatement, RawTransaction, StatementSummary
from app.services import import_service
from app.services.fingerprint import transaction_fingerprint
from app.services.import_service import (
    AccountNotFoundError,
    ParserNotRegisteredError,
    _map_type,
    import_statement,
)
from app.services.merchant import normalize_merchant


@pytest.fixture
def seeded(session: Session) -> tuple[User, Account]:
    user = User(id=get_settings().v1_user_id)
    session.add(user)
    session.flush()
    account = Account(
        user_id=user.id,
        name="Axis CC",
        type="credit_card",
        issuer="axis",
        last4="1234",
    )
    session.add(account)
    session.commit()
    session.refresh(account)
    return user, account


@pytest.mark.parametrize(
    ("txn_type", "amount_paise", "merchant_raw", "expected"),
    [
        ("purchase", -8500, "X", "spend"),
        ("payment", 100000, "X", "income"),
        # A parser-flagged refund is spend-typed too (ADR-0009) — its positive
        # sign, not the type, is what makes it a refund.
        ("refund", 5000, "X", "spend"),
        ("other", -25000, "X", "spend"),  # finance charge, fee — debit
        # An unmatched credit-card credit — vocabulary the parser's _REFUND_RE
        # missed — defaults to spend, not income (PRD §F5: its positive sign
        # makes it a refund; the user corrects the rare genuine-cashback case;
        # a wrong income default would inflate spend by the credit's full
        # magnitude, per §F4a note 3).
        ("other", 5000, "X", "spend"),
        # ...unless the description names it cashback itself — then the keyword
        # is trusted over the generic spend fallback (is_cashback_credit).
        ("other", 5000, "ADDITIONAL CASHBACK FOR SWIGGY TRANSACTIONS", "income"),
        ("other", 5000, "cashback credit", "income"),  # case-insensitive
    ],
)
def test_map_type(txn_type: str, amount_paise: int, merchant_raw: str, expected: str) -> None:
    row = RawTransaction(
        date=date(2026, 3, 5),
        amount_paise=amount_paise,
        merchant_raw=merchant_raw,
        txn_type=txn_type,  # type: ignore[arg-type]
    )
    assert _map_type(row) == expected


def test_account_not_found_error_unknown_account(session: Session) -> None:
    # Both class AND message-format guarded — class drives the HTTP mapping,
    # the message format is what a future log/Sentry breadcrumb would surface.
    with pytest.raises(AccountNotFoundError, match=r"account_id=9999"):
        import_statement(
            user_id=get_settings().v1_user_id,
            account_id=9999,
            file_bytes=b"x",
            password=None,
            session=session,
        )


def test_parser_not_registered_error_unknown_issuer(
    session: Session, seeded: tuple[User, Account]
) -> None:
    user, _ = seeded
    unknown = Account(
        user_id=user.id,
        name="Unknown",
        type="credit_card",
        issuer="hdfc",
        last4="9999",
    )
    session.add(unknown)
    session.commit()
    session.refresh(unknown)

    with pytest.raises(ParserNotRegisteredError, match=r"no parser"):
        import_statement(
            user_id=user.id,
            account_id=unknown.id,
            file_bytes=b"x",
            password=None,
            session=session,
        )


def _stub_parser_with(rows: list[RawTransaction]) -> type:
    """Factory for a StubParser that returns ``rows`` unmodified.

    Shared by every test that needs to drive ``import_statement`` against
    a controlled row set without hitting a real PDF parser.
    """

    class StubParser:
        @classmethod
        def parse(cls, pdf_bytes: bytes, password: str | None) -> ParsedStatement:
            return ParsedStatement(rows=rows, summary=StatementSummary())

    return StubParser


def _identical_pair() -> list[RawTransaction]:
    """Two genuinely-distinct rows that share all four fingerprint inputs.

    The realistic Indian case: two auto rides at the same fare on one day.
    """
    return [
        RawTransaction(
            date=date(2026, 5, 4),
            amount_paise=-25000,
            merchant_raw="UBER INDIA",
            txn_type="purchase",
        ),
        RawTransaction(
            date=date(2026, 5, 4),
            amount_paise=-25000,
            merchant_raw="UBER INDIA",
            txn_type="purchase",
        ),
    ]


def test_two_identical_rows_in_one_statement_both_import(
    session: Session,
    seeded: tuple[User, Account],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D1: same-day duplicates are distinct events, not a dedup hit.

    Before ADR-0006 the second row was silently dropped: ``existing_fps`` was
    mutated inside the row loop, so row N was compared against rows 1..N-1 of
    the *same* statement. Both rows share one fingerprint and are told apart by
    ``occurrence``.
    """
    user, account = seeded
    monkeypatch.setitem(
        import_service.PARSERS, ("axis", "credit_card"), _stub_parser_with(_identical_pair())
    )

    result = import_statement(
        user_id=user.id,
        account_id=account.id,
        file_bytes=b"file",
        password=None,
        session=session,
    )
    session.commit()

    assert result.imported == 2
    assert result.skipped == 0

    persisted = session.scalars(select(Transaction)).all()
    assert len(persisted) == 2
    assert {t.occurrence for t in persisted} == {0, 1}
    # One fingerprint, two occurrences — identity is shared, multiplicity is not.
    assert len({t.fingerprint for t in persisted}) == 1


def test_reupload_of_a_file_with_duplicates_stages_nothing(
    session: Session,
    seeded: tuple[User, Account],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The re-upload invariant, for a duplicate group.

    n_file == n_db == 2, so the multiset difference is empty and nothing
    re-stages. This is what the old set-membership check got right by accident
    (and only because it had already collapsed the pair to one row).
    """
    user, account = seeded
    monkeypatch.setitem(
        import_service.PARSERS, ("axis", "credit_card"), _stub_parser_with(_identical_pair())
    )
    kwargs = {
        "user_id": user.id,
        "account_id": account.id,
        "file_bytes": b"file",
        "password": None,
    }
    import_statement(**kwargs, session=session)
    session.commit()

    again = import_statement(**kwargs, session=session)
    session.commit()

    assert again.imported == 0
    assert again.skipped == 2
    assert again.already_imported is True
    assert len(session.scalars(select(Transaction)).all()) == 2


def test_reupload_after_deleting_the_first_occurrence_restages_one(
    session: Session,
    seeded: tuple[User, Account],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deleting one of two duplicates and re-uploading re-stages exactly one row.

    This is the documented re-stage contract (see the module docstring: "rows the
    user discarded in the review queue … therefore re-surface") applied to a
    duplicate group — the same behaviour
    ``test_reimport_resurfaces_rows_missing_from_the_db`` pins for singletons, not
    a regression. Suppressing it would need soft-delete tombstones.

    The re-staged row takes occurrence 2, NOT the vacated 0: occurrences can be
    gapped, so the assignment tracks MAX rather than COUNT. Reusing 0 would be
    fine here, but reusing an *occupied* slot would trip the unique constraint,
    and MAX is what makes that impossible.
    """
    user, account = seeded
    monkeypatch.setitem(
        import_service.PARSERS, ("axis", "credit_card"), _stub_parser_with(_identical_pair())
    )
    kwargs = {
        "user_id": user.id,
        "account_id": account.id,
        "file_bytes": b"file",
        "password": None,
    }
    import_statement(**kwargs, session=session)
    session.commit()

    first = session.scalars(select(Transaction).where(Transaction.occurrence == 0)).one()
    session.delete(first)
    session.commit()

    again = import_statement(**kwargs, session=session)
    session.commit()

    assert again.imported == 1
    assert again.skipped == 1
    survivors = session.scalars(select(Transaction)).all()
    assert sorted(t.occurrence for t in survivors) == [1, 2]


def test_third_identical_row_from_a_later_statement_gets_a_fresh_occurrence(
    session: Session,
    seeded: tuple[User, Account],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later statement carrying 3 copies stages only the surplus one."""
    user, account = seeded
    monkeypatch.setitem(
        import_service.PARSERS, ("axis", "credit_card"), _stub_parser_with(_identical_pair())
    )
    import_statement(
        user_id=user.id,
        account_id=account.id,
        file_bytes=b"file-a",
        password=None,
        session=session,
    )
    session.commit()

    monkeypatch.setitem(
        import_service.PARSERS,
        ("axis", "credit_card"),
        _stub_parser_with([*_identical_pair(), _identical_pair()[0]]),
    )
    result = import_statement(
        user_id=user.id,
        account_id=account.id,
        file_bytes=b"file-b",  # distinct bytes → a new batch, not a re-upload
        password=None,
        session=session,
    )
    session.commit()

    assert result.imported == 1
    assert result.skipped == 2
    assert sorted(t.occurrence for t in session.scalars(select(Transaction)).all()) == [0, 1, 2]


def test_file_with_fewer_copies_than_the_db_stages_nothing(
    session: Session,
    seeded: tuple[User, Account],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """n_file < n_db → max(0, n_file - n_db) == 0, no negative-surplus bug."""
    user, account = seeded
    monkeypatch.setitem(
        import_service.PARSERS, ("axis", "credit_card"), _stub_parser_with(_identical_pair())
    )
    import_statement(
        user_id=user.id,
        account_id=account.id,
        file_bytes=b"file-a",
        password=None,
        session=session,
    )
    session.commit()

    monkeypatch.setitem(
        import_service.PARSERS,
        ("axis", "credit_card"),
        _stub_parser_with([_identical_pair()[0]]),
    )
    result = import_statement(
        user_id=user.id,
        account_id=account.id,
        file_bytes=b"file-b",
        password=None,
        session=session,
    )
    session.commit()

    assert result.imported == 0
    assert result.skipped == 1
    assert len(session.scalars(select(Transaction)).all()) == 2


def test_zero_paise_row_skipped(
    session: Session,
    seeded: tuple[User, Account],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user, account = seeded
    monkeypatch.setitem(
        import_service.PARSERS,
        ("axis", "credit_card"),
        _stub_parser_with(
            [
                RawTransaction(
                    date=date(2026, 3, 5),
                    amount_paise=-8500,
                    merchant_raw="REAL TXN",
                    txn_type="purchase",
                ),
                # Zero-paise other row: skipped before fingerprinting.
                RawTransaction(
                    date=date(2026, 3, 6),
                    amount_paise=0,
                    merchant_raw="ZERO ADJUSTMENT",
                    txn_type="other",
                ),
            ]
        ),
    )

    result = import_statement(
        user_id=user.id,
        account_id=account.id,
        file_bytes=b"file",
        password=None,
        session=session,
    )
    session.commit()

    assert result.imported == 1
    assert result.skipped == 1
    assert result.already_imported is False

    persisted = session.scalars(select(Transaction)).all()
    assert len(persisted) == 1
    assert persisted[0].merchant_raw == "REAL TXN"


def test_import_completed_telemetry(
    session: Session,
    seeded: tuple[User, Account],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # PRD §Production-grade essentials: import operations log import_batch_id,
    # parser, rows_in, rows_imported, rows_skipped. 1 real + 1 zero-paise row.
    user, account = seeded
    monkeypatch.setitem(
        import_service.PARSERS,
        ("axis", "credit_card"),
        _stub_parser_with(
            [
                RawTransaction(
                    date=date(2026, 3, 5),
                    amount_paise=-8500,
                    merchant_raw="REAL TXN",
                    txn_type="purchase",
                ),
                RawTransaction(
                    date=date(2026, 3, 6),
                    amount_paise=0,
                    merchant_raw="ZERO ADJUSTMENT",
                    txn_type="other",
                ),
            ]
        ),
    )

    with structlog.testing.capture_logs() as logs:
        result = import_statement(
            user_id=user.id,
            account_id=account.id,
            file_bytes=b"file",
            password=None,
            session=session,
        )
    session.commit()

    events = [e for e in logs if e.get("event") == "import_completed"]
    assert len(events) == 1
    ev = events[0]
    assert ev["parser"] == "StubParser"
    assert ev["import_batch_id"] == result.batch_id
    assert ev["rows_in"] == 2
    assert ev["rows_imported"] == result.imported == 1
    assert ev["rows_skipped"] == result.skipped == 1
    # Invariant a reader sanity-checks against the numbers.
    assert ev["rows_in"] == ev["rows_imported"] + ev["rows_skipped"]


# ---------- F3 auto-tag at import time ------------------------------------


def test_import_auto_tags_from_existing_map(
    session: Session,
    seeded: tuple[User, Account],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user, account = seeded
    food = Category(user_id=user.id, name="Food")
    session.add(food)
    session.commit()
    session.add(
        MerchantTagMap(
            user_id=user.id,
            merchant_normalized="swiggy",
            category_id=food.id,
            hit_count=4,
        )
    )
    session.commit()

    monkeypatch.setitem(
        import_service.PARSERS,
        ("axis", "credit_card"),
        _stub_parser_with(
            [
                RawTransaction(
                    date=date(2026, 3, 5),
                    amount_paise=-8500,
                    merchant_raw="Swiggy",
                    txn_type="purchase",
                ),
            ]
        ),
    )

    import_statement(
        user_id=user.id,
        account_id=account.id,
        file_bytes=b"file",
        password=None,
        session=session,
    )
    session.commit()

    txn = session.scalar(select(Transaction))
    assert txn is not None
    assert txn.category_id == food.id
    # auto_category_id freezes the suggestion for the acceptance-rate metric
    # (GET /dashboards/tagging-stats). It mirrors category_id at ingest and is
    # never touched by a later PATCH.
    assert txn.auto_category_id == food.id


def test_import_prefills_via_alias(
    session: Session,
    seeded: tuple[User, Account],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of Phase A2: a raw descriptor that never literally
    matches any merchant_tag_map row still prefills, because it tokenizes
    through an alias to a canonical the map WAS taught under."""
    user, account = seeded
    food = Category(user_id=user.id, name="Food")
    session.add(food)
    session.commit()
    session.add(MerchantAlias(user_id=user.id, pattern="swiggy", canonical="food delivery"))
    session.add(
        MerchantTagMap(
            user_id=user.id,
            merchant_normalized="food delivery",
            category_id=food.id,
            hit_count=4,
        )
    )
    session.commit()

    monkeypatch.setitem(
        import_service.PARSERS,
        ("axis", "credit_card"),
        _stub_parser_with(
            [
                RawTransaction(
                    date=date(2026, 3, 5),
                    amount_paise=-8500,
                    merchant_raw="swiggy*blr*99999",
                    txn_type="purchase",
                ),
            ]
        ),
    )

    import_statement(
        user_id=user.id,
        account_id=account.id,
        file_bytes=b"file",
        password=None,
        session=session,
    )
    session.commit()

    txn = session.scalar(select(Transaction))
    assert txn is not None
    assert txn.category_id == food.id
    assert txn.auto_category_id == food.id
    # merchant_normalized still stores normalize_merchant's output (lowercase +
    # whitespace collapse only -- normalize_merchant does not strip "*"), never
    # the canonical: it is a read-time key only, never persisted on the row.
    assert txn.merchant_normalized == "swiggy*blr*99999"


def test_fingerprint_is_byte_identical_with_full_alias_table_loaded(
    session: Session,
    seeded: tuple[User, Account],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Required test 5 (ADR-0006 guard) -- a REAL comparison: every stored
    fingerprint must equal transaction_fingerprint() computed directly from
    normalize_merchant(row.merchant_raw), independent of the resolver, even
    with a full alias table loaded."""
    user, account = seeded
    session.add_all(
        [
            MerchantAlias(user_id=user.id, pattern="swiggy", canonical="food"),
            MerchantAlias(user_id=user.id, pattern="ola", canonical="transport"),
            MerchantAlias(user_id=user.id, pattern="big basket", canonical="groceries"),
        ]
    )
    session.commit()

    rows = [
        RawTransaction(
            date=date(2026, 3, 5),
            amount_paise=-8500,
            merchant_raw="Swiggy*BLR*11111",
            txn_type="purchase",
        ),
        RawTransaction(
            date=date(2026, 3, 6),
            amount_paise=-1200,
            merchant_raw="UPI/OLA/2222@ybl",
            txn_type="purchase",
        ),
        RawTransaction(
            date=date(2026, 3, 7),
            amount_paise=-3400,
            merchant_raw="Big Basket Online",
            txn_type="purchase",
        ),
    ]
    expected_fps = {
        r.merchant_raw: transaction_fingerprint(
            txn_date=r.date,
            amount_paise=r.amount_paise,
            normalized_merchant=normalize_merchant(r.merchant_raw),
            account_id=account.id,
        )
        for r in rows
    }
    monkeypatch.setitem(import_service.PARSERS, ("axis", "credit_card"), _stub_parser_with(rows))

    import_statement(
        user_id=user.id,
        account_id=account.id,
        file_bytes=b"file",
        password=None,
        session=session,
    )
    session.commit()

    stored = {t.merchant_raw: t.fingerprint for t in session.scalars(select(Transaction)).all()}
    assert stored == expected_fps


def test_import_skips_auto_tag_for_income_and_transfer(
    session: Session,
    seeded: tuple[User, Account],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Income + transfer-styled rows must persist with category_id=None even
    when the merchant has a map entry. PRD §F3 restricts auto-tag to
    "spending transactions" — read as spend + refund only.
    """
    user, account = seeded
    salary_cat = Category(user_id=user.id, name="Income")
    session.add(salary_cat)
    session.commit()
    session.add(
        MerchantTagMap(
            user_id=user.id,
            merchant_normalized="acme corp",
            category_id=salary_cat.id,
            hit_count=10,
        )
    )
    session.commit()

    monkeypatch.setitem(
        import_service.PARSERS,
        ("axis", "credit_card"),
        _stub_parser_with(
            [
                # ``payment`` → "income" via _map_type — must NOT auto-tag.
                RawTransaction(
                    date=date(2026, 3, 5),
                    amount_paise=100000,
                    merchant_raw="ACME Corp",
                    txn_type="payment",
                ),
            ]
        ),
    )

    import_statement(
        user_id=user.id,
        account_id=account.id,
        file_bytes=b"file",
        password=None,
        session=session,
    )
    session.commit()

    txn = session.scalar(select(Transaction))
    assert txn is not None
    assert txn.transaction_type == "income"
    assert txn.category_id is None
    # No auto-tag for income → the frozen suggestion is NULL too (excluded from
    # the acceptance-metric denominator).
    assert txn.auto_category_id is None


def test_import_auto_tags_refund_row(
    session: Session,
    seeded: tuple[User, Account],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user, account = seeded
    food = Category(user_id=user.id, name="Food")
    session.add(food)
    session.commit()
    session.add(
        MerchantTagMap(
            user_id=user.id,
            merchant_normalized="swiggy",
            category_id=food.id,
            hit_count=3,
        )
    )
    session.commit()

    monkeypatch.setitem(
        import_service.PARSERS,
        ("axis", "credit_card"),
        _stub_parser_with(
            [
                RawTransaction(
                    date=date(2026, 3, 5),
                    amount_paise=5000,
                    merchant_raw="Swiggy",
                    txn_type="refund",
                ),
            ]
        ),
    )

    import_statement(
        user_id=user.id,
        account_id=account.id,
        file_bytes=b"file",
        password=None,
        session=session,
    )
    session.commit()

    txn = session.scalar(select(Transaction))
    assert txn is not None
    assert txn.transaction_type == "spend"
    assert txn.amount_paise == 5000  # the positive sign IS the refund (ADR-0009)
    assert txn.category_id == food.id


def test_import_no_match_leaves_category_null(
    session: Session,
    seeded: tuple[User, Account],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user, account = seeded
    monkeypatch.setitem(
        import_service.PARSERS,
        ("axis", "credit_card"),
        _stub_parser_with(
            [
                RawTransaction(
                    date=date(2026, 3, 5),
                    amount_paise=-8500,
                    merchant_raw="UnknownMerchant",
                    txn_type="purchase",
                ),
            ]
        ),
    )

    import_statement(
        user_id=user.id,
        account_id=account.id,
        file_bytes=b"file",
        password=None,
        session=session,
    )
    session.commit()

    txn = session.scalar(select(Transaction))
    assert txn is not None
    assert txn.category_id is None


def test_import_auto_tag_picks_highest_hit_count_per_merchant(
    session: Session,
    seeded: tuple[User, Account],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two map rows for one merchant → prefetch reducer picks the winner."""
    user, account = seeded
    food = Category(user_id=user.id, name="Food")
    subs = Category(user_id=user.id, name="Subscriptions")
    session.add_all([food, subs])
    session.commit()
    session.add_all(
        [
            MerchantTagMap(
                user_id=user.id,
                merchant_normalized="swiggy",
                category_id=food.id,
                hit_count=5,
            ),
            MerchantTagMap(
                user_id=user.id,
                merchant_normalized="swiggy",
                category_id=subs.id,
                hit_count=2,
            ),
        ]
    )
    session.commit()

    monkeypatch.setitem(
        import_service.PARSERS,
        ("axis", "credit_card"),
        _stub_parser_with(
            [
                RawTransaction(
                    date=date(2026, 3, 5),
                    amount_paise=-8500,
                    merchant_raw="Swiggy",
                    txn_type="purchase",
                ),
            ]
        ),
    )

    import_statement(
        user_id=user.id,
        account_id=account.id,
        file_bytes=b"file",
        password=None,
        session=session,
    )
    session.commit()

    txn = session.scalar(select(Transaction))
    assert txn is not None
    assert txn.category_id == food.id


def test_reimport_does_not_resurface_tag_map_on_dedup_skipped_rows(
    session: Session,
    seeded: tuple[User, Account],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second import of the same bytes hits the batch short-circuit; existing
    txns aren't re-processed and the tag-map is untouched. Locks the contract
    against a future "move lookup before dedup for clarity" refactor that would
    silently re-fire the lookup on already-imported rows.
    """
    user, account = seeded
    food = Category(user_id=user.id, name="Food")
    session.add(food)
    session.commit()
    session.add(
        MerchantTagMap(
            user_id=user.id,
            merchant_normalized="swiggy",
            category_id=food.id,
            hit_count=1,
        )
    )
    session.commit()

    monkeypatch.setitem(
        import_service.PARSERS,
        ("axis", "credit_card"),
        _stub_parser_with(
            [
                RawTransaction(
                    date=date(2026, 3, 5),
                    amount_paise=-8500,
                    merchant_raw="Swiggy",
                    txn_type="purchase",
                ),
            ]
        ),
    )

    first = import_statement(
        user_id=user.id,
        account_id=account.id,
        file_bytes=b"file-v1",
        password=None,
        session=session,
    )
    session.commit()
    assert first.imported == 1
    assert first.already_imported is False

    # Second call: same bytes → re-parses and reconciles, but the one row is
    # still present (pending) so it's skipped, and record_tag never runs at
    # import time, so the tag-map is untouched either way.
    second = import_statement(
        user_id=user.id,
        account_id=account.id,
        file_bytes=b"file-v1",
        password=None,
        session=session,
    )
    session.commit()
    assert second.imported == 0
    assert second.already_imported is True

    # Tag-map untouched: hit_count still 1, still exactly one row.
    rows = session.scalars(select(MerchantTagMap)).all()
    assert len(rows) == 1
    assert rows[0].hit_count == 1


# --- ADR-0007 rule 9: origin_fingerprint provenance -----------------------------------


def _single_row(
    *,
    txn_date: date = date(2026, 5, 4),
    amount_paise: int = -25000,
    merchant_raw: str = "UBER INDIA",
) -> list[RawTransaction]:
    return [
        RawTransaction(
            date=txn_date,
            amount_paise=amount_paise,
            merchant_raw=merchant_raw,
            txn_type="purchase",
        )
    ]


def _simulate_identity_edit(
    session: Session,
    txn: Transaction,
    *,
    amount_paise: int | None = None,
    txn_date: date | None = None,
    account_id: int | None = None,
    merchant_raw: str | None = None,
) -> None:
    """Apply what ADR-0007 rule 3 says ``PATCH /transactions`` will do.

    The widened route lands in the NEXT commit, so these tests drive the service
    seam directly: mutate the identity column(s), recompute ``merchant_normalized``
    and ``fingerprint``, reset ``occurrence`` to 0 — and leave ``origin_fingerprint``
    strictly alone, which is the whole point of rule 9.
    """
    if amount_paise is not None:
        txn.amount_paise = amount_paise
    if txn_date is not None:
        txn.date = txn_date
    if account_id is not None:
        txn.account_id = account_id
    if merchant_raw is not None:
        txn.merchant_raw = merchant_raw
        txn.merchant_normalized = normalize_merchant(merchant_raw)
    txn.fingerprint = transaction_fingerprint(
        txn_date=txn.date,
        amount_paise=txn.amount_paise,
        normalized_merchant=txn.merchant_normalized,
        account_id=txn.account_id,
    )
    txn.occurrence = 0
    session.commit()


def test_import_stamps_origin_fingerprint_equal_to_fingerprint(
    session: Session,
    seeded: tuple[User, Account],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stamped at STAGE time, so a still-pending row already carries provenance."""
    user, account = seeded
    monkeypatch.setitem(
        import_service.PARSERS, ("axis", "credit_card"), _stub_parser_with(_single_row())
    )

    import_statement(
        user_id=user.id,
        account_id=account.id,
        file_bytes=b"file",
        password=None,
        session=session,
    )
    session.commit()

    txn = session.scalars(select(Transaction)).one()
    assert txn.confirmed_at is None  # pending — the stamp does not wait for commit
    assert txn.origin_fingerprint == txn.fingerprint


def test_edited_row_is_not_restaged_on_reimport(
    session: Session,
    seeded: tuple[User, Account],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0007 §Verification 1, service half — the widening is unsafe without this.

    Before rule 9 the DB held ``fp'`` while the file yielded ``fp``, so the corrected
    row re-staged wearing its original wrong value, indistinguishable in the queue
    from a deliberately-cancelled row. Committing that gave two rows for one real
    transaction whose fingerprints differ *by construction*, so F4 could never
    detect the duplicate.
    """
    user, account = seeded
    monkeypatch.setitem(
        import_service.PARSERS, ("axis", "credit_card"), _stub_parser_with(_single_row())
    )
    kwargs = {
        "user_id": user.id,
        "account_id": account.id,
        "file_bytes": b"file",
        "password": None,
    }
    import_statement(**kwargs, session=session)
    session.commit()

    txn = session.scalars(select(Transaction)).one()
    original_origin = txn.origin_fingerprint
    _simulate_identity_edit(session, txn, amount_paise=-27500)
    assert txn.fingerprint != txn.origin_fingerprint

    again = import_statement(**kwargs, session=session)
    session.commit()

    assert again.imported == 0
    assert again.skipped == 1
    assert len(session.scalars(select(Transaction)).all()) == 1
    # Provenance is immutable: the importer reads it, never rewrites it.
    assert session.scalars(select(Transaction)).one().origin_fingerprint == original_origin


def test_cancelled_rows_still_restage_after_an_edit(
    session: Session,
    seeded: tuple[User, Account],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of Verification 1: cancel is still not a tombstone.

    Rule 9 narrows exactly one thing — an *edit* no longer masquerades as a
    deletion. A row the user actually discarded re-surfaces as before.
    """
    user, account = seeded
    rows = [
        RawTransaction(
            date=date(2026, 5, 4), amount_paise=-25000, merchant_raw="UBER", txn_type="purchase"
        ),
        RawTransaction(
            date=date(2026, 5, 5), amount_paise=-40000, merchant_raw="SWIGGY", txn_type="purchase"
        ),
    ]
    monkeypatch.setitem(import_service.PARSERS, ("axis", "credit_card"), _stub_parser_with(rows))
    kwargs = {
        "user_id": user.id,
        "account_id": account.id,
        "file_bytes": b"file",
        "password": None,
    }
    import_statement(**kwargs, session=session)
    session.commit()

    kept, cancelled = session.scalars(select(Transaction).order_by(Transaction.date)).all()
    session.delete(cancelled)
    session.commit()
    _simulate_identity_edit(session, kept, amount_paise=-26000)

    again = import_statement(**kwargs, session=session)
    session.commit()

    # Exactly the cancelled row comes back; the edited one does not.
    assert again.imported == 1
    assert again.skipped == 1
    persisted = session.scalars(select(Transaction)).all()
    assert len(persisted) == 2
    assert {t.merchant_raw for t in persisted} == {"UBER", "SWIGGY"}


def test_manual_row_edited_then_statement_import_does_not_duplicate(
    session: Session,
    seeded: tuple[User, Account],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule 9's decisive case — why manual rows keep a NULL origin_fingerprint.

    The user logs a UPI spend manually as 500, corrects it to 550, then imports the
    statement carrying the real 550 line. With NULL + COALESCE the key is the row's
    *current* fingerprint, so the line matches and nothing stages. Had the row been
    stamped at creation, the key would be frozen at fp(500), the 550 line would look
    new, and the user would get a duplicate.
    """
    user, account = seeded
    manual = Transaction(
        user_id=user.id,
        account_id=account.id,
        date=date(2026, 5, 4),
        amount_paise=-50000,
        transaction_type="spend",
        merchant_raw="UBER INDIA",
        merchant_normalized=normalize_merchant("UBER INDIA"),
        fingerprint=transaction_fingerprint(
            txn_date=date(2026, 5, 4),
            amount_paise=-50000,
            normalized_merchant=normalize_merchant("UBER INDIA"),
            account_id=account.id,
        ),
        source="manual",
    )
    session.add(manual)
    session.commit()
    assert manual.origin_fingerprint is None

    _simulate_identity_edit(session, manual, amount_paise=-55000)

    monkeypatch.setitem(
        import_service.PARSERS,
        ("axis", "credit_card"),
        _stub_parser_with(_single_row(amount_paise=-55000)),
    )
    result = import_statement(
        user_id=user.id,
        account_id=account.id,
        file_bytes=b"file",
        password=None,
        session=session,
    )
    session.commit()

    assert result.imported == 0
    assert result.skipped == 1
    assert len(session.scalars(select(Transaction)).all()) == 1
    # Still NULL — the importer must not backfill provenance onto a manual row.
    assert session.scalars(select(Transaction)).one().origin_fingerprint is None


def test_duplicate_group_with_one_edited_row_accounts_for_every_line(
    session: Session,
    seeded: tuple[User, Account],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule 9 bullet 5: N identical lines, one edited — db_count stays N.

    All N rows still coalesce to the same key, so the file's N lines are all
    accounted for and nothing re-stages, even though the group's rows no longer
    agree on ``fingerprint``.
    """
    user, account = seeded
    monkeypatch.setitem(
        import_service.PARSERS, ("axis", "credit_card"), _stub_parser_with(_identical_pair())
    )
    kwargs = {
        "user_id": user.id,
        "account_id": account.id,
        "file_bytes": b"file",
        "password": None,
    }
    import_statement(**kwargs, session=session)
    session.commit()

    edited = session.scalars(select(Transaction).where(Transaction.occurrence == 1)).one()
    _simulate_identity_edit(session, edited, amount_paise=-30000)

    again = import_statement(**kwargs, session=session)
    session.commit()

    assert again.imported == 0
    assert again.skipped == 2
    assert len(session.scalars(select(Transaction)).all()) == 2


def test_date_edit_outside_the_statement_window_does_not_restage(
    session: Session,
    seeded: tuple[User, Account],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The prefetch is scoped to the FILE'S FINGERPRINTS, not to a date window.

    ``origin_fingerprint`` freezes the row's original date while the row now stores
    the corrected one, so a date fixed outside the statement's period would fall
    outside a date-windowed prefetch, its provenance would go unread, and the line
    would re-stage as an undetectable duplicate.
    """
    user, account = seeded
    monkeypatch.setitem(
        import_service.PARSERS, ("axis", "credit_card"), _stub_parser_with(_single_row())
    )
    kwargs = {
        "user_id": user.id,
        "account_id": account.id,
        "file_bytes": b"file",
        "password": None,
    }
    import_statement(**kwargs, session=session)
    session.commit()

    txn = session.scalars(select(Transaction)).one()
    # Months away — far outside [min(dates), max(dates)] for this one-row statement.
    _simulate_identity_edit(session, txn, txn_date=date(2026, 9, 17))

    again = import_statement(**kwargs, session=session)
    session.commit()

    assert again.imported == 0
    assert again.skipped == 1
    assert len(session.scalars(select(Transaction)).all()) == 1


def test_row_moved_to_another_account_is_not_restaged(
    session: Session,
    seeded: tuple[User, Account],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same argument for the account half — the prefetch is no longer account-scoped.

    A file fingerprint already encodes ``account_id`` (PRD §F4), so matching on the
    fingerprint set cannot pull in another account's rows; dropping the redundant
    predicate is what lets a row the user *moved* still answer for its source line.
    """
    user, account = seeded
    other = Account(
        user_id=user.id, name="Axis CC 2", type="credit_card", issuer="axis", last4="5678"
    )
    session.add(other)
    session.commit()
    session.refresh(other)

    monkeypatch.setitem(
        import_service.PARSERS, ("axis", "credit_card"), _stub_parser_with(_single_row())
    )
    kwargs = {
        "user_id": user.id,
        "account_id": account.id,
        "file_bytes": b"file",
        "password": None,
    }
    import_statement(**kwargs, session=session)
    session.commit()

    txn = session.scalars(select(Transaction)).one()
    _simulate_identity_edit(session, txn, account_id=other.id)

    again = import_statement(**kwargs, session=session)
    session.commit()

    assert again.imported == 0
    assert again.skipped == 1
    assert len(session.scalars(select(Transaction)).all()) == 1


def test_reupload_after_an_edit_into_a_deleted_rows_fingerprint_does_not_collide(
    session: Session,
    seeded: tuple[User, Account],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MAX(occurrence) keys on the CURRENT fingerprint, not the coalesced one.

    ADR-0007 rule 9 bullet 5 claims a coalesced MAX "can only skip an ordinal, never
    reuse an occupied one". True *within* a duplicate group, false across groups: an
    edit can move a row's fingerprint INTO a group whose coalesced key sits
    elsewhere, so grouping MAX on the coalesced value hides that row and the
    allocator re-issues occurrence 0 — straight into the unique constraint, with no
    per-row SAVEPOINT to recover (``app.services.occurrence``). That is a 500 that
    fails the whole batch, so this asserts the import completes at all.
    """
    user, account = seeded
    rows = [
        RawTransaction(
            date=date(2026, 5, 4), amount_paise=-25000, merchant_raw="UBER", txn_type="purchase"
        ),
        RawTransaction(
            date=date(2026, 5, 5), amount_paise=-40000, merchant_raw="SWIGGY", txn_type="purchase"
        ),
    ]
    monkeypatch.setitem(import_service.PARSERS, ("axis", "credit_card"), _stub_parser_with(rows))
    kwargs = {
        "user_id": user.id,
        "account_id": account.id,
        "file_bytes": b"file",
        "password": None,
    }
    import_statement(**kwargs, session=session)
    session.commit()

    uber, swiggy = session.scalars(select(Transaction).order_by(Transaction.date)).all()
    uber_fp = uber.fingerprint
    session.delete(uber)
    session.commit()
    # SWIGGY is corrected into exactly what the deleted UBER line hashes to. Legal:
    # nothing holds (uber_fp, 0) any more, so rule 3's recompute would not 409.
    _simulate_identity_edit(
        session, swiggy, txn_date=date(2026, 5, 4), amount_paise=-25000, merchant_raw="UBER"
    )
    assert swiggy.fingerprint == uber_fp
    assert swiggy.occurrence == 0

    again = import_statement(**kwargs, session=session)
    session.commit()

    # The UBER line re-stages (its row was deleted — the documented contract) and
    # must land on a FREE ordinal beside the edited row, not on the occupied 0.
    assert again.imported == 1
    persisted = session.scalars(select(Transaction).where(Transaction.fingerprint == uber_fp)).all()
    assert sorted(t.occurrence for t in persisted) == [0, 1]


# ---------- Balance reconciliation (PRD §F1/§F4a, migration 0030) ----------


def _stub_parser_with_summary(rows: list[RawTransaction], summary: StatementSummary) -> type:
    """Like :func:`_stub_parser_with`, but with a controlled ``StatementSummary``
    instead of the all-None default — these tests need real opening/closing
    balances and a period to reconcile against."""

    class StubParser:
        @classmethod
        def parse(cls, pdf_bytes: bytes, password: str | None) -> ParsedStatement:
            return ParsedStatement(rows=rows, summary=summary)

    return StubParser


def test_reconciliation_metadata_stamped_and_clean_import_reconciles(
    session: Session,
    seeded: tuple[User, Account],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 3 test 1: a clean import — the newly-staged rows sum exactly to
    closing − opening — stamps all four metadata columns and reconciles to
    delta 0, using this batch's still-pending rows (decision 1: checked at
    upload, before the user has confirmed anything)."""
    user, account = seeded
    rows = [
        RawTransaction(
            date=date(2026, 3, 5), amount_paise=-8500, merchant_raw="A", txn_type="purchase"
        ),
        RawTransaction(
            date=date(2026, 3, 10), amount_paise=-1500, merchant_raw="B", txn_type="purchase"
        ),
    ]
    summary = StatementSummary(
        opening_balance_paise=-2000,
        closing_balance_paise=-12000,  # -2000 + (-8500 - 1500) == -12000
        period_start=date(2026, 3, 1),
        period_end=date(2026, 3, 31),
    )
    monkeypatch.setitem(
        import_service.PARSERS, ("axis", "credit_card"), _stub_parser_with_summary(rows, summary)
    )

    result = import_statement(
        user_id=user.id,
        account_id=account.id,
        file_bytes=b"file",
        password=None,
        session=session,
    )
    session.commit()

    batch = session.get(import_service.ImportBatch, result.batch_id)
    assert batch is not None
    assert batch.statement_opening_balance_paise == -2000
    assert batch.statement_closing_balance_paise == -12000
    assert batch.period_start == date(2026, 3, 1)
    assert batch.period_end == date(2026, 3, 31)
    assert batch.reconciliation_delta_paise == 0
    assert result.reconciliation_delta_paise == 0


def test_reconciliation_counts_board_rows_plus_this_batchs_pending_rows(
    session: Session,
    seeded: tuple[User, Account],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 3 test 2: a pre-existing CONFIRMED board row inside the window
    counts alongside this batch's newly-staged (still-pending) row — the
    delta only reconciles because both contribute to ``actual``."""
    user, account = seeded
    session.add(
        Transaction(
            user_id=user.id,
            account_id=account.id,
            date=date(2026, 3, 3),
            amount_paise=-1000,
            transaction_type="spend",
            merchant_raw="PRIOR",
            merchant_normalized="prior",
            fingerprint="fp-prior",
            source="manual",
            confirmed_at=datetime.now(UTC),
        )
    )
    session.commit()

    rows = [
        RawTransaction(
            date=date(2026, 3, 5), amount_paise=-9000, merchant_raw="NEW", txn_type="purchase"
        ),
    ]
    summary = StatementSummary(
        opening_balance_paise=-2000,
        closing_balance_paise=-12000,  # -2000 + (-1000 - 9000) == -12000
        period_start=date(2026, 3, 1),
        period_end=date(2026, 3, 31),
    )
    monkeypatch.setitem(
        import_service.PARSERS, ("axis", "credit_card"), _stub_parser_with_summary(rows, summary)
    )

    result = import_statement(
        user_id=user.id,
        account_id=account.id,
        file_bytes=b"file",
        password=None,
        session=session,
    )
    session.commit()

    assert result.reconciliation_delta_paise == 0


def test_short_statement_produces_a_positive_delta_not_a_negative_one(
    session: Session,
    seeded: tuple[User, Account],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 3 test 3, corrected sign. plans/balance-reconciliation.md
    originally stated this fixture's delta as -12345678; corrected
    2026-08-12 to +12345678, per the plan's OWN formula (actual − expected,
    stated twice in the brief): deleting a row of amount A from ``actual``
    while ``expected`` stays fixed always yields ``delta = -A``. Here
    A = -12345678 (a debit — the removed SENTINEL ELECTRONICS DEL row), so
    the delta must be positive.

    Numbers mirror the plan's own Axis fixture: opening -569900,
    closing -11519128 (expected -10949228); the one staged row is the
    full-statement row sum (-10949228) with that -12345678 row removed.
    """
    user, account = seeded
    rows = [
        RawTransaction(
            date=date(2026, 3, 15),
            amount_paise=1396450,  # -10949228 - (-12345678)
            merchant_raw="SENTINEL CREDIT",
            txn_type="other",
        ),
    ]
    summary = StatementSummary(
        opening_balance_paise=-569900,
        closing_balance_paise=-11519128,
        period_start=date(2026, 3, 1),
        period_end=date(2026, 3, 31),
    )
    monkeypatch.setitem(
        import_service.PARSERS, ("axis", "credit_card"), _stub_parser_with_summary(rows, summary)
    )

    result = import_statement(
        user_id=user.id,
        account_id=account.id,
        file_bytes=b"file",
        password=None,
        session=session,
    )
    session.commit()

    assert result.imported == 1
    assert result.reconciliation_delta_paise == 12345678


def test_no_summary_block_leaves_metadata_and_delta_null(
    session: Session,
    seeded: tuple[User, Account],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 3 test 4: a layout with no summary block at all (the Flipkart
    co-branded Axis layout's real closing text) legitimately reconciles to
    "not checked" — never a ParserError. ``_stub_parser_with``'s default
    ``StatementSummary()`` already models this."""
    user, account = seeded
    rows = [
        RawTransaction(
            date=date(2026, 3, 5), amount_paise=-500, merchant_raw="X", txn_type="purchase"
        ),
    ]
    monkeypatch.setitem(import_service.PARSERS, ("axis", "credit_card"), _stub_parser_with(rows))

    result = import_statement(
        user_id=user.id,
        account_id=account.id,
        file_bytes=b"file",
        password=None,
        session=session,
    )
    session.commit()

    batch = session.get(import_service.ImportBatch, result.batch_id)
    assert batch is not None
    assert batch.statement_opening_balance_paise is None
    assert batch.statement_closing_balance_paise is None
    assert batch.period_start is None
    assert batch.period_end is None
    assert batch.reconciliation_delta_paise is None
    assert result.reconciliation_delta_paise is None


def test_reupload_restamps_metadata_and_recomputes_the_delta(
    session: Session,
    seeded: tuple[User, Account],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 3 test 6: metadata stamping runs on EVERY import, not just the
    first — unlike imported_count/skipped_count/status, which record the
    first import's outcome. A re-upload of a batch stamped before this
    feature shipped (all-None metadata) repairs it: same file in, fresh
    metadata + a real delta out."""
    user, account = seeded
    row = RawTransaction(
        date=date(2026, 3, 5), amount_paise=-1000, merchant_raw="X", txn_type="purchase"
    )
    monkeypatch.setitem(import_service.PARSERS, ("axis", "credit_card"), _stub_parser_with([row]))
    first = import_statement(
        user_id=user.id,
        account_id=account.id,
        file_bytes=b"file",
        password=None,
        session=session,
    )
    session.commit()
    assert first.reconciliation_delta_paise is None

    # Re-upload: same file_bytes (same source_file_hash → reuses the batch),
    # but the parser now reads a real summary for the SAME row.
    summary = StatementSummary(
        opening_balance_paise=-500,
        closing_balance_paise=-1500,  # -500 + -1000 == -1500
        period_start=date(2026, 3, 1),
        period_end=date(2026, 3, 31),
    )
    monkeypatch.setitem(
        import_service.PARSERS,
        ("axis", "credit_card"),
        _stub_parser_with_summary([row], summary),
    )
    second = import_statement(
        user_id=user.id,
        account_id=account.id,
        file_bytes=b"file",
        password=None,
        session=session,
    )
    session.commit()

    assert second.batch_id == first.batch_id
    assert second.already_imported is True
    batch = session.get(import_service.ImportBatch, second.batch_id)
    assert batch is not None
    assert batch.statement_opening_balance_paise == -500
    assert batch.statement_closing_balance_paise == -1500
    assert second.reconciliation_delta_paise == 0
