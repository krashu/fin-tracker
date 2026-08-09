"""Account model — credit card / bank / cash / investment buckets.

``opening_balance_paise`` is signed int64 (debt / negative balance for a
fresh CC; positive for a bank account starting with cash). ``last4`` is
nullable for cash accounts. ``currency`` is hardcoded INR for v1; the
column exists for forward-compat, but adding v2's GBP/EUR is a schema
change, not just a data write — the ``CHECK (currency IN (...))`` (see
the enum note below) must be recreated (batch table rebuild on SQLite,
``DROP``/``ADD CONSTRAINT`` on Postgres).

Active (non-archived) account names are unique per user via the partial
index ``uq_accounts_active_user_name``; archived rows can share names so
soft-delete + recreate works (mirrors the categories pattern).

``parent_account_id`` is the CC → bank link for F4a auto-reconciliation
(PRD §F4a rule 1: "This rule activates only when a CC account has been
associated with a parent bank account"). It is nullable and deliberately absent
from ``AccountCreate``: the link is set via ``PATCH /accounts/{id}``, whose
five-rule gate needs the stored ``type`` to validate against. Both halves now
ship — the PATCH route and the "Paid from" picker in /settings/accounts — so F4a
is reachable. It stays dark for any card the user has not linked, which is the
intended default, not a gap.

``type`` and ``currency`` use SQLAlchemy ``Enum(..., native_enum=False)``
so the values land as ``VARCHAR + CHECK constraint`` on SQLite and
remain trivially portable to Postgres without rewriting the column.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, get_args

from sqlalchemy import BigInteger, Enum, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

AccountTypeStr = Literal["credit_card", "bank", "cash", "investment"]
CurrencyStr = Literal["INR", "USD"]


class Account(Base, TimestampMixin):
    __tablename__ = "accounts"
    __table_args__ = (
        Index(
            "uq_accounts_active_user_name",
            "user_id",
            "name",
            unique=True,
            sqlite_where=text("archived_at IS NULL"),
            postgresql_where=text("archived_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(128))
    type: Mapped[AccountTypeStr] = mapped_column(
        Enum(
            *get_args(AccountTypeStr),
            name="account_type",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
        )
    )
    issuer: Mapped[str | None] = mapped_column(String(64))
    last4: Mapped[str | None] = mapped_column(String(4))
    opening_balance_paise: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0")
    )
    currency: Mapped[CurrencyStr] = mapped_column(
        Enum(
            *get_args(CurrencyStr),
            name="currency",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
        ),
        default="INR",
        # Python-side default AND server_default, the pairing category.kind documents: the
        # former populates ORM inserts that omit currency, the latter is what a raw
        # INSERT (a migration, a script, `sqlite3`) relies on. Alembic 0001 has always
        # declared it; the model did not, so `create_all` — the schema every test outside
        # test_migration_parity builds — raised NOT NULL where the migrated DB stored 'INR',
        # and the first Postgres autogenerate would have emitted DROP DEFAULT.
        server_default="INR",
    )
    parent_account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"))
    archived_at: Mapped[datetime | None] = mapped_column(default=None)
