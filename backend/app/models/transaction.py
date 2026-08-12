"""Transaction model — the main spend/income table (per PRD §F1, §F4).

``amount_paise`` is signed int64: negative for spends, positive for income
/ payments / refunds (PRD §F2 sign rule, §F4a refund treatment). The
parser ``RawTransaction.txn_type`` (CC-specific) maps into the PRD-level
``transaction_type`` enum here when the future ``import_service``
composes a row.

**A refund is not a transaction type.** It is a ``spend`` row carrying a
*positive* ``amount_paise`` — derived at read time, never stored
(``docs/adr/0009-refund-as-signed-spend.md``). So ``spend`` is the only type
that accepts either sign, and every F8 aggregate discriminates on the sign
rather than on the type. Identity is unaffected: the ADR-0006 fingerprint
never hashed ``transaction_type``, so collapsing the enum moves no hash,
no ``origin_fingerprint`` and no ``occurrence``.

``fingerprint`` is the dedup key from PRD §F4, as amended by
``docs/adr/0006-f4-dedup-key.md``:
``sha256("\\x1f".join(date_iso, amount_paise, normalized_merchant, account_id))``.

``occurrence`` is its companion: the ordinal among rows sharing
``(user_id, account_id, fingerprint)``, so two genuinely-distinct transactions
that agree on all four hashed fields (two auto rides at the same fare on one day)
can both be stored. Identity lives in the hash; multiplicity lives here. Dedup is
therefore a per-fingerprint **multiset difference**, not set membership — see
``services/import_service.py``.

``transfer_pair_id`` links two transaction rows that represent the same
movement of money. Populated by F4a auto-reconciliation and F2 manual
transfers per ``docs/adr/0002-transfer-pair-id-semantics.md`` (Accepted).
Nullable for non-transfer rows. Three DB-level invariants enforce the
ADR contract:

* ``uq_transactions_id_user`` — composite unique INDEX on
  ``(id, user_id)`` so the same-user composite FK below has a valid
  reference target. PK uniqueness on ``id`` is enough on SQLite; the
  composite unique is Postgres-portability insurance (composite FK
  targets need a composite unique index on Postgres, not just a PK).
  Declared as ``Index(..., unique=True)`` rather than
  ``UniqueConstraint(...)`` so the migration can create it standalone
  before the batch FK swap — see migration 0005 docstring.
* ``fk_transactions_transfer_pair_same_user`` — composite FK
  ``(transfer_pair_id, user_id) → (id, user_id)``. Guarantees any
  non-null ``transfer_pair_id`` points at a row with matching
  ``user_id``. Replaces the original single-column FK from migration
  0001; landed in migration 0005.
* ``ck_transactions_no_self_pair`` — CHECK ``transfer_pair_id IS NULL
  OR transfer_pair_id != id``. Prevents a row from pairing with
  itself.

Symmetry (``A.transfer_pair_id = B ⇒ B.transfer_pair_id = A``) and
exactly-two pairing are NOT DB-enforceable portably (would need
triggers); they're service-layer concerns when F4a auto-link and F2
paired-transfer writers land.
"""

from __future__ import annotations

import uuid
from datetime import date as date_t
from datetime import datetime
from typing import TYPE_CHECKING, Literal, get_args

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.label import Label

TransactionTypeStr = Literal["spend", "income", "transfer"]
TransactionSourceStr = Literal["import", "manual"]


