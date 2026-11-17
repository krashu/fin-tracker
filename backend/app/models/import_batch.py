"""Import batch — one row per uploaded statement / CAS file.

Captures the parse → dedup → tag → persist pipeline outcome for one upload
so the user can see "10 imported, 3 skipped as duplicate" on the review
screen and so a re-upload of the same file (matched by
``source_file_hash``) can be detected upstream of row-level fingerprint
dedup.

``account_id`` is nullable: spend-statement batches (PRD §F1) are scoped to one
account, but investment-import batches (PRD §F7 / ``investment_import_service``) are
account-less — investments are decoupled from the spend tables. An investment batch is
identified by ``account_id IS NULL`` + ``parser_name == "investment_csv"``.

``status`` is informational — ``failed`` lets future error-reporting UIs
list batches that need user attention without scanning import logs. The
investment importer reuses ``failed`` for a batch left with FX-unavailable
rows (some rows couldn't be stamped — re-upload after ``POST /fx/refresh``
reprocesses); ``imported_count`` still records how many landed.

**Balance reconciliation (PRD §F1/§F4a, migration 0030).** Five columns
carry the statement's own summary block plus the computed verdict:

* ``statement_opening_balance_paise`` / ``statement_closing_balance_paise`` —
  read off the statement (``ParsedStatement.summary``), in the user's position
  (positive = money held, negative = owed; ADR-0010 rule 7). Both ``NULL`` when the parser
  found no summary block (e.g. the Flipkart co-branded layout) — never a parse failure.
* ``period_start`` / ``period_end`` — the statement's billing period as
  printed, ``NULL`` when the PDF printed none (ADR-0010 rule 8). When the
  parser could read both balances but not the period, ``reconcile_batch``
  falls back to ``min(row.date)`` / ``max(row.date)`` over the batch's own
  rows at reconcile time and never writes that window back here.
* ``reconciliation_delta_paise`` — ``NULL`` = not checked (no usable
  metadata, or the batch is account-less); ``0`` = reconciled; non-zero =
  mismatch, this many paise (``actual − expected``, signed — see
  ``reconciliation_service.reconcile_batch``). Deliberately not a status
  enum: one nullable BigInteger carries all three states, and status is
  derived at read time rather than stored.

  This is a **window delta**, not the ``/overview`` running balance —
  intentionally a different quantity (chosen so a first-ever import
  reconciles); see ``reconcile_batch``'s docstring for why they don't drift.

  **Known false-positive class, accepted:** a manual F2 row for the same
  card transaction, on the same account inside the window, is counted and
  produces a mismatch — the statement lists everything the issuer recorded,
  so that row is also on the statement and imports as a near-duplicate F4
  cannot catch (different merchant text). Informative, not noise;
  warn-never-block makes it cheap.

  **Known false-positive class, accepted:** a pending row **discarded**
  during review (e.g. an investment-transfer debit that belongs to F7, not
  F1 spend) permanently removes its amount from ``actual`` with no trace —
  a hard ``DELETE``, not a soft one. The delta will not self-correct; pair
  it with ``reconciliation_service.rows_removed_since_import`` (compares
  ``imported_count`` against a live count of rows still tied to this batch)
  to tell the user *some rows were removed since import* alongside the
  number, rather than a mismatch with no explanation.

No pre-existing batch is backfilled — it reads as "not checked", not
"reconciled", since it genuinely never ran through this check.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Literal, get_args

from sqlalchemy import BigInteger, Date, Enum, ForeignKey, ForeignKeyConstraint, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

ImportStatusStr = Literal["pending", "completed", "failed"]


class ImportBatch(Base, TimestampMixin):
    __tablename__ = "import_batches"
    __table_args__ = (
        # ADR-0003 rule 10: the (id, user_id) target of the same-user composite FKs from
        # transactions / investment_transactions.import_batch_id, and the composite FK
        # for this table's own account_id.
        Index("uq_import_batches_id_user", "id", "user_id", unique=True),
        ForeignKeyConstraint(
            ["account_id", "user_id"],
            ["accounts.id", "accounts.user_id"],
            name="fk_import_batches_account_id_accounts",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    # Nullable: spend batches carry an account; backup-restore and investment batches
    # are account-less (they resolve an account per row instead, or have none at all).
    # Composite same-user FK (``__table_args__``), so the DB refuses another user's
    # account; reads that join Account off it still restate `Account.user_id`
    # (ADR-0003 rule 7) — see `GET /imports/pending`.
    account_id: Mapped[int | None] = mapped_column(nullable=True)
    source_file_hash: Mapped[str] = mapped_column(String(64), index=True)
    parser_name: Mapped[str] = mapped_column(String(64))
    imported_count: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    # Duplicates only; zero-amount statement lines are not counted (ADR-0010 rule 5).
    skipped_count: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    status: Mapped[ImportStatusStr] = mapped_column(
        Enum(
            *get_args(ImportStatusStr),
            name="import_status",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
        ),
        default="pending",
        server_default="pending",
    )

    # Balance reconciliation (migration 0030) — see the module docstring for
    # what each column means and its NULL semantics.
    statement_opening_balance_paise: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    statement_closing_balance_paise: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    period_start: Mapped[date | None] = mapped_column(Date(), nullable=True)
    period_end: Mapped[date | None] = mapped_column(Date(), nullable=True)
    reconciliation_delta_paise: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
