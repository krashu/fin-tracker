"""Merchant → category memory (per PRD §F3 exact-match auto-tagging).

One row per ``(user, normalized_merchant, category)`` triple. Multiple
categories per merchant are allowed because a user might tag the same
merchant differently across rows (Swiggy → Food usually, Swiggy → Gift
when they sent a gift card); the import_service picks the row with the
highest ``hit_count`` to prefill.

``last_used`` lets a future v1.5 cleanup pass prune stale tags without
losing the recency signal.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint, false, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, utcnow_default


class MerchantTagMap(Base, TimestampMixin):
    __tablename__ = "merchant_tag_map"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "merchant_normalized",
            "category_id",
            name="uq_merchant_tag_map_user_merchant_category",
        ),
        Index(
            "ix_merchant_tag_map_user_merchant",
            "user_id",
            "merchant_normalized",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    # 512 (not 256) to match transactions.merchant_normalized and stay ≥
    # merchant_raw's String(512): normalize_merchant is length-unbounded, so a
    # long parsed merchant must not overflow on Postgres (SQLite ignores the
    # cap). See migration 0020.
    merchant_normalized: Mapped[str] = mapped_column(String(512))
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"))
    hit_count: Mapped[int] = mapped_column(default=1, server_default=text("1"))
    # last_used: domain timestamp (when import_service last applied this rule);
    # distinct from updated_at (any row mutation, e.g. manual category re-point).
    last_used: Mapped[datetime] = mapped_column(default=utcnow_default, server_default=func.now())
    # Manual-override marker (F3 rule authoring). A pinned row wins prefetch_tag_map
    # regardless of hit_count (reducer orders `pinned DESC` first); the learning
    # path never sets it. pin/un-pin toggle ONLY this flag — never hit_count /
    # last_used — so un-pinning reverts cleanly to the learned ranking.
    pinned: Mapped[bool] = mapped_column(default=False, server_default=false())
