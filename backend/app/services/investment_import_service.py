"""Investment import orchestration: parse → upsert instruments → validate → dedup → stamp FX.

The **parse** step is source-specific (the caller's): the canonical CSV parser today, an
INDmoney PDF parser later. The **persist** path is shared — :func:`persist_investment_rows`
takes already-parsed rows and runs the account-less ``ImportBatch`` + ``source_file_hash``
short-circuit, per-row ``fingerprint`` dedup, instrument upsert, and FX stamping. Two
consumers, one persist core — no ``source`` enum / branching.

Mirrors :mod:`app.services.import_service`: the caller (route) owns the commit; this
module uses the caller's session and never calls :meth:`Session.commit`.

Investments are **account-less** (the ``ImportBatch`` carries ``account_id=None``) and
have no review/tagging queue (no categories), so rows are persisted directly and a
summary (counts + per-row reject warnings) is returned.

Decisions locked for this slice:

* **``(symbol, currency)``-keyed instrument upsert.** The normalised broker ticker plus its
  currency is the identity. Reuse an active instrument with the same ``(symbol, currency)``;
  else create one with ``current_nav=None`` (a row carries transactions, not NAV — the holding
  shows invested but no current value until a NAV is set). A same ticker across an INR and a USD
  row resolves to **two distinct instruments** (a cross-listed name), not a collision.
  ``asset_class`` / ``exchange`` / ``name`` come from the parsed row. ``isin`` (when present) is
  stored fill-if-null: set on create, back-filled onto a pre-existing instrument whose ``isin``
  is still NULL; an existing non-null value is never overwritten (single-user).
* **FX is server-stamped at ingest.** Each row's ``fx_rate_to_inr`` is resolved via
  :func:`app.services.fx_service.resolve_fx_rate_to_inr` (INR → exact 1, the byte-identical
  no-op; USD → the historical ``rate_on(date)``). A USD row with **no** cached rate at-or-before
  its date is **rejected** (a counted warning), never stamped 1 — mis-stamping would silently
  corrupt the INR rollup.
* **Idempotent re-import, retryable on FX-reject.** Identical re-upload short-circuits on
  ``source_file_hash`` — but an import with any **rejected row** does not mark the batch
  ``completed`` (the state the short-circuit matches). Note the status keys on *rejections*
  only: parse and validation warnings do not block ``completed``, so a batch can be
  ``completed`` and still carry warnings. If any row was FX-rejected the batch is left
  ``failed`` (its "needs user attention" sense) so a re-upload after ``POST /fx/refresh``
  reprocesses; per-row
  ``fingerprint`` dedup makes the already-imported rows no-ops. The fingerprint hashes the
  **resolved instrument_id** (not the symbol string), so case/ticker drift can't double-count,
  and is FX-free (the rate is a derived stamp, not identity) so a re-import after a rate backfill
  still dedups cleanly.
* **Row-level dedup is a per-fingerprint multiset difference, not set membership** (ADR-0006,
  migration 0027). The hash carries identity only; multiplicity lives in
  ``InvestmentTransaction.occurrence``. So two identical same-day lumpsum buys of one fund — or
  two same-day SIP instalments across folios resolving to one instrument — both import, as
  occurrences 0 and 1, instead of the second being silently dropped as a duplicate. Re-upload
  stays idempotent: the DB holds ``n_db`` rows for a fingerprint, the file yields ``n_file``, and
  ``max(0, n_file - n_db)`` are staged. Row order is parser-emitted file order (never
  date-sorted), so identical bytes replay identically and the difference is empty.
* **Per-type validation at the import boundary.** ``switch_*`` / ``split`` are rejected by the
  parser; :func:`_reject_reason` checks the magnitude/price invariants (buy/sip/sell need
  units+price+amount; dividend units==0/no price; bonus units>0/no price/no amount), dropping a
  violating row to a warning rather than into the FIFO read-model. Its type dispatch is an
  explicit whitelist, so an unrecognised type is rejected by name instead of falling through to
  the priced-trade branch.
* **IDCW reinvestment is out of scope for CSV, by design.** A ``dividend`` row carrying units is
  rejected with a reason pointing at ``POST /investment-transactions/reinvestment``, because a
  reinvestment is a linked dividend+buy pair and this format cannot express the link: rows dedup
  independently (:func:`_fingerprint` has no notion of a sibling), so a re-upload after a partial
  failure could persist one leg without the other, undetectably.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.log_config import get_logger
from app.models import ImportBatch, Instrument, InvestmentTransaction
from app.models.instrument import AssetClassStr
from app.parsers import ParsedInvestmentRow, parse_investment_csv
from app.services.fx_service import resolve_fx_rate_to_inr
from app.services.holdings_service import UNIT_SIGN, available_units
from app.services.occurrence import OccurrenceAllocator

logger = get_logger(__name__)

_PARSER_NAME = "investment_csv"


@dataclass(frozen=True, slots=True)
class InvestmentImportResult:
    batch_id: int
    instruments_new: int
    txns_imported: int
    txns_skipped_dupe: int
    rows_rejected: int
    warnings: list[str]
    already_imported: bool


def import_investment_csv(
    *,
    user_id: UUID,
    file_bytes: bytes,
    default_asset_class: AssetClassStr,
    session: Session,
) -> InvestmentImportResult:
    """Parse a canonical investment CSV, then persist via the shared core.

    Thin wrapper: hash the bytes, **parse first** (a parse error raises before any batch row
    is created), then hand the rows to :func:`persist_investment_rows`. ``default_asset_class``
    (picked by the user in the upload form) applies to rows without an ``asset_class`` column.

    Raises:
        CSVParseError: propagated from :func:`parse_investment_csv` (bad file, missing
            required column, no importable rows).
    """
    source_file_hash = hashlib.sha256(file_bytes).hexdigest()
    rows, parse_warnings = parse_investment_csv(file_bytes, default_asset_class=default_asset_class)
    return persist_investment_rows(
        session,
        user_id=user_id,
        rows=rows,
        parse_warnings=parse_warnings,
        parser_name=_PARSER_NAME,
        source_file_hash=source_file_hash,
    )


def persist_investment_rows(
    session: Session,
    *,
    user_id: UUID,
    rows: list[ParsedInvestmentRow],
    parse_warnings: list[str],
    parser_name: str,
    source_file_hash: str,
) -> InvestmentImportResult:
    """Persist already-parsed rows: hash short-circuit → validate → upsert → dedup → stamp FX.

    Shared by every parser (CSV today, INDmoney next). ``parse_warnings`` are the caller's
    parser's per-row skips, merged into the returned warnings. ``rows_rejected`` is the total
    rows dropped (parse skips + per-type validation + FX-unavailable) — a user-facing total,
    deliberately NOT the ``rows_rejected`` in the ``import_completed`` event below, which
    excludes the parse skips so its counts satisfy ``rows_in == imported + skipped + rejected``.
    """
    existing_batch = session.scalar(
        select(ImportBatch).where(
            ImportBatch.user_id == user_id,
            ImportBatch.account_id.is_(None),
            ImportBatch.source_file_hash == source_file_hash,
            ImportBatch.status == "completed",
        )
    )
    if existing_batch is not None:
        # Import telemetry (PRD §Production-grade essentials). Emitted HERE too, not only
        # at the finalise below: unlike import_service — which flows through one exit and
        # re-runs the dedup loop on a re-upload — this path returns before a batch exists,
        # so the finalise log would miss every re-upload. rows_skipped == rows_in keeps the
        # rows_in == imported + skipped + rejected invariant true rather than exempting
        # this path from it: on a hash short-circuit every row is already present.
        logger.info(
            "import_completed",
            import_batch_id=existing_batch.id,
            parser=parser_name,
            rows_in=len(rows),
            rows_imported=0,
            rows_skipped=len(rows),
            rows_rejected=0,
            rows_parse_skipped=len(parse_warnings),
            status=existing_batch.status,
            already_imported=True,
        )
        return InvestmentImportResult(
            batch_id=existing_batch.id,
            instruments_new=0,
            txns_imported=0,
            txns_skipped_dupe=0,
            rows_rejected=0,
            warnings=[],
            already_imported=True,
        )

    batch = ImportBatch(
        user_id=user_id,
        account_id=None,
        source_file_hash=source_file_hash,
        parser_name=parser_name,
        status="pending",
    )
    session.add(batch)
    session.flush()

    # Validate per-type BEFORE upsert so a dropped row doesn't create a dangling
    # instrument (unlike CAS, the rows are the only instrument source here).
    valid_rows, validation_warnings = _validate_rows(rows)
    instruments_new, instruments_by_key = _upsert_instruments(
        session, user_id=user_id, rows=valid_rows
    )
    imported, skipped_dupe, reject_warnings = _persist_txns(
        session,
        user_id=user_id,
        batch_id=batch.id,
        rows=valid_rows,
        instruments_by_key=instruments_by_key,
    )

    batch.imported_count = imported
    batch.skipped_count = skipped_dupe
    # Keys on ``reject_warnings`` ALONE: ``parse_warnings`` / ``validation_warnings`` do NOT
    # block ``completed``, so a batch can be ``completed`` and still carry warnings. Only a
    # *rejected row* (a row that produced no transaction) is treated as needing attention.
    # A rejected row leaves the batch ``failed`` — its documented "needs user attention" sense —
    # and the next upload of the same file reprocesses (per-row fingerprint dedup makes the
    # already-imported rows no-ops). An FX-reject is transient (fixable by POST /fx/refresh); an
    # oversell reject needs a corrected CSV, but the reprocess-on-reupload handling is the same.
    # No dedicated "partial" status: it would cost a CHECK-altering table rebuild for a field that
    # is purely informational, and a retry leaves a lingering non-completed batch either way.
    batch.status = "failed" if reject_warnings else "completed"
    # Flush (not commit — the route owns commit) so the batch, instruments, and txn
    # rows are visible to a subsequent read or a second import in the same session.
    session.flush()

    # Import telemetry (PRD §Production-grade essentials), same field set as
    # import_service.import_statement. request_id / user_id are inherited from the HTTP
    # middleware's contextvars when called via the API (absent for any non-HTTP caller).
    #
    # rows_rejected counts only what THIS function dropped — a row that failed per-type
    # validation or FX resolution — so that rows_in == imported + skipped + rejected holds
    # and a reader can sanity-check the event. That is deliberately NOT
    # InvestmentImportResult.rows_rejected, which also folds in ``parse_warnings``: those
    # rows never reached ``rows``, so counting them here would break the arithmetic. They
    # get their own key instead.
    logger.info(
        "import_completed",
        import_batch_id=batch.id,
        parser=parser_name,
        rows_in=len(rows),
        rows_imported=imported,
        rows_skipped=skipped_dupe,
        rows_rejected=len(validation_warnings) + len(reject_warnings),
        rows_parse_skipped=len(parse_warnings),
        status=batch.status,
        already_imported=False,
    )

    warnings = [*parse_warnings, *validation_warnings, *reject_warnings]
    return InvestmentImportResult(
        batch_id=batch.id,
        instruments_new=instruments_new,
        txns_imported=imported,
        txns_skipped_dupe=skipped_dupe,
        rows_rejected=len(warnings),
        warnings=warnings,
        already_imported=False,
    )


def _validate_rows(
    rows: list[ParsedInvestmentRow],
) -> tuple[list[ParsedInvestmentRow], list[str]]:
    """Partition rows into importable ones + a PII-safe warning per dropped row."""
    valid: list[ParsedInvestmentRow] = []
    warnings: list[str] = []
    for r in rows:
        reason = _reject_reason(r)
        if reason is None:
            valid.append(r)
        else:
            warnings.append(reason)
    return valid, warnings


def _reject_reason(r: ParsedInvestmentRow) -> str | None:
    """Per-type magnitude/price invariants (mirrors ``InvestmentTransactionCreate``).

    Returns ``None`` when the row is importable, else a PII-safe reason (line number
    only, never merchant / amount text) so the caller can surface something the user
    can act on rather than a uniform "failed validation".

    ``switch_*`` / ``split`` never reach here — the parser rejects them first. The
    trailing branch is an explicit **whitelist**, not a fall-through: a 9th member
    added to ``InvestmentTxnTypeStr`` and forgotten in the parser's
    ``_CSV_DISALLOWED_TYPES`` would otherwise be silently validated as a priced
    trade and persisted, where ``holdings_service._apply`` ignores it and
    ``portfolio_service._signed_cashflow`` raises — the importer accepting what
    XIRR fails loud on is exactly backwards.
    """
    # Type-INDEPENDENT first, because the schema's pins are: ``InvestmentTransactionCreate``
    # declares units / amount_native_paise / fees_native_paise all ``ge=0``, and the parser
    # preserves the sign of each. Fees in particular reached no per-type branch below, so a
    # negative fee imported clean and ``_apply`` opened a FIFO lot at ``amount + (-fee)`` —
    # understating cost basis forever and inflating both per-holding and portfolio XIRR.
    if r.units < 0 or r.amount_native_paise < 0 or r.fees_native_paise < 0:
        return f"row {r.line_no}: units / amount / fees must not be negative — skipped"
    if r.txn_type == "dividend":
        if r.units > 0 or r.price is not None:
            # An IDCW *reinvestment*: income plus an acquisition at that date's NAV.
            # One row cannot carry both without conflating them, and conflating them
            # is what breaks FIFO holding periods — so it is a linked dividend+buy
            # pair, which the CSV shape has no way to express (rows dedup
            # independently, so a partial failure could persist one leg alone).
            return (
                f"row {r.line_no}: dividend carrying units (IDCW reinvest) is not importable "
                f"via CSV — record it via POST /investment-transactions/reinvestment"
            )
        if r.amount_native_paise <= 0:
            return f"row {r.line_no}: dividend requires a positive amount — skipped"
        return None
    if r.txn_type == "bonus":
        if r.units > 0 and r.price is None and r.amount_native_paise == 0:
            return None
        return f"row {r.line_no}: bonus requires units > 0, no price, zero amount — skipped"
    if r.txn_type in ("buy", "sip", "sell"):
        # Unit-bearing, priced cashflows.
        if r.units > 0 and r.price is not None and r.price > 0 and r.amount_native_paise > 0:
            return None
        return f"row {r.line_no}: failed per-type validation — skipped"
    return f"row {r.line_no}: transaction type not importable via CSV — skipped"


def _upsert_instruments(
    session: Session, *, user_id: UUID, rows: list[ParsedInvestmentRow]
) -> tuple[int, dict[tuple[str, str], Instrument]]:
    """Resolve each row's ``(symbol, currency)`` to an instrument, creating missing ones.

    Tracks this-run creations so a ``(symbol, currency)`` repeated across rows maps to one
    instrument (the first row's name / asset_class / exchange win). A same symbol across an
    INR and a USD row resolves to two distinct instruments. Returns
    ``(created_count, {(symbol, currency): instrument})``.
    """
    existing: dict[tuple[str, str], Instrument] = {
        (inst.symbol, inst.currency): inst
        for inst in session.scalars(
            select(Instrument).where(
                Instrument.user_id == user_id,
                Instrument.archived_at.is_(None),
            )
        )
    }
    resolved: dict[tuple[str, str], Instrument] = {}
    created = 0
    for row in rows:
        key = (row.symbol, row.currency)
        if key in resolved:
            continue
        inst = existing.get(key)
        if inst is None:
            inst = Instrument(
                user_id=user_id,
                symbol=row.symbol,
                isin=row.isin,
                name=row.name,
                asset_class=row.asset_class,
                currency=row.currency,
                exchange=row.exchange,
                current_nav=None,  # a CSV carries transactions, not NAV
                nav_updated_at=None,
            )
            session.add(inst)
            created += 1
        elif inst.isin is None and row.isin is not None:
            inst.isin = row.isin  # fill-if-null; never overwrite an existing value (single-user)
        resolved[key] = inst
    session.flush()  # assign ids for the FK on transaction rows
    return created, resolved


def _persist_txns(
    session: Session,
    *,
    user_id: UUID,
    batch_id: int,
    rows: list[ParsedInvestmentRow],
    instruments_by_key: dict[tuple[str, str], Instrument],
) -> tuple[int, int, list[str]]:
    """Dedup + stamp FX + oversell-guard + insert pre-validated rows.

    Returns ``(imported, skipped_dupe, rejected)`` where ``rejected`` collects both
    FX-unavailable and oversell row warnings. The dedup check runs **before** FX
    resolution so an already-imported row is a clean ``skipped_dupe`` even when rates
    are currently unavailable (it was stamped on its first, successful import). The
    oversell check runs **after** dedup + FX (only rows that would actually persist),
    against a running per-instrument net that is mutated only on the persist branch.
    """
    # Prefetch, per (instrument_id, fingerprint), HOW MANY rows already exist and the
    # highest occurrence in use — the row loop then does a multiset difference
    # (ADR-0006) rather than a set-membership test, so N identical rows in one CSV
    # produce N transactions instead of one. Keyed on the tuple to mirror the
    # UNIQUE (user_id, instrument_id, fingerprint, occurrence) scope, and scoped to
    # THIS batch's instruments only. The fingerprint embeds the resolved
    # instrument_id, so a re-upload (same instrument, same row) hashes identically →
    # skipped; different instruments can't collide.
    #
    # Unwindowed by date, unlike import_service (which windows by the statement
    # period): the instrument scope is already the narrow axis here, and a CSV
    # carries no bounded period.
    #
    # ``IS NOT NULL`` is load-bearing, not an optimisation — it keeps NULL-fingerprint
    # manual rows out of the map entirely. A NULL group would otherwise become a
    # bogus count for rows that never participate in dedup.
    #
    # MAX is tracked, not just COUNT: occurrences can be gapped (the user deletes
    # occurrence 0 and keeps 1), and reusing an occupied slot would trip
    # uq_investment_transactions_user_instrument_fingerprint.
    instrument_ids = {inst.id for inst in instruments_by_key.values()}
    existing_counts: dict[tuple[int, str], tuple[int, int]] = {
        (instrument_id, fingerprint): (count, max_occ)
        for instrument_id, fingerprint, count, max_occ in session.execute(
            select(
                InvestmentTransaction.instrument_id,
                InvestmentTransaction.fingerprint,
                func.count(),
                func.max(InvestmentTransaction.occurrence),
            )
            .where(
                InvestmentTransaction.user_id == user_id,
                InvestmentTransaction.instrument_id.in_(instrument_ids),
                InvestmentTransaction.fingerprint.is_not(None),
            )
            .group_by(InvestmentTransaction.instrument_id, InvestmentTransaction.fingerprint)
        )
    }
    allocator = OccurrenceAllocator(existing_counts)
    imported = 0
    skipped_dupe = 0
    rejected: list[str] = []
    # Running net units per instrument, seeded from committed DB state (the batch's
    # own rows aren't added yet). Mutated ONLY when a row actually persists, so a
    # deduped or FX-rejected row never moves it and a later same-instrument sell
    # validates against the true balance. Rows are walked in parser-emitted (file)
    # order — never date-sorted — so an in-file buy→sell passes and an in-file
    # oversell is caught; a sell listed before its funding buy in the same file may
    # false-reject (documented — re-order the CSV or split the import).
    running: dict[int, Decimal] = {
        iid: available_units(session, user_id=user_id, instrument_id=iid) for iid in instrument_ids
    }
    for row in rows:
        inst = instruments_by_key[(row.symbol, row.currency)]
        fp = _fingerprint(instrument_id=inst.id, row=row)
        # CALL POSITION IS LOAD-BEARING: allocate() must run HERE, ahead of both the
        # FX reject and the oversell reject below, because it is what increments the
        # allocator's per-file tally. Move it onto the persist branch and a rejected
        # row leaves the tally at 0, so a re-upload after the reject is fixed
        # re-imports a copy the DB already holds. The cost is that a rejected row can
        # leave an occurrence gap, which the MAX-based assignment tolerates by design.
        # Pinned by test_a_duplicate_row_is_skipped_before_the_oversell_guard_sees_it,
        # which was RED when allocate() was moved past both rejects.
        #
        # `is None` — occurrence 0 is a valid first sighting (see the allocator).
        occurrence = allocator.allocate((inst.id, fp))
        if occurrence is None:
            skipped_dupe += 1
            continue
        rate = resolve_fx_rate_to_inr(session, currency=inst.currency, on=row.date)
        if rate is None:
            # USD row with no cached rate at-or-before its date — reject, don't mis-stamp.
            rejected.append(
                f"row {row.line_no}: no USD/INR rate on or before "
                f"{row.date.isoformat()} — run POST /fx/refresh and retry"
            )
            continue
        if UNIT_SIGN.get(row.txn_type, 0) < 0 and row.units > running[inst.id]:
            # Oversell — would leave an impossible position. Skip (don't persist,
            # don't touch running), mirroring the FX-reject warning shape. The
            # read-model FIFO stays the authority for bad *stored* data.
            rejected.append(
                f"row {row.line_no}: sell of {row.units} exceeds available units — skipped"
            )
            continue
        session.add(
            InvestmentTransaction(
                user_id=user_id,
                instrument_id=inst.id,
                date=row.date,
                transaction_type=row.txn_type,
                units=row.units,
                price_per_unit_native=row.price,
                amount_native_paise=row.amount_native_paise,
                fees_native_paise=row.fees_native_paise,
                fx_rate_to_inr=rate,
                import_batch_id=batch_id,
                fingerprint=fp,
                occurrence=occurrence,
                pair_id=None,
            )
        )
        imported += 1
        # Sign comes from the one authority, holdings_service.UNIT_SIGN — the same map
        # available_units and _apply read, so this projection can no longer drift from
        # the read-model. Persist branch only.
        running[inst.id] += UNIT_SIGN.get(row.txn_type, 0) * row.units
    return imported, skipped_dupe, rejected


def _fingerprint(*, instrument_id: int, row: ParsedInvestmentRow) -> str:
    """``sha256("\\x1f".join(instrument_id, date, type, amount_paise, scaled_units))``.

    Keyed on the resolved ``instrument_id`` (stable post-upsert), NOT the free-text
    symbol: ticker case/rename can't change the hash, so re-import stays idempotent.
    Uses the scaled integer units (already 8-dp quantized) so trailing-zero reprints
    hash identically. Collision-free across instruments — the id is in the payload —
    and the existing-fingerprint prefetch is scoped to ``(user_id, instrument_id)``.
    FX-free: the rate is a derived stamp, not identity, so a re-import after a rate
    backfill still dedups.

    Identity only, never multiplicity: two genuinely-distinct identical rows share this
    hash and differ by ``InvestmentTransaction.occurrence`` (ADR-0006 rule 3).

    The ``\\x1f`` separator (ASCII Unit Separator) is ADR-0006's project-wide
    convention, applied here by migration 0027. Concatenating the fields left
    ``amount_native_paise | units_scaled`` ambiguous — two variable-length
    non-negative ints — so ``amount=12, units_scaled=345`` hashed identically to
    ``amount=123, units_scaled=45``. ``\\x1f`` is provably absent from all five
    fields: ``isoformat()`` is ``[0-9-]``, both ints render as ``[0-9]``,
    ``instrument_id`` likewise, and ``txn_type`` is a closed alphabetic vocabulary.

    Deliberately does **not** reuse ``app.services.fingerprint``'s ``_SEP``. These are
    two independent formulas over different field lists; sharing the constant would
    mean a future change to one silently rewrites the other's stored hashes, and each
    table's recompute migration is frozen against its own revision.
    """
    units_scaled = int((row.units * 10**8).to_integral_value(rounding=ROUND_HALF_EVEN))
    payload = "\x1f".join(
        (
            str(instrument_id),
            row.date.isoformat(),
            row.txn_type,
            str(row.amount_native_paise),
            str(units_scaled),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
