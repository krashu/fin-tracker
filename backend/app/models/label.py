"""Label model — freeform user tags on spending transactions (PRD §F3a).

Distinct from the F3 merchant→category ``merchant_tag_map`` "tag" domain: a
label is a cross-cutting user tag (``#online``, ``#travel``) applied manually,
orthogonal to the single ``category``. User-facing name is "Tags" (rendered with
a leading ``#``); the stored ``name`` is the normalized plain word (lowercase, no
``#``, whitespace collapsed, ``;`` removed — see
``services/transaction_labels.normalize_label_name``). Unique per user.

**Hard-delete** (no ``archived_at``): removing a label cascades its
``transaction_labels`` links, so a plain ``UniqueConstraint(user_id, name)``
suffices — no partial "active-name" carve-out like ``categories`` needs.

``uq_labels_id_user`` is the composite-unique target the same-user FK from
``transaction_labels`` requires (mirrors ``uq_transactions_id_user``; ADR-0002 /
migration 0005 reasoning). ``id`` alone is the PK; the composite unique is
Postgres-portability insurance for that composite FK.
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class Label(Base, TimestampMixin):
    __tablename__ = "labels"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_labels_user_name"),
        Index("uq_labels_id_user", "id", "user_id", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(64))
