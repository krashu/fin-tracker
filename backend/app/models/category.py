"""Category model — flat list, no hierarchy in v1 (per PRD §F5).

``is_seeded`` flags the defaults the app inserts on first run; user-
created categories carry ``False``. Soft-delete via ``archived_at`` so the
foreign key from ``transactions.category_id`` doesn't break when a user
"removes" a category that still has historical rows.

``kind`` scopes a category to spending or income (parallel sets). Spend
categories serve both ``spend`` and ``refund`` transactions (a refund nets
against spend in the same category per §F4a); income categories serve
``income`` transactions; transfers carry ``category_id IS NULL`` and so
never reference a category. ``kind`` is set at create and immutable
thereafter — flipping it would orphan transactions tagged under the old
scope. The active-name unique index includes ``kind`` so the same name
(e.g. "Other") can exist once per scope.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, get_args

from sqlalchemy import Enum, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import expression

from app.models.base import Base, TimestampMixin

CategoryKindStr = Literal["spend", "income"]


class Category(Base, TimestampMixin):
    __tablename__ = "categories"
    # Partial unique index: active (non-archived) categories must have unique
    # names per user; archived rows can share names freely so soft-delete +
    # re-create with the same name works. Portable across SQLite >= 3.8 and
    # Postgres. Alembic autogenerate may miss the WHERE clause on partial
    # indexes — the first generated migration for this table must be
    # hand-checked (see docs/adr/0001-sqlite-postgres-portability.md).
    # ``kind`` is part of the index so "Other" (and any other shared name)
    # can exist once as a spend category and once as an income category.
    __table_args__ = (
        Index(
            "uq_categories_active_user_name",
            "user_id",
            "name",
            "kind",
            unique=True,
            sqlite_where=text("archived_at IS NULL"),
            postgresql_where=text("archived_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(64))
    # Python-side default AND server_default: the former populates ORM inserts
    # that omit kind (test fixtures, seeders), the latter backfills the migration's
    # ADD COLUMN on existing rows. Mirrors account.currency, which now carries both in the
    # model too — until this pairing was restated on all five enum columns, this was the ONE
    # column where the model matched the migration.
    kind: Mapped[CategoryKindStr] = mapped_column(
        Enum(
            *get_args(CategoryKindStr),
            name="category_kind",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
        ),
        default="spend",
        server_default="spend",
    )
    is_seeded: Mapped[bool] = mapped_column(default=False, server_default=expression.false())
    archived_at: Mapped[datetime | None] = mapped_column(default=None)
    # User-picked ``#rrggbb`` hex color for the category's dot/bar. NULL = derive
    # the color from the id (the Auto fallback). The hex shape is validated at the
    # schema boundary (CategoryCreate/Update), not in the DB, so a plain String
    # keeps the column portable. No server_default: NULL is the intended sentinel,
    # not a gap to backfill.
    color: Mapped[str | None] = mapped_column(String(16), default=None)