class Transaction(Base, TimestampMixin):
    __tablename__ = "transactions"
    __table_args__ = (
        # ADR-0006. ``occurrence`` widens this from 3 columns to 4 so identical
        # rows can coexist. The NAME is deliberately unchanged: the Postgres
        # branch of ``core.db_errors.is_unique_violation`` matches on the index
        # name, so keeping it means ``api/v1/transactions._is_fingerprint_conflict``
        # (and its 409 mapping) needs no edit. Its SQLite branch is a subset test
        # over ``table.col`` tokens, so the extra column is inert there too.
        UniqueConstraint(
            "user_id",
            "account_id",
            "fingerprint",
            "occurrence",
            name="uq_transactions_user_account_fingerprint",
        ),
        # ADR-0002: composite-unique reference target for the same-user
        # composite FK below. Declared as a unique Index (not a
        # UniqueConstraint) so the migration can create it as a standalone
        # CREATE UNIQUE INDEX before the batch FK swap — the standalone
        # form provides a valid composite-unique target on the OLD
        # transactions table at copy time, which the batch's self-
        # referential composite FK validation requires. Functionally
        # identical to a UniqueConstraint as an FK target on both SQLite
        # and Postgres.
        Index("uq_transactions_id_user", "id", "user_id", unique=True),
        ForeignKeyConstraint(
            ["transfer_pair_id", "user_id"],
            ["transactions.id", "transactions.user_id"],
            name="fk_transactions_transfer_pair_same_user",
        ),
        CheckConstraint(
            "transfer_pair_id IS NULL OR transfer_pair_id != id",
            name="ck_transactions_no_self_pair",
        ),
        Index("ix_transactions_user_account_date", "user_id", "account_id", "date"),
        Index("ix_transactions_user_merchant_normalized", "user_id", "merchant_normalized"),
        Index("ix_transactions_user_category_date", "user_id", "category_id", "date"),
        # Partial index for the board's newest-confirmed-rows lookup. WHERE
        # predicate mirrors alembic/versions/0004_add_transaction_confirmed_at.py;
        # keep in sync — test_migration_parity does NOT verify partial-index
        # WHERE clauses.
        Index(
            "ix_transactions_user_confirmed_date",
            "user_id",
            "date",
            "id",
            sqlite_where=text("confirmed_at IS NOT NULL"),
            postgresql_where=text("confirmed_at IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    date: Mapped[date_t] = mapped_column(Date)
    amount_paise: Mapped[int] = mapped_column(BigInteger)
    transaction_type: Mapped[TransactionTypeStr] = mapped_column(
        Enum(
            *get_args(TransactionTypeStr),
            name="transaction_type",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
        )
    )
    merchant_raw: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # 512 to match merchant_raw's cap: normalize_merchant is length-unbounded, so
    # a long parsed merchant must not overflow on Postgres (SQLite ignores the
    # cap; the fingerprint hashes the full normalized string). See migration 0020.
    merchant_normalized: Mapped[str] = mapped_column(String(512))
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"))
    # Frozen record of the category the import auto-tag (F3) prefilled at ingest,
    # for the acceptance-rate metric (GET /dashboards/tagging-stats, PRD
    # §Success-metrics: ≥80% pre-tagged correctly). Set ONLY by import_service
    # when a merchant_tag_map match prefills category_id; NULL for manual rows
    # and for import rows with no suggestion. NEVER synced on PATCH — a later
    # category_id edit is exactly what the metric counts as "not kept". Kept off
    # TransactionRead (internal metric field; the wire schema stays narrow).
    auto_category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"))
    # PRD §F4 dedup key — *what does this row say*. Recomputed whenever a PATCH
    # edits one of the four ADR-0006 identity inputs, and unique-constrained
    # together with `occurrence` (see __table_args__).
    fingerprint: Mapped[str] = mapped_column(String(64))
    # ADR-0007 rule 9 — *which statement line produced this row*. Same formula,
    # stamped once by import_service at STAGE time and never recomputed, so an
    # edit to an identity column does not read as a deletion to the importer and
    # re-stage the pre-edit row. NULL = no external source line: manual entry,
    # both transfer legs, F4a, the demo seeder, backup CSV import — those key on
    # their own current `fingerprint` instead, via the file-dedup prefetches'
    # COALESCE(origin_fingerprint, fingerprint). Deliberately neither unique nor
    # indexed; do not "simplify" the COALESCE away and never recompute this.
    origin_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # ADR-0006. Ordinal among rows sharing (user_id, account_id, fingerprint).
    # 0 for every manually-created row — manual entry, both transfer legs, and the
    # demo seeder never set it, so a second identical POST still 409s (the
    # double-submit guard is a feature). Assigned ascending ONLY by the import
    # paths, which can count the file's multiset and therefore *prove* the row is
    # a distinct event, where a lone POST cannot. Kept off TransactionRead:
    # internal disambiguator, same reasoning as auto_category_id.
    occurrence: Mapped[int] = mapped_column(SmallInteger, default=0, server_default=text("0"))
    source: Mapped[TransactionSourceStr] = mapped_column(
        Enum(
            *get_args(TransactionSourceStr),
            name="transaction_source",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
        )
    )
    import_batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batches.id"))
    # Review/commit gate. NULL = pending (still in the per-batch review queue);
    # non-NULL = on the board. F2 manual POST stamps now() at create time;
    # import rows stay NULL until POST /imports/{batch_id}/commit.
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime())
    # FK declared composite-style in __table_args__ (ADR-0002 same-user
    # invariant). Do NOT add an inline ForeignKey here — that would create
    # a second, single-column FK on the same column and drift from the
    # migration.
    transfer_pair_id: Mapped[int | None] = mapped_column()

    # F3a labels — many-to-many via ``transaction_labels``. VIEWONLY: reads +
    # ``selectinload`` only. Writes go through
    # ``services/transaction_labels.set_labels_on_transaction`` because the join
    # carries ``user_id`` (composite same-user FKs, ADR-0002) that a plain
    # ``secondary`` relationship can't populate. The composite join is inferred
    # correctly (``user_id`` correlates on both sides) — no explicit
    # primaryjoin/secondaryjoin needed. First relationship on this scalar-FK model.
    labels: Mapped[list[Label]] = relationship(
        secondary="transaction_labels",
        viewonly=True,
        order_by="Label.name",
    )
