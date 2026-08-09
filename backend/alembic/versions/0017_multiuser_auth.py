"""multi-user auth: user credentials + refresh sessions + demo seed

Revision ID: 0017_multiuser_auth
Revises: 0016_widen_instrument_identity
Create Date: 2026-07-18

Lands the auth layer (PRD §Users & access v2 — see
``~/.claude/plans/we-need-to-make-glimmering-kurzweil.md``):

1. ``users``: add ``password_hash`` + ``display_name`` (plain ``ADD COLUMN`` —
   nullable, no rebuild) + a partial unique index on ``email`` (one account per
   email). The both-or-neither email/password invariant is enforced at the app
   layer, NOT a DB CHECK: adding a CHECK on SQLite needs a table rebuild, which
   fails here because ``users`` is referenced by seeded child rows (the demo
   user's categories/benchmarks) — the rebuild's implicit row-DELETE trips those
   FKs. Postgres (v2) can add the CHECK cheaply later.
2. ``sessions``: refresh-token rows (sha256 ``token_hash`` uniquely indexed,
   ``family_id`` rotation lineage, ``expires_at`` / ``revoked_at``). Mirrors
   ``app/models/session.py``.
3. **Seed the fixed-UUID row as the demo account** — set ``email`` +
   ``password_hash`` (argon2id of a known demo password, inlined so the
   migration stays a self-contained snapshot — the encoded hash carries its
   own params and verifies without app code). The row already exists (0001
   ``bulk_insert``); we only stamp credentials. Both non-null → satisfies the
   both-or-neither invariant (app-enforced; there is no DB CHECK — see point 1).

The demo password lives in :mod:`app.core.demo` for the login "Try the demo"
button; the hash here must correspond to it. Stamping it is unconditional — this
migration runs on every stack — so whether those creds actually authenticate is
decided at runtime by ``Settings.demo_login_permitted`` (off by default), never here.

Hand-written so constraint / index names match the SA ``NAMING_CONVENTION``;
``tests/test_migration_parity`` guards drift.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0017_multiuser_auth"
down_revision: str | Sequence[str] | None = "0016_widen_instrument_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_V1_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_DEMO_EMAIL = "demo@fin-tracker.local"
# argon2id digest of the demo password (see app/core/demo.py::DEMO_PASSWORD).
# Encoded hash is self-describing (v/m/t/p + salt) → verifies regardless of the
# app's configured PasswordHasher params. Inlined to keep this migration frozen.
_DEMO_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$YNWYruipcEYfcD4yhnAZTQ$"
    "VOQqaRjd/CXDE5kIN0FnvnGHB0P0rbKlPKZyaHazAVQ"
)


def upgrade() -> None:
    # 1. users: add credential columns. Plain ADD COLUMN (nullable) — no rebuild,
    #    so the FKs into users from seeded child rows stay intact.
    op.add_column("users", sa.Column("password_hash", sa.String(length=255), nullable=True))
    op.add_column("users", sa.Column("display_name", sa.String(length=128), nullable=True))

    # 2. One account per email. Partial (WHERE email IS NOT NULL) + unique.
    op.create_index(
        "uq_users_email",
        "users",
        ["email"],
        unique=True,
        sqlite_where=sa.text("email IS NOT NULL"),
        postgresql_where=sa.text("email IS NOT NULL"),
    )

    # 3. Refresh-session table.
    op.create_table(
        "sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("family_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_sessions_user_id_users"),
        sa.PrimaryKeyConstraint("id", name="pk_sessions"),
    )
    op.create_index("uq_sessions_token_hash", "sessions", ["token_hash"], unique=True)
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"], unique=False)
    op.create_index("ix_sessions_family_id", "sessions", ["family_id"], unique=False)

    # 4. Claim the seeded fixed-UUID row as the loginable demo account.
    #    Parameterised so the UUID matches on SQLite (hex) and Postgres (native).
    op.execute(
        sa.text(
            "UPDATE users SET email = :email, password_hash = :pw "
            "WHERE id = :uid AND email IS NULL AND password_hash IS NULL"
        ).bindparams(email=_DEMO_EMAIL, pw=_DEMO_PASSWORD_HASH, uid=_V1_USER_ID)
    )


def downgrade() -> None:
    op.drop_table("sessions")
    op.drop_index("uq_users_email", table_name="users")
    # Restore the pre-0017 state: demo row back to a null email (0001 seeded it
    # null), then drop the added columns. Plain drops — neither column is in a
    # constraint/index (the email index is already gone above).
    op.execute(sa.text("UPDATE users SET email = NULL WHERE id = :uid").bindparams(uid=_V1_USER_ID))
    op.drop_column("users", "display_name")
    op.drop_column("users", "password_hash")
