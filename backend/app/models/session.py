"""Refresh-token session model (PRD §Users & access v2).

One row per issued refresh token. The raw opaque token lives only in the
user's httpOnly cookie; the DB stores its sha256 (``token_hash``) so a DB
leak can't be replayed as a live token.

**Rotation lineage.** Every refresh rotates: the presented row is revoked and
a new row is issued carrying the same ``family_id``. Presenting an
already-revoked/rotated token (reuse) is treated as compromise — the whole
``family_id`` is revoked, killing any attacker session spun off the stolen
token. ``family_id`` is minted at login/register (the root of a lineage).

``token_hash`` is uniquely indexed (a hash collision or double-issue is a
bug, not a silent second row). ``expires_at`` bounds the refresh window;
``revoked_at`` marks logout / rotation / family-revoke.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class RefreshSession(Base, TimestampMixin):
    __tablename__ = "sessions"
    __table_args__ = (
        Index("uq_sessions_token_hash", "token_hash", unique=True),
        Index("ix_sessions_user_id", "user_id"),
        Index("ix_sessions_family_id", "family_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    family_id: Mapped[uuid.UUID] = mapped_column()
    token_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime())
