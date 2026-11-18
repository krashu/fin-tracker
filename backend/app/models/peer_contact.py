"""PeerContact model — contacts for peer debt and Khatabook balances (ADR-0017).

Active contacts (non-archived) have unique names per user (case- and whitespace-insensitive)
via the partial index ``uq_peer_contacts_active_user_name``. Archived rows can share names
so soft-deleted contacts can be re-created freely (mirrors ``accounts`` and ``categories``).

``uq_peer_contacts_user_id_id`` provides the composite-unique target for same-user
composite foreign keys from ``peer_entries`` (ADR-0003 rule 10).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class PeerContact(Base, TimestampMixin):
    __tablename__ = "peer_contacts"
    __table_args__ = (
        UniqueConstraint("id", "user_id", name="uq_peer_contacts_user_id_id"),
        Index(
            "uq_peer_contacts_active_user_name",
            "user_id",
            text("LOWER(TRIM(name))"),
            unique=True,
            sqlite_where=text("archived_at IS NULL"),
            postgresql_where=text("archived_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(100))
    phone: Mapped[str | None] = mapped_column(String(20), default=None)
    upi_id: Mapped[str | None] = mapped_column(String(100), default=None)
    notes: Mapped[str | None] = mapped_column(Text, default=None)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), default=None)
