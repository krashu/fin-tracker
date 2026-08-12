"""Merchant string -> canonical key (PRD §F3 / ADR-0011 merchant-alias layer).

Second normalisation layer, downstream of and independent from
:func:`app.services.merchant.normalize_merchant` (frozen -- see its CHANGE
HAZARD block). Where ``normalize_merchant`` is lowercase + whitespace collapse
and feeds the F4 fingerprint, this table lets a user fold multiple raw
descriptors (``swiggy*blr*12345``, ``upi/swiggy/9876@ybl``) onto one
``canonical`` string, which becomes the new key F3/F3a read sites aggregate
on (Phase A2). See :mod:`app.services.merchant_alias` for the resolver that
reads this table; nothing writes ``canonical`` back onto a transaction row.

One row per ``(user, pattern)``. ``pattern`` and ``canonical`` are both
``normalize_merchant()``-normalized at the API boundary (Phase A4's
``/rules/aliases`` routes) -- this model trusts its caller and does not
normalize, per the boundary-only validation rule.

``is_seeded`` mirrors ``Category.is_seeded`` -- flags the ~100 dictionary
entries a later phase (A5) inserts at registration, distinct from
``merchant_tag_map.hit_count == 0`` (decision 4), which is a different
table's confidence marker.
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint, false
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class MerchantAlias(Base, TimestampMixin):
    __tablename__ = "merchant_alias"
    __table_args__ = (
        UniqueConstraint("user_id", "pattern", name="uq_merchant_alias_user_pattern"),
        Index("ix_merchant_alias_user_id", "user_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    # 512 to match merchant_tag_map / merchant_label_map's merchant_normalized
    # (migration 0020) -- normalize_merchant is length-unbounded.
    pattern: Mapped[str] = mapped_column(String(512))
    canonical: Mapped[str] = mapped_column(String(512))
    is_seeded: Mapped[bool] = mapped_column(default=False, server_default=false())
