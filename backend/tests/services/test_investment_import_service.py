"""Service tests for the investment importer (PRD §F7).

Coverage-gated module for the slice. Exercises ``import_investment_csv`` directly
against an in-memory session (no TestClient): instrument upsert by ``(symbol, currency)``,
the two dedup layers (source_file_hash short-circuit + per-row fingerprint), the
re-import-idempotency guarantees that motivated the instrument-id fingerprint
(different ticker casing → 0 new; two instruments sharing a magnitude → no false
collision), the FX stamping path (INR→1, USD→cached rate, no-rate→reject+retryable), and
every ``_reject_reason`` per-type branch.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from structlog.testing import capture_logs

from app.models import (
    FxRateQuote,
    ImportBatch,
    Instrument,
    InvestmentTransaction,
    User,
)
from app.parsers.investment_csv import ParsedInvestmentRow
from app.services.investment_import_service import (
    _fingerprint,
    _reject_reason,
    import_investment_csv,
)

_HEADER = "date,type,symbol,units,price,amount"
_HEADER_ISIN = "date,type,symbol,isin,units,price,amount"


def _row(
    *,
    units: Decimal = Decimal("10"),
    amount_native_paise: int = 100_000,
    txn_type: str = "buy",
    on: date = date(2024, 1, 1),
) -> ParsedInvestmentRow:
    """A minimal parsed row for the direct ``_fingerprint`` tests. Only the five hashed
    fields matter; the rest are filler the hash never reads."""
    return ParsedInvestmentRow(
        line_no=2,
        symbol="INFY",
        isin=None,
        name="INFY",
        asset_class="indian_equity",
        exchange="NSE",
        currency="INR",
        date=on,
        txn_type=txn_type,  # type: ignore[arg-type]
        units=units,
        price=Decimal("100"),
        amount_native_paise=amount_native_paise,
        fees_native_paise=0,
    )


def _csv(*rows: str, header: str = _HEADER) -> bytes:
    return ("\n".join([header, *rows]) + "\n").encode("utf-8")


def _import(session: Session, user_id: UUID, csv: bytes, asset_class: str = "indian_equity"):  # type: ignore[no-untyped-def]
    return import_investment_csv(
        user_id=user_id,
        file_bytes=csv,
        default_asset_class=asset_class,  # type: ignore[arg-type]
        session=session,
    )


def _count(session: Session, model: type) -> int:  # type: ignore[type-arg]
    return session.scalar(select(func.count()).select_from(model)) or 0


_USD_HEADER = "date,type,symbol,units,price,amount,currency"


def _seed_fx(session: Session, rate: str, on: date) -> None:
    session.add(
        FxRateQuote(
            date=on, from_currency="USD", to_currency="INR", rate=Decimal(rate), source="seed"
        )
    )
    session.flush()


def test_import_creates_instrument_and_txns(session: Session, user: User) -> None:
    result = _import(session, user.id, _csv("2024-01-01,buy,infy,10,100,1000"))

    assert result.instruments_new == 1
    assert result.txns_imported == 1
    assert result.txns_skipped_dupe == 0
    assert result.rows_rejected == 0
    assert result.already_imported is False

    inst = session.scalar(select(Instrument))
    assert inst is not None
    assert inst.symbol == "INFY"  # normalised strip+upper
    assert inst.asset_class == "indian_equity"
    assert inst.current_nav is None  # a CSV carries no NAV
    txn = session.scalar(select(InvestmentTransaction))
    assert txn is not None
    assert txn.fingerprint is not None and txn.import_batch_id == result.batch_id


def test_reimport_same_file_short_circuits(session: Session, user: User) -> None:
    csv = _csv("2024-01-01,buy,INFY,10,100,1000")
    first = _import(session, user.id, csv)
    second = _import(session, user.id, csv)

    assert second.already_imported is True
    assert second.batch_id == first.batch_id
    assert second.txns_imported == 0
    assert _count(session, InvestmentTransaction) == 1


def test_reimport_overlapping_file_dedups_by_fingerprint(session: Session, user: User) -> None:
    _import(session, user.id, _csv("2024-01-01,buy,INFY,10,100,1000"))
    # Different bytes (extra row) → no source-hash short-circuit; row 1 is a dupe.
    second = _import(
        session,
        user.id,
        _csv("2024-01-01,buy,INFY,10,100,1000", "2024-02-01,buy,INFY,5,110,550"),
    )
    assert second.already_imported is False
    assert second.txns_imported == 1
    assert second.txns_skipped_dupe == 1
    assert _count(session, InvestmentTransaction) == 2


def test_reimport_different_casing_is_idempotent(session: Session, user: User) -> None:
    """The instrument-id fingerprint + symbol normalisation make casing irrelevant."""
    _import(session, user.id, _csv("2024-01-01,buy,infy,10,100,1000"))
    second = _import(session, user.id, _csv("2024-01-01,buy,INFY,10,100,1000"))

    assert second.txns_imported == 0
    assert second.txns_skipped_dupe == 1
    assert second.instruments_new == 0
    assert _count(session, Instrument) == 1
    assert _count(session, InvestmentTransaction) == 1


def test_no_cross_instrument_fingerprint_collision(session: Session, user: User) -> None:
    """Two instruments sharing date+type+amount+units must both persist."""
    result = _import(
        session,
        user.id,
        _csv("2024-01-01,buy,INFY,10,100,1000", "2024-01-01,buy,TCS,10,100,1000"),
    )
    assert result.txns_imported == 2
    assert result.txns_skipped_dupe == 0
    assert _count(session, Instrument) == 2
    assert _count(session, InvestmentTransaction) == 2


def test_two_identical_rows_in_one_csv_both_import(session: Session, user: User) -> None:
    """D4 (ADR-0006 applied to this table): dedup is a multiset difference, not set
    membership. Two identical same-day lumpsum buys of one fund — or two same-day SIP
    instalments across folios that resolve to one instrument — are genuinely distinct
    events. The pre-fix loop mutated ``existing_fps`` in-flight, so row 2 was compared
    against row 1 of its own file and silently dropped as a duplicate."""
    result = _import(
        session,
        user.id,
        _csv("2024-01-01,buy,INFY,10,100,1000", "2024-01-01,buy,INFY,10,100,1000"),
    )
    assert result.txns_imported == 2
    assert result.txns_skipped_dupe == 0
    assert _count(session, InvestmentTransaction) == 2

    txns = list(session.scalars(select(InvestmentTransaction)))
    # Same identity hash, distinguished only by the occurrence ordinal.
    assert len({t.fingerprint for t in txns}) == 1
    assert {t.occurrence for t in txns} == {0, 1}


def test_reimport_of_a_duplicate_pair_stages_nothing(session: Session, user: User) -> None:
    """The multiset difference is idempotent: the DB already holds both copies, so a
    re-upload (byte-changed, to defeat the ``source_file_hash`` short-circuit) skips
    both. Row order is parser-emitted file order, so identical bytes replay identically."""
    dupe_rows = ("2024-01-01,buy,INFY,10,100,1000", "2024-01-01,buy,INFY,10,100,1000")
    _import(session, user.id, _csv(*dupe_rows))

    second = _import(session, user.id, _csv(*dupe_rows, "2024-03-01,buy,INFY,5,110,550"))
    assert second.already_imported is False  # different bytes → row-level path, not the hash
    assert second.txns_skipped_dupe == 2
    assert second.txns_imported == 1
    assert _count(session, InvestmentTransaction) == 3


def test_a_duplicate_row_is_skipped_before_the_oversell_guard_sees_it(
    session: Session, user: User
) -> None:
    """Pins the allocator's CALL POSITION: dedup runs ahead of the oversell reject.

    Bought because moving ``allocate()`` past the two rejects broke NOTHING in the suite
    — the ordering was load-bearing prose with no net under it, and hoisting the
    algorithm into :mod:`app.services.occurrence` turned it from a visible line into a
    property of the call site, which is what drifts.

    Setup: hold 15 units, then import two identical sells of 10. The first persists
    (10 ≤ 15), the second oversell-rejects (10 > 5). On re-upload the DB already holds
    one copy, so row 1 must be **skipped as a duplicate** rather than oversell-rejected
    a second time — ``txns_skipped_dupe`` is what distinguishes the two orderings.
    """
    _import(session, user.id, _csv("2024-01-01,buy,INFY,15,100,1500"))
    sells = ("2024-02-01,sell,INFY,10,120,1200", "2024-02-01,sell,INFY,10,120,1200")

    first = _import(session, user.id, _csv(*sells))
    assert first.txns_imported == 1  # the second sell would leave an impossible position
    assert first.rows_rejected == 1

    # Identical bytes do NOT short-circuit: a rejected row leaves the batch `failed`.
    second = _import(session, user.id, _csv(*sells))
    assert second.already_imported is False
    assert second.txns_skipped_dupe == 1  # ← 0 if the oversell guard ran first
    assert second.txns_imported == 0
    assert second.rows_rejected == 1
    assert _count(session, InvestmentTransaction) == 2  # the buy + one sell, unchanged


def test_occurrence_is_assigned_from_max_not_count(session: Session, user: User) -> None:
    """Occurrences can be gapped (ADR-0006 §Decision rule 3): a user deletes occurrence 0
    and keeps 1, then re-uploads. Assignment tracks ``MAX``, so the re-staged row takes
    2 — reusing the occupied slot 1 would trip the unique index."""
    dupe_rows = ("2024-01-01,buy,INFY,10,100,1000", "2024-01-01,buy,INFY,10,100,1000")
    _import(session, user.id, _csv(*dupe_rows))
    drop_occ_0 = session.scalar(
        select(InvestmentTransaction).where(InvestmentTransaction.occurrence == 0)
    )
    assert drop_occ_0 is not None
    session.delete(drop_occ_0)
    session.flush()

    second = _import(session, user.id, _csv(*dupe_rows, "2024-03-01,buy,INFY,5,110,550"))
    assert second.txns_skipped_dupe == 1  # one of the two copies is already stored
    assert second.txns_imported == 2  # the surplus copy + the new row
    occurrences = set(
        session.scalars(
            select(InvestmentTransaction.occurrence).where(
                InvestmentTransaction.date == date(2024, 1, 1)
            )
        )
    )
    assert occurrences == {1, 2}


def test_existing_instrument_is_reused(session: Session, user: User) -> None:
    session.add(
        Instrument(
            user_id=user.id,
            symbol="INFY",
            name="Infosys",
            asset_class="indian_equity",
            currency="INR",
            exchange="NSE",
            current_nav=Decimal("1500"),
        )
    )
    session.flush()

    result = _import(session, user.id, _csv("2024-01-01,buy,INFY,10,100,1000"))
    assert result.instruments_new == 0
    assert _count(session, Instrument) == 1
    inst = session.scalar(select(Instrument))
    assert inst is not None and inst.current_nav == Decimal("1500")  # NAV not clobbered


def test_import_captures_isin_on_new_instrument(session: Session, user: User) -> None:
    _import(
        session,
        user.id,
        _csv("2024-01-01,buy,INFY,INE009A01021,10,100,1000", header=_HEADER_ISIN),
    )
    inst = session.scalar(select(Instrument))
    assert inst is not None and inst.isin == "INE009A01021"


def test_import_fills_isin_when_existing_is_null(session: Session, user: User) -> None:
    # Pre-existing instrument with no ISIN (created before the column, or via manual entry).
    session.add(
        Instrument(
            user_id=user.id,
            symbol="INFY",
            name="Infosys",
            asset_class="indian_equity",
            currency="INR",
            exchange="NSE",
            isin=None,
        )
    )
    session.flush()

    _import(
        session,
        user.id,
        _csv("2024-01-01,buy,INFY,INE009A01021,10,100,1000", header=_HEADER_ISIN),
    )
    inst = session.scalar(select(Instrument))
    assert inst is not None and inst.isin == "INE009A01021"  # back-filled fill-if-null


def test_import_never_overwrites_existing_isin(session: Session, user: User) -> None:
    # An existing non-null ISIN is authoritative — the import must not clobber it.
    session.add(
        Instrument(
            user_id=user.id,
            symbol="INFY",
            name="Infosys",
            asset_class="indian_equity",
            currency="INR",
            exchange="NSE",
            isin="INE467B01029",
        )
    )
    session.flush()

    _import(
        session,
        user.id,
        _csv("2024-01-01,buy,INFY,INE009A01021,10,100,1000", header=_HEADER_ISIN),
    )
    inst = session.scalar(select(Instrument))
    assert inst is not None and inst.isin == "INE467B01029"  # not clobbered


def test_default_asset_class_applied(session: Session, user: User) -> None:
    _import(session, user.id, _csv("2024-01-01,buy,GOLDBEES,10,100,1000"), asset_class="gold")
    inst = session.scalar(select(Instrument))
    assert inst is not None and inst.asset_class == "gold"


def test_buy_without_price_is_rejected(session: Session, user: User) -> None:
    csv = _csv("2024-01-01,buy,INFY,10,,", header="date,type,symbol,units,price,amount")
    result = _import(session, user.id, csv)
    assert result.txns_imported == 0
    assert result.rows_rejected == 1
    assert _count(session, InvestmentTransaction) == 0
    assert result.warnings and result.warnings[0].startswith("row ")


def test_negative_fees_is_rejected(session: Session, user: User) -> None:
    """B#15: ``InvestmentTransactionCreate`` pins ``fees_native_paise`` at ``ge=0`` and
    ``_reject_reason`` claims to mirror it, but no per-type branch read fees — so a
    negative fee imported clean and ``_apply`` opened the FIFO lot at
    ``amount + (-fee)``, understating cost basis and inflating XIRR forever.

    The same body posted to ``POST /investment-transactions`` is a 422.
    """
    header = "date,type,symbol,units,price,amount,fees"
    result = _import(session, user.id, _csv("2024-01-01,buy,INFY,10,100,1000,-50", header=header))

    assert result.txns_imported == 0
    assert result.rows_rejected == 1
    assert _count(session, InvestmentTransaction) == 0
    assert len(result.warnings) == 1
    assert result.warnings[0].startswith("row 2:")
    assert "negative" in result.warnings[0]

    # Positive control: identical row, fees the right way up, still imports.
    ok = _import(session, user.id, _csv("2024-01-01,buy,INFY,10,100,1000,50", header=header))
    assert ok.txns_imported == 1


def test_negative_units_is_rejected(session: Session, user: User) -> None:
    """The same guard's second clause, which the fees-only version of this fix would
    have missed: ``units: ge=0`` is pinned by the schema too, and the dividend branch
    tests ``units > 0``, so ``units=-5`` was False on both clauses, fell through to the
    amount check, and imported clean.
    """
    result = _import(session, user.id, _csv("2024-01-01,dividend,INFY,-5,,1000"))

    assert result.txns_imported == 0
    assert result.rows_rejected == 1
    assert _count(session, InvestmentTransaction) == 0
    assert "negative" in result.warnings[0]


def test_dividend_valid_and_invalid(session: Session, user: User) -> None:
    # Valid dividend: units==0, no price, amount>0. Invalid: carries units.
    result = _import(
        session,
        user.id,
        _csv("2024-01-01,dividend,INFY,,,500", "2024-02-01,dividend,INFY,3,,500"),
    )
    assert result.txns_imported == 1
    assert result.rows_rejected == 1


def test_dividend_with_units_is_rejected_with_an_actionable_reason(
    session: Session, user: User
) -> None:
    """A CSV IDCW-reinvest row names the endpoint that CAN record it.

    The batch stays ``completed``, not ``failed``: ``batch.status`` keys off
    ``reject_warnings`` (FX / oversell), while per-type drops land in
    ``validation_warnings``. Flipping that is a behaviour change, not this fix.
    """
    result = _import(session, user.id, _csv("2025-12-19,dividend,INFY,0.8,125,100"))

    assert result.txns_imported == 0
    assert result.rows_rejected == 1
    assert len(result.warnings) == 1
    assert "IDCW reinvest" in result.warnings[0]
    assert "/investment-transactions/reinvestment" in result.warnings[0]
    assert "row 2" in result.warnings[0]

    batch = session.scalar(select(ImportBatch))
    assert batch is not None and batch.status == "completed"


def test_out_of_vocabulary_type_is_rejected_not_treated_as_a_trade() -> None:
    """The whitelist tail, pinned directly.

    ``_reject_reason`` is called with a type the parser would have blocked, because
    that block is exactly why this branch is unreachable end-to-end — and why a
    future 9th enum member added without updating ``_CSV_DISALLOWED_TYPES`` would
    silently be validated as a priced trade if the tail were a fall-through.
    """
    row = ParsedInvestmentRow(
        line_no=2,
        symbol="INFY",
        isin=None,
        name="INFY",
        asset_class="indian_equity",
        exchange="NSE",
        currency="INR",
        date=date(2025, 12, 19),
        txn_type="switch_in",
        units=Decimal("5"),
        price=Decimal("100"),
        amount_native_paise=50_000,
        fees_native_paise=0,
    )
    reason = _reject_reason(row)
    assert reason is not None
    assert "not importable via CSV" in reason


def test_bonus_valid_and_invalid(session: Session, user: User) -> None:
    # Valid bonus: units>0, no price, amount==0. Invalid: carries an amount.
    result = _import(
        session,
        user.id,
        _csv("2024-01-01,bonus,INFY,5,,", "2024-02-01,bonus,INFY,5,,999"),
    )
    assert result.txns_imported == 1
    assert result.rows_rejected == 1


# --------------------------------------------------------------------------- #
# FX stamping (S3a) — INR no-op, USD cached-rate, reject + retry, identity
# --------------------------------------------------------------------------- #
def test_inr_row_stamps_rate_one_without_touching_fx_cache(session: Session, user: User) -> None:
    # INR import is byte-identical to pre-FX: stamps exactly 1 and never reads fx_rates.
    _import(session, user.id, _csv("2024-01-01,buy,INFY,10,100,1000"))
    txn = session.scalar(select(InvestmentTransaction))
    assert txn is not None and txn.fx_rate_to_inr == Decimal("1")
    assert _count(session, FxRateQuote) == 0


def test_usd_row_stamped_from_cached_rate(session: Session, user: User) -> None:
    _seed_fx(session, "83.5", date(2024, 1, 1))
    result = _import(
        session,
        user.id,
        _csv("2024-01-01,buy,AAPL,10,100,1000,USD", header=_USD_HEADER),
        asset_class="us_equity",
    )
    assert result.txns_imported == 1
    inst = session.scalar(select(Instrument))
    assert inst is not None and inst.currency == "USD"
    txn = session.scalar(select(InvestmentTransaction))
    assert txn is not None and txn.fx_rate_to_inr == Decimal("83.5")


def test_usd_row_without_rate_rejected_and_batch_not_completed(
    session: Session, user: User
) -> None:
    result = _import(
        session,
        user.id,
        _csv("2024-01-01,buy,AAPL,10,100,1000,USD", header=_USD_HEADER),
        asset_class="us_equity",
    )
    assert result.txns_imported == 0
    assert result.rows_rejected == 1
    assert result.warnings[0].startswith("row ") and "/fx/refresh" in result.warnings[0]
    # The instrument is created (orphan until retry), but no transaction lands.
    assert _count(session, Instrument) == 1
    assert _count(session, InvestmentTransaction) == 0
    # Batch left non-completed so a re-upload after seeding rates reprocesses.
    batch = session.get(ImportBatch, result.batch_id)
    assert batch is not None and batch.status != "completed"


def test_fx_rejected_row_is_retryable_after_rate_seeded(session: Session, user: User) -> None:
    # The retry hole (Copilot #1): an FX-rejected file must NOT short-circuit on re-upload.
    usd_csv = _csv("2024-01-01,buy,AAPL,10,100,1000,USD", header=_USD_HEADER)
    first = _import(session, user.id, usd_csv, asset_class="us_equity")
    assert first.txns_imported == 0 and first.rows_rejected == 1
    assert _count(session, InvestmentTransaction) == 0

    _seed_fx(session, "83", date(2024, 1, 1))
    second = _import(session, user.id, usd_csv, asset_class="us_equity")
    assert second.already_imported is False  # not short-circuited despite identical bytes
    assert second.txns_imported == 1
    assert _count(session, InvestmentTransaction) == 1


def test_same_symbol_across_currencies_creates_two_instruments(
    session: Session, user: User
) -> None:
    _seed_fx(session, "83", date(2024, 1, 1))
    result = _import(
        session,
        user.id,
        _csv(
            "2024-01-01,buy,RELI,10,100,1000,INR,indian_equity",
            "2024-01-01,buy,RELI,10,100,1000,USD,us_equity",
            header="date,type,symbol,units,price,amount,currency,asset_class",
        ),
    )
    assert result.instruments_new == 2
    assert result.txns_imported == 2
    insts = list(session.scalars(select(Instrument)))
    assert all(i.symbol == "RELI" for i in insts)
    assert {i.currency for i in insts} == {"INR", "USD"}


def test_fingerprint_stable_across_rate_change(session: Session, user: User) -> None:
    # The fingerprint is FX-free: a row re-imports as a dupe even after a different rate is
    # cached (the stamp is derived, not identity).
    _seed_fx(session, "83", date(2024, 1, 1))
    usd_row = "2024-01-01,buy,AAPL,10,100,1000,USD"
    _import(session, user.id, _csv(usd_row, header=_USD_HEADER), asset_class="us_equity")

    _seed_fx(session, "90", date(2024, 2, 1))  # a later, different rate
    second = _import(
        session,
        user.id,
        _csv(usd_row, "2024-02-01,buy,AAPL,5,110,550,USD", header=_USD_HEADER),
        asset_class="us_equity",
    )
    assert second.txns_skipped_dupe == 1  # the 2024-01-01 row dedups
    assert second.txns_imported == 1
    assert _count(session, InvestmentTransaction) == 2


def test_fingerprint_disambiguates_amount_from_units() -> None:
    """D5: ``amount_native_paise | units_scaled`` is the one genuinely ambiguous boundary
    in this payload — two variable-length non-negative ints. Pre-fix, ``amount=12`` with
    ``units_scaled=345`` and ``amount=123`` with ``units_scaled=45`` both rendered
    ``"12345"`` and hashed identically. The ``\\x1f`` separator (ADR-0006) makes the
    payload injective; ``\\x1f`` is provably absent from every field here (both ints are
    ``[0-9]``, ``isoformat()`` is ``[0-9-]``, and ``txn_type`` is a closed alphabetic
    vocabulary)."""
    a = _row(units=Decimal("0.00000345"), amount_native_paise=12)
    b = _row(units=Decimal("0.00000045"), amount_native_paise=123)

    assert _fingerprint(instrument_id=1, row=a) != _fingerprint(instrument_id=1, row=b)


def test_fingerprint_units_are_hashed_at_8dp_scale() -> None:
    """Trailing-zero reprints hash identically — the payload carries the 8-dp scaled int,
    not the as-written decimal string. This is also the value migration 0027's recompute
    must read (the raw stored column), so pin it."""
    assert _fingerprint(instrument_id=1, row=_row(units=Decimal("1.5"))) == _fingerprint(
        instrument_id=1, row=_row(units=Decimal("1.50000000"))
    )


# --- import telemetry (PRD §Production-grade essentials) ----------------------
def test_import_emits_telemetry_with_a_checkable_invariant(session: Session, user: User) -> None:
    """B9.4: the PRD promises EVERY import logs import_batch_id / parser / rows_in /
    rows_imported / rows_skipped. This module logged nothing — it did not even import
    get_logger — so a dropped-row report left only the middleware's request_completed line.

    ``rows_rejected`` is the count THIS function dropped (per-type validation + FX
    resolution), which is what makes rows_in == imported + skipped + rejected a checkable
    invariant. That is deliberately NOT ``InvestmentImportResult.rows_rejected``, which
    also folds in ``parse_warnings`` — rows that never reached ``rows`` at all. Folding
    them in would break the arithmetic silently. Here the CSV carries one importable buy
    and one buy with no price (rejected by ``_reject_reason``).
    """
    csv = _csv("2024-01-01,buy,INFY,10,100,1000", "2024-01-02,buy,TCS,5,,")
    with capture_logs() as logs:
        result = _import(session, user.id, csv)

    events = [e for e in logs if e.get("event") == "import_completed"]
    assert len(events) == 1
    ev = events[0]
    assert ev["parser"] == "investment_csv"
    assert ev["import_batch_id"] == result.batch_id
    assert ev["rows_in"] == 2
    assert ev["rows_imported"] == result.txns_imported == 1
    assert ev["rows_skipped"] == result.txns_skipped_dupe == 0
    assert ev["rows_rejected"] == 1
    assert ev["already_imported"] is False
    assert ev["rows_in"] == ev["rows_imported"] + ev["rows_skipped"] + ev["rows_rejected"]
    # rows_rejected > 0 does NOT imply status == "failed", and the log must not imply it
    # either: batch.status keys on the PERSIST-layer rejects alone (FX-unavailable /
    # oversell — the retryable ones), so a row dropped by per-type validation is counted
    # here while the batch still completes. Both fields are emitted precisely because
    # neither can be inferred from the other.
    assert ev["status"] == "completed"

    # PII: counts and ids only — no ticker, merchant, PAN or account number. This asserts
    # CALL-SITE HYGIENE, not masking: capture_logs swaps the whole processor chain, so
    # mask_pii never runs here (test_http_logging.py unit-tests it directly for that
    # reason). The per-row reject reasons are deliberately NOT folded into the event —
    # they carry line numbers only today, but that is a convention, not a type.
    rendered = repr(ev).lower()
    for leaked in ("infy", "tcs", "row "):
        assert leaked not in rendered, f"{leaked!r} leaked into the import telemetry event"


def test_short_circuited_reimport_still_emits_telemetry(session: Session, user: User) -> None:
    """The source_file_hash short-circuit returns BEFORE a batch is created, so a single
    log at the finalise point would miss every re-upload — the exact case an operator is
    investigating when they ask why a re-import did nothing.

    Unlike ``import_service``, which flows through one exit and re-runs the dedup loop
    (so its re-upload rows land in ``skipped`` naturally), this path skips the loop
    entirely. Reporting rows_skipped == rows_in keeps the invariant true rather than
    exempting the path from it: on a hash match every row is already present.
    """
    csv = _csv("2024-01-01,buy,INFY,10,100,1000", "2024-01-02,buy,TCS,5,50,250")
    first = _import(session, user.id, csv)
    with capture_logs() as logs:
        second = _import(session, user.id, csv)

    assert second.already_imported is True
    ev = next(e for e in logs if e.get("event") == "import_completed")
    assert ev["already_imported"] is True
    assert ev["import_batch_id"] == first.batch_id
    assert ev["rows_in"] == 2
    assert ev["rows_imported"] == 0
    assert ev["rows_skipped"] == 2
    assert ev["rows_in"] == ev["rows_imported"] + ev["rows_skipped"] + ev["rows_rejected"]
