"""Transaction ↔ label join (PRD §F3a) — many-to-many user tags.

An owned link between two owned rows, so it follows the **ADR-0002 composite
same-user FK pattern** (the ``transfer_pair_id`` precedent) rather than a lean
join: ``user_id`` is part of BOTH composite FKs, so a row can never link one
user's transaction to another user's label. ``user_id``'s referential integrity
to ``users`` is transitively guaranteed by those composite FKs (each parent FKs
``users``), so no direct ``users`` FK is declared here — the composite targets
are the isolation contract.

Both FKs are ``ON DELETE CASCADE`` (SQLite honours it under
``PRAGMA foreign_keys=ON``; see ``app/core/db.py``): deleting a transaction OR a
label auto-clears its link rows — that is why ``delete_transaction`` and the
label ``DELETE`` route need no manual join cleanup.

Writes go through ``services/transaction_labels.set_labels_on_transaction``: the
``user_id`` column means a plain ``secondary`` relationship can't auto-manage
rows (it wouldn't populate ``user_id``), so ``Transaction.labels`` is
``viewonly`` (reads / selectinload) and the join rows are inserted/deleted
explicitly.

Carries ``TimestampMixin`` (``created_at`` / ``updated_at``) like every other
model — no pure-join-table exemption. ``set_labels_on_transaction`` inserts these
rows *without* the timestamp columns, so both rely on the DB ``server_default``
(migration 0021 must stamp ``server_default=now()`` on them — ``TimestampMixin``
has no Python-side default).
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKeyConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class TransactionLabel(Base, TimestampMixin):
    __tablename__ = "transaction_labels"
    __table_args__ = (
        ForeignKeyConstraint(
            ["transaction_id", "user_id"],
            ["transactions.id", "transactions.user_id"],
            name="fk_transaction_labels_transaction_id_transactions",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["label_id", "user_id"],
            ["labels.id", "labels.user_id"],
            name="fk_transaction_labels_label_id_labels",
            ondelete="CASCADE",
        ),
        Index("ix_transaction_labels_user_label", "user_id", "label_id"),
    )

    # Composite PK (transaction_id, label_id) — one link per (txn, label). Named
    # pk_transaction_labels by the base naming convention, matching migration 0021.
    transaction_id: Mapped[int] = mapped_column(primary_key=True)
    label_id: Mapped[int] = mapped_column(primary_key=True)
    # Non-Optional → NOT NULL: a nullable member would make the composite FK
    # MATCH-SIMPLE (unchecked on NULL) and the same-user isolation would collapse.
    user_id: Mapped[uuid.UUID] = mapped_column()
