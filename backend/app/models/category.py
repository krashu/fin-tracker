"""Category model — a two-level hierarchy (per PRD §F5, ADR-0012).

``parent_id`` is a nullable self-FK capped at depth 2: a row with
``parent_id IS NULL`` is a parent, a row pointing at one is a subcategory,
and a subcategory can never itself be a parent. The cap is enforced in
``app/api/v1/categories.py``, not here — this model carries no parent
validation. The FK is composite same-user ``(parent_id, user_id) →
(id, user_id)`` with ``ON DELETE NO ACTION`` (ADR-0012 rule 1): a parent
can never be another user's, and hard-deleting a parent that still has
children fails rather than cascading.

``is_seeded`` flags the defaults the app inserts on first run; user-
created categories carry ``False``. Soft-delete via ``archived_at`` so the
foreign key from ``transactions.category_id`` doesn't break when a user
"removes" a category that still has historical rows.

``kind`` scopes a category to spending or income (parallel sets). Spend
categories serve ``spend`` rows of either sign — a refund (ADR-0009) or a
purchase cashback (ADR-0013) is a positive spend and nets against spend in
the same category; income categories serve ``income`` transactions;
transfers carry ``category_id IS NULL`` and so never reference a category.
``kind`` is set at create and immutable thereafter — flipping it would
orphan transactions tagged under the old scope. The active-name unique
index includes ``kind`` so the same name (e.g. "Other") can exist once per
scope.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, get_args

from sqlalchemy import (
    CheckConstraint,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    and_,
    text,
)
from sqlalchemy.orm import Mapped, foreign, mapped_column, relationship, remote
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
        Index("ix_categories_user_parent_id", "user_id", "parent_id"),
        # ADR-0012 rule 7: a root always carries a colour; only a subcategory may inherit.
        CheckConstraint("parent_id IS NOT NULL OR color IS NOT NULL", name="root_has_color"),
        # ADR-0003 rule 10: the (id, user_id) target every same-user composite FK into
        # categories references — the parent FK below included.
        Index("uq_categories_id_user", "id", "user_id", unique=True),
        # ADR-0012 rule 1: no ``ondelete`` = NO ACTION. Only whole-user teardown hard-
        # deletes categories, and it removes subcategories first (rule 11).
        ForeignKeyConstraint(
            ["parent_id", "user_id"],
            ["categories.id", "categories.user_id"],
            name="fk_categories_parent_id_categories",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    parent_id: Mapped[int | None] = mapped_column(default=None, nullable=True)
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
    # User-picked ``#rrggbb`` hex color for the category's dot/bar. NULL only on a
    # subcategory (inherits its parent's hue); a root always carries one: the API,
    # promotion and restore assign the least-used palette slot of its kind (ADR-0012
    # rule 7), and ``ck_categories_root_has_color`` enforces it. The hex shape is
    # validated at the schema boundary.
    color: Mapped[str | None] = mapped_column(String(16), default=None)

    # The FK is two columns, but only ``parent_id`` is foreign here: ``user_id`` is the
    # row's own tenant and must never be written through this relationship — otherwise
    # ``child.parent = None`` would null ``user_id`` along with ``parent_id``.
    parent: Mapped[Category | None] = relationship(
        "Category",
        primaryjoin=lambda: and_(
            foreign(Category.parent_id) == remote(Category.id),
            Category.user_id == remote(Category.user_id),
        ),
        back_populates="subcategories",
    )
    # NOT cascade="all, delete-orphan" — de-associating a child (PATCH parent_id
    # to a new parent, or to null to promote it to root) is a legitimate,
    # user-facing write, not a delete. delete-orphan would turn that PATCH into
    # a row DELETE and take the child's transactions.category_id references down
    # with it (ADR-0012 rule 11). The default cascade ("save-update, merge") is what
    # this relationship wants. passive_deletes="all" leaves a parent delete to the
    # DB's NO ACTION, so it fails while children exist instead of the ORM quietly
    # re-rooting loaded children. No user-facing path hard-deletes a category.
    subcategories: Mapped[list[Category]] = relationship(
        "Category",
        primaryjoin=lambda: and_(
            foreign(remote(Category.parent_id)) == Category.id,
            remote(Category.user_id) == Category.user_id,
        ),
        back_populates="parent",
        passive_deletes="all",
    )
