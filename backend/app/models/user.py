"""User model.

Multi-user (PRD §Users & access v2): every owned table FKs to a ``users``
row via :data:`CurrentUserId`, resolved from the request's access-token
cookie (see :mod:`app.core.security` / :mod:`app.api.deps`).

``id`` is :class:`uuid.UUID` (v4, Python-side default). Random rather than
sequential — fin-tracker's write volume is low enough that index-locality
cost is negligible, and a UUID PK avoids exposing user-count via the API.

``email`` and ``password_hash`` are nullable at the column and follow a
**both-or-neither** invariant — a row has both (a real/demo user) or neither.
The invariant is enforced at the app layer (register always writes both; no
endpoint writes one alone), NOT by a DB CHECK: adding a CHECK to ``users`` on
SQLite would require rebuilding the table, which fails because it is
referenced by seeded child rows (categories/benchmarks for the demo user).
Postgres (v2) can add the CHECK cheaply later. A partial unique index
enforces one account per email (case-normalised by the register/login flow
before storage).

``password_hash`` is an argon2id digest (see :mod:`app.core.security`) — the
raw password never touches the DB.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class User(Base, TimestampMixin):
    __tablename__ = "users"
    __table_args__ = (
        # One account per email. Partial (WHERE email IS NOT NULL) so a future
        # credential-less row wouldn't collide on NULL; portable across SQLite
        # and Postgres.
        Index(
            "uq_users_email",
            "email",
            unique=True,
            sqlite_where=text("email IS NOT NULL"),
            postgresql_where=text("email IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    email: Mapped[str | None] = mapped_column(String(255))
    password_hash: Mapped[str | None] = mapped_column(String(255))
    display_name: Mapped[str | None] = mapped_column(String(128))
