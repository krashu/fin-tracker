"""Transaction model — the main spend/income table (per PRD §F1, §F4).

``amount_paise`` is signed int64: negative for spends and income reversals,
positive for income / payments / refunds (ADR-0009; zero never). The
parser ``RawTransaction.txn_type`` (per-parser vocabulary) maps into the PRD-level
``transaction_type`` enum here when the future ``import_service``
composes a row.

**A refund is not a transaction type.** It is a ``spend`` row carrying a
*positive* ``amount_paise`` — derived at read time, never stored
(``docs/adr/0009-refund-as-signed-spend.md``); an income reversal is likewise
a *negative* ``income`` row. So every type accepts either non-zero sign, and
every F8 aggregate discriminates on the sign rather than on the type.
A refund may optionally point at the spend it reverses through
``refund_of_id`` (ADR-0009 rule 13) — informational only, never read by a sum.
Identity is unaffected: the ADR-0006 fingerprint never hashed
``transaction_type``, so collapsing the enum moves no hash,
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
movement of money. Populated by four writers: F4a auto-reconciliation, the manual
link (``POST /transactions/{id}/link``), F2 manual transfers and backup restore, per
``docs/adr/0002-transfer-pair-id-semantics.md`` (Accepted).
Nullable for non-transfer rows. Five DB-level invariants enforce the
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
* ``ck_transactions_paired_is_transfer`` — CHECK ``transfer_pair_id IS
  NULL OR transaction_type = 'transfer'`` (ADR-0002 rule 8). A paired row
  is a transfer, so every writer sets the pair and the type in one UPDATE
  per row (migration 0038).
* ``ck_transactions_pre_link_category_while_linked`` — CHECK
  ``pre_link_category_id IS NULL OR (transfer_pair_id IS NOT NULL AND category_id IS NULL)``
  (ADR-0002 rule 9). A link moves the row's category aside; a saved category exists only
  while the row is paired and uncategorised, so any category written on a linked leg must
  clear it in the same UPDATE (migration 0044).

Symmetry (``A.transfer_pair_id = B ⇒ B.transfer_pair_id = A``) and
exactly-two pairing are NOT DB-enforceable portably (would need
triggers); they're service-layer concerns, held by the pair writers (F4a
``auto_link_cc_bill``, the manual link and ``POST /transactions/transfer``, ADR-0002).
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


# All four origin inputs or none; set inputs imply a stored origin_fingerprint. Mirrored by
# alembic/versions/0041_portable_origin_inputs.py (which inlines it; migrations import nothing).
_ORIGIN_ALL_OR_NONE = (
    "(origin_date IS NULL AND origin_amount_paise IS NULL "
    "AND origin_merchant_normalized IS NULL AND origin_account_id IS NULL) "
    "OR (origin_date IS NOT NULL AND origin_amount_paise IS NOT NULL "
    "AND origin_merchant_normalized IS NOT NULL AND origin_account_id IS NOT NULL "
    "AND origin_fingerprint IS NOT NULL)"
)

# A saved pre-link category exists only on a linked, uncategorised row. Mirrored by
# alembic/versions/0044_pre_link_category.py (which inlines it; migrations import nothing).
_PRE_LINK_CATEGORY_WHILE_LINKED = (
    "pre_link_category_id IS NULL OR (transfer_pair_id IS NOT NULL AND category_id IS NULL)"
)


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
        # ADR-0003 rule 10: every other owned FK is same-user composite too.
        ForeignKeyConstraint(
            ["account_id", "user_id"],
            ["accounts.id", "accounts.user_id"],
            name="fk_transactions_account_id_accounts",
        ),
        ForeignKeyConstraint(
            ["category_id", "user_id"],
            ["categories.id", "categories.user_id"],
            name="fk_transactions_category_id_categories",
        ),
        ForeignKeyConstraint(
            ["auto_category_id", "user_id"],
            ["categories.id", "categories.user_id"],
            name="fk_transactions_auto_category_id_categories",
        ),
        ForeignKeyConstraint(
            ["import_batch_id", "user_id"],
            ["import_batches.id", "import_batches.user_id"],
            name="fk_transactions_import_batch_id_import_batches",
        ),
        # ADR-0009 rule 13: refund → original, same-user self-reference. ON DELETE NO
        # ACTION: every delete that can leave a referencing row behind (single, bulk, demo
        # reseed: services/transaction_delete.delete_owned_transactions) nulls the refunds
        # pointing at a row first; user teardown deletes them in the same statement.
        ForeignKeyConstraint(
            ["refund_of_id", "user_id"],
            ["transactions.id", "transactions.user_id"],
            name="fk_transactions_refund_of_id_transactions",
        ),
        # ADR-0006 rule 17: the origin's identity inputs, frozen by the first identity
        # edit of an imported row. The account is stored because an account move is an
        # identity edit; composite same-user FK like every owned FK (ADR-0003 rule 10).
        ForeignKeyConstraint(
            ["origin_account_id", "user_id"],
            ["accounts.id", "accounts.user_id"],
            name="fk_transactions_origin_account_id_accounts",
        ),
        CheckConstraint(
            "transfer_pair_id IS NULL OR transfer_pair_id != id",
            name="no_self_pair",
        ),
        CheckConstraint(_ORIGIN_ALL_OR_NONE, name="origin_inputs_all_or_none"),
        # ADR-0002 rule 9: the category a link moved aside; composite same-user FK.
        ForeignKeyConstraint(
            ["pre_link_category_id", "user_id"],
            ["categories.id", "categories.user_id"],
            name="fk_transactions_pre_link_category_id_categories",
        ),
        CheckConstraint(_PRE_LINK_CATEGORY_WHILE_LINKED, name="pre_link_category_while_linked"),
        # ADR-0002 rule 8: the type, not the link, excludes a row from spend and income.
        CheckConstraint(
            "transfer_pair_id IS NULL OR transaction_type = 'transfer'",
            name="paired_is_transfer",
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
    account_id: Mapped[int] = mapped_column()
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
    # 512 to match merchant_raw's cap (migration 0020). normalize_merchant can lengthen
    # text, so every write boundary refuses a merchant that normalizes past this width
    # rather than truncating it (ADR-0006 rule 3b; services.merchant.MERCHANT_MAX_CHARS).
    # SQLite ignores the cap, so the refusal is the only guard until Postgres.
    merchant_normalized: Mapped[str] = mapped_column(String(512))
    category_id: Mapped[int | None] = mapped_column()
    # Frozen record of the category the import auto-tag (F3) prefilled at ingest,
    # for the acceptance-rate metric (GET /dashboards/tagging-stats, PRD
    # §Success-metrics: ≥80% pre-tagged correctly). Set ONLY by import_service
    # when a merchant_tag_map match prefills category_id; NULL for manual rows
    # and for import rows with no suggestion. NEVER synced on PATCH — a later
    # category_id edit is exactly what the metric counts as "not kept". Kept off
    # TransactionRead (internal metric field; the wire schema stays narrow).
    auto_category_id: Mapped[int | None] = mapped_column()
    # PRD §F4 dedup key — *what does this row say*. Recomputed whenever a PATCH
    # edits one of the four ADR-0006 identity inputs, and unique-constrained
    # together with `occurrence` (see __table_args__).
    fingerprint: Mapped[str] = mapped_column(String(64))
    # ADR-0006 rule 17 — *which statement line produced this row*. Same formula, stamped by
    # import_service at STAGE time (equal to `fingerprint`) and never rewritten, so an edit to
    # an identity column does not read as a deletion to the importer and re-stage the pre-edit
    # row. NULL = no external source line: manual entry, both transfer legs, F4a, the demo
    # seeder — those key on their own current `fingerprint` instead, via the file-dedup
    # prefetches' COALESCE(origin_fingerprint, fingerprint). Always recomputable: from the
    # origin_* inputs below once the row has been edited, else it equals `fingerprint`. Backup
    # restore recomputes it on the target from those inputs; a hash is never carried because it
    # bakes in the install-local account id. Deliberately neither unique nor indexed; do not
    # "simplify" the COALESCE away and never recompute this on PATCH.
    origin_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # The four inputs origin_fingerprint was hashed from, frozen by the first identity edit of
    # a row that has an origin (PATCH). All four or none (CHECK origin_inputs_all_or_none);
    # NULL means "the origin is the row's own current identity". Kept off TransactionRead.
    origin_date: Mapped[date_t | None] = mapped_column(Date, nullable=True)
    origin_amount_paise: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    origin_merchant_normalized: Mapped[str | None] = mapped_column(String(512), nullable=True)
    origin_account_id: Mapped[int | None] = mapped_column()
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
    import_batch_id: Mapped[int | None] = mapped_column()
    # Review/commit gate. NULL = pending (still in the per-batch review queue);
    # non-NULL = on the board. F2 manual POST stamps now() at create time;
    # import rows stay NULL until POST /imports/{batch_id}/commit.
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime())
    # FK declared composite-style in __table_args__ (ADR-0002 same-user
    # invariant). Do NOT add an inline ForeignKey here — that would create
    # a second, single-column FK on the same column and drift from the
    # migration.
    transfer_pair_id: Mapped[int | None] = mapped_column()
    # ADR-0002 rule 9: the type an EXISTING row had before a link made it a transfer
    # (F4a auto-link, the manual link and backup restore write it). Unlink and
    # partner-delete restore it and clear it. NULL on legs born as transfers (F2), which
    # stay `transfer`. The full vocabulary on
    # purpose: F4a's bank candidate may already be a `transfer`. Doubles as provenance;
    # backup carries it. Internal, kept off TransactionRead like auto_category_id.
    pre_link_type: Mapped[TransactionTypeStr | None] = mapped_column(
        Enum(
            *get_args(TransactionTypeStr),
            name="pre_link_type",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
        ),
        nullable=True,
    )
    # ADR-0002 rule 9: the category an EXISTING row had before a link cleared it (a pair
    # belongs to no category). Unlink and partner-delete restore it unless the user set a
    # category on the leg since: every category write on a linked leg clears it, and the DB
    # CHECK enforces that. Composite same-user FK in __table_args__. Internal, kept off
    # TransactionRead; backup carries it.
    pre_link_category_id: Mapped[int | None] = mapped_column()
    # ADR-0009 rule 13: optional link from a refund (a positive `spend`) to the
    # confirmed negative `spend` it reverses. Written only by PUT/DELETE
    # /transactions/{id}/refund-link; several refunds may point at one original.
    # Informational — no sum reads it, netting is by sign. Composite FK in
    # __table_args__; surfaced on TransactionRead for the refund picker.
    refund_of_id: Mapped[int | None] = mapped_column()

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
