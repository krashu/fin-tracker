"""Investment transaction model — buys / sells / dividends etc. (PRD §F7).

One row per investment event against an ``instrument``. Carries ``user_id`` but no
``account_id`` — investments are decoupled from the spend tables (PRD §Data model).

Sign convention differs from the spend ``transactions`` table: ``units`` and the
money columns are **unsigned magnitudes**, and ``transaction_type`` carries the
direction (buy adds units, sell removes them). This mirrors the parser's
"unsigned paise, caller assigns sign" idiom. Per-type field rules (units==0 for
dividend, amount==0 for bonus, price required for buy/sip/sell, …) are enforced at
the HTTP boundary by the create schema, not the DB.

``units`` / ``price_per_unit_native`` use the scaled-int ``Units`` / ``PriceNative``
types (8 dp); ``amount_native_paise`` / ``fees_native_paise`` stay integer paise
like the rest of the app; ``fx_rate_to_inr`` is a scaled ``FxRate`` (6 dp), always
``1.0`` in v1 (INR-only) — the column exists so v0.5's FX layer can stamp real
rates without a migration.

``pair_id`` is the investment analogue of ``transactions.transfer_pair_id``: it links
the two legs of ONE economic event. Same DB-level invariants as the spend table — a
composite-unique target index, a same-user composite FK, and a no-self-pair CHECK.

Two pair kinds, identified by their **member types** rather than a discriminator
column (a nullable enum with one live value would be speculative — add one only when a
third kind arrives whose members are genuinely ambiguous):

* ``(dividend, buy)`` — an IDCW dividend **reinvestment**. The dividend leg is the
  income; the ``buy`` leg is the acquisition at that date's NAV, opening its own FIFO
  lot with its own cost basis and acquisition date. Written by
  ``POST /investment-transactions/reinvestment``. This is the only kind produced today.
* ``(switch_out, switch_in)`` — a CAS-era scheme switch. Reserved: both types are
  rejected by manual entry and by the CSV importer, so nothing writes this today.

**Any writer MUST set both directions in the same transaction**
(``a.pair_id = b.id`` *and* ``b.pair_id = a.id``). The delete path keys on
``txn.pair_id`` to null its partner, so a one-directional link would leave a dangling
composite FK when the *pointed-at* row is deleted first. The two conforming writers are
``create_reinvestment`` here and ``create_transfer`` on the spend side; there is
deliberately no defensive "null any row pointing at me" query, because the invariant
holds by construction (one writer, and PATCH is note-only).

Renamed from ``switch_pair_id`` in migration 0026. The FK / CHECK constraint **names**
still carry the old ``switch_pair`` spelling — renaming a constraint on SQLite needs a
full table rebuild, which 0026 exists to avoid.

``import_batch_id`` + ``fingerprint`` back the CSV importer (``investment_import_service``,
PRD §F7). ``import_batch_id`` is a reserved audit-trail FK to the originating
``import_batches`` row — NULL for manual rows, and there is **no** batch-cancel verb
in this slice (undo a bad import via per-row ``DELETE``); declared now like
``pair_id``. ``fingerprint`` backs idempotent re-import — UNIQUE on
``(user_id, instrument_id, fingerprint, occurrence)``. It is **nullable** (manual rows
= NULL),
unlike the spend ``transactions.fingerprint`` (NOT NULL, computed for manual rows):
NULLs are distinct under UNIQUE on SQLite *and* Postgres, so the backstop is inert
for manual rows by design — they aren't the dedup concern.

``occurrence`` (SMALLINT NOT NULL DEFAULT 0, migration 0027) is this table's copy of
ADR-0006 rule 3: the hash carries **identity only**, and multiplicity lives here. Two
identical same-day lumpsum buys of one fund are genuinely distinct events, so they
share a fingerprint and differ by occurrence, and import dedup is a per-fingerprint
**multiset difference** rather than a set-membership test. Only
``investment_import_service`` increments it; manual entry and the demo seeder leave it
at 0. Unlike the spend side there is no 409 double-submit guard here to preserve —
``POST /investment-transactions`` writes ``fingerprint = NULL``, so manual rows never
participate in dedup at all (see that route's docstring). That asymmetry with
``transactions.occurrence``, which DOES coexist with a 409, is deliberate.
"""

