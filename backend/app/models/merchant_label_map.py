"""Merchant → label memory (PRD §F3a Phase 2 — auto-learn user tags).

Sibling to :mod:`app.models.merchant_tag_map` (merchant → *category*): one row
per ``(user, normalized_merchant, label)`` triple, so a merchant accumulates a
*set* of learned labels (unlike the single winning category). The import review
queue prefills every label whose ``hit_count`` clears the confidence bar
(``services/merchant_labels.LABEL_PREFILL_MIN``).

**Composite same-user FK (ADR-0002 pattern), not the plain FK ``merchant_tag_map``
uses.** This is an owned link between two owned rows — a user's merchant memory
and that user's label — exactly the :mod:`app.models.transaction_label` case. The
map also needs ``ON DELETE CASCADE`` on the label FK (``labels`` **hard-delete**,
unlike the soft-deleted ``categories`` behind ``merchant_tag_map``, whose rows are
deliberately KEPT on archive, so that map needs no cascade at all). A plain FK +
cascade would let one user's
label-delete cascade-wipe a stray cross-user map row; the composite
``(label_id, user_id) → labels(id, user_id)`` makes such a row impossible at the
DB level, so the cascade is tenant-safe. ``user_id``'s integrity to ``users`` is
transitively guaranteed by that composite FK (``labels`` FKs ``users``), so no
direct ``users`` FK is declared — same as ``transaction_label``.

``last_used`` mirrors ``merchant_tag_map`` — reserved for a future v1.5 stale-tag
cleanup pass; there is no decay / un-learning in v1.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKeyConstraint, Index, String, UniqueConstraint, false, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, utcnow_default


class MerchantLabelMap(Base, TimestampMixin):
    __tablename__ = "merchant_label_map"
    __table_args__ = (
        ForeignKeyConstraint(
            ["label_id", "user_id"],
            ["labels.id", "labels.user_id"],
            name="fk_merchant_label_map_label_id_labels",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "user_id",
            "merchant_normalized",
            "label_id",
            name="uq_merchant_label_map_user_merchant_label",
        ),
        Index(
            "ix_merchant_label_map_user_merchant",
            "user_id",
            "merchant_normalized",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # NOT NULL: a nullable member would slacken the composite FK to MATCH-SIMPLE
    # (unchecked on NULL) and the same-user isolation would collapse.
    user_id: Mapped[uuid.UUID] = mapped_column()
    # 512 to match transactions.merchant_normalized / merchant_tag_map (see
    # migration 0020): normalize_merchant is length-unbounded, so a long parsed
    # merchant must not overflow on Postgres (SQLite ignores the cap).
    merchant_normalized: Mapped[str] = mapped_column(String(512))
    label_id: Mapped[int] = mapped_column()
    hit_count: Mapped[int] = mapped_column(default=1, server_default=text("1"))
    # last_used: domain timestamp (when import last prefilled this rule); distinct
    # from updated_at (any row mutation).
    last_used: Mapped[datetime] = mapped_column(default=utcnow_default, server_default=func.now())
    # Manual-override marker (F3a rule authoring). A pinned label prefills even
    # below LABEL_PREFILL_MIN (prefetch_label_map widens its WHERE to
    # `hit_count >= MIN OR pinned`); the learning path never sets it. pin/un-pin
    # toggle ONLY this flag — never hit_count / last_used.
    pinned: Mapped[bool] = mapped_column(default=False, server_default=false())