from __future__ import annotations

import uuid
from datetime import date as date_t
from decimal import Decimal
from typing import Literal, get_args

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    SmallInteger,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.models.types import FxRate, PriceNative, Units

InvestmentTxnTypeStr = Literal[
    "buy",
    "sell",
    "sip",
    "dividend",
    "bonus",
    "split",
    "switch_in",
    "switch_out",
]


class InvestmentTransaction(Base, TimestampMixin):
    __tablename__ = "investment_transactions"
    __table_args__ = (
        # Composite-unique reference target for the same-user self-referential
        # composite FK below. Declared as a unique Index (not UniqueConstraint)
        # to mirror transactions.uq_transactions_id_user — see that model + the
        # 0005 migration docstring for the Postgres composite-FK-target rationale.
        Index("uq_investment_transactions_id_user", "id", "user_id", unique=True),
        ForeignKeyConstraint(
            ["pair_id", "user_id"],
            ["investment_transactions.id", "investment_transactions.user_id"],
            name="fk_investment_transactions_switch_pair_same_user",
        ),
        CheckConstraint(
            "pair_id IS NULL OR pair_id != id",
            name="ck_investment_transactions_no_self_pair",
        ),
        # Backs the holdings FIFO read: per-instrument, oldest-first, with id as
        # the same-date tie-break (PRD §F7 FIFO; tie-break = txn id ascending).
        Index(
            "ix_investment_transactions_user_instrument_date",
            "user_id",
            "instrument_id",
            "date",
            "id",
        ),
        # CSV-import dedup backstop. NULLs distinct → inert for manual rows (NULL
        # fingerprint), and adding ``occurrence`` does not change that. Scoped by
        # instrument_id to mirror the spend side's
        # uq_transactions_user_account_fingerprint. The NAME is deliberately unchanged
        # across the 0027 widening — same reasoning as ADR-0006's "do not rename
        # uq_transactions_user_account_fingerprint".
        Index(
            "uq_investment_transactions_user_instrument_fingerprint",
            "user_id",
            "instrument_id",
            "fingerprint",
            "occurrence",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"))
    date: Mapped[date_t] = mapped_column(Date)
    transaction_type: Mapped[InvestmentTxnTypeStr] = mapped_column(
        Enum(
            *get_args(InvestmentTxnTypeStr),
            name="investment_transaction_type",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
        )
    )
    units: Mapped[Decimal] = mapped_column(Units())
    price_per_unit_native: Mapped[Decimal | None] = mapped_column(PriceNative(), nullable=True)
    amount_native_paise: Mapped[int] = mapped_column(BigInteger)
    fees_native_paise: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    # Scaled FxRate (6 dp): server_default is the *scaled* int (1.0 → 1_000_000)
    # because the raw SQL default lives below the TypeDecorator. Always 1.0 in v1.
    fx_rate_to_inr: Mapped[Decimal] = mapped_column(
        FxRate(), default=Decimal("1"), server_default=text("1000000")
    )
    note: Mapped[str | None] = mapped_column(String(1024))
    # FK declared composite-style in __table_args__ (same-user invariant). Do NOT
    # add an inline ForeignKey here — that would create a second single-column FK
    # and drift from the migration (mirrors transactions.transfer_pair_id).
    pair_id: Mapped[int | None] = mapped_column()
    # Reserved audit-trail FK to the originating CAS import_batches row (NULL for
    # manual rows; no batch-cancel verb this slice). Single-column FK, inline.
    import_batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("import_batches.id"), nullable=True
    )
    # CSV dedup key (NULL for manual rows). Unique per
    # (user_id, instrument_id, occurrence) — see __table_args__ + the class docstring
    # for the NULLs-distinct rationale.
    fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Which of the N identical transactions this is (ADR-0006 rule 3). The fingerprint
    # answers "what is this?"; occurrence answers "which of the identical ones?". Only
    # the CSV importer increments it; manual entry leaves it 0 (see the docstring).
    occurrence: Mapped[int] = mapped_column(SmallInteger, default=0, server_default=text("0"))
