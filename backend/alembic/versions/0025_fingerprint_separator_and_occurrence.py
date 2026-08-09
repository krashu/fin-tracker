"""f4 dedup key: \x1f separator + transactions.occurrence

Revision ID: 0025_fingerprint_separator_and_occurrence
Revises: 0024_add_pinned_to_merchant_maps
Create Date: 2026-07-30

ADR-0006 (``docs/adr/0006-f4-dedup-key.md``). Two coupled changes to the PRD §F4
dedup key, shipped together because they are one semantic change to that key:

1. ``transactions.occurrence`` (SMALLINT NOT NULL DEFAULT 0), with the unique
   constraint widened to ``(user_id, account_id, fingerprint, occurrence)``. Two
   genuinely-distinct transactions agreeing on all four hashed fields — two auto
   rides at the same fare on one day — can now both be stored. The constraint
   NAME is deliberately unchanged so the Postgres branch of
   ``app.core.db_errors.is_unique_violation`` keeps mapping the conflict to a 409
   with no code edit (its SQLite branch is a subset test over ``table.col``
   tokens, so the extra column is inert there too).
2. Every stored fingerprint is recomputed over the ``\x1f``-joined payload.
   Previously the four fields were concatenated with no separator, leaving two
   ambiguous boundaries between variable-length values: merchant ``"amazon1"`` +
   account ``2`` hashed identically to ``"amazon"`` + account ``12``.

The hash is inlined below rather than imported from ``app.services.fingerprint``.
A migration must be frozen against the formula as it stood at its own revision,
or a future formula change silently rewrites history — the same reason no other
migration in this tree imports app code.

**No backfill is needed for ``occurrence``.** The pre-fix bug plus the 3-column
unique constraint made duplicates unstorable, so every existing row is
occurrence 0 and the server default covers them. No archaeology.

**The recompute cannot transiently violate the widened constraint.** Two rows
hashing to the same NEW value share all four inputs, so they shared the OLD value
too and could not both have existed under the pre-0025 constraint.

SQLite needs ``batch_alter_table`` for the constraint swap. That is the proven
path on this table (migrations 0009 / 0020): the batch copy re-validates the
self-referential composite FK against the *pre-existing*
``uq_transactions_id_user`` target, and ``alembic/env.py`` sets
``PRAGMA foreign_keys=OFF`` outside the transaction for the copy itself. Migration
0005's ``foreign key mismatch`` trap was a different failure — a schema-shape
error, where the new table's composite FK had no composite unique target on the
OLD table — which is precisely why 0005 notes the pragma could not save it. That
target has existed since 0005, so the copy validates cleanly here.

**Reversibility.** The schema half reverses always. The data half reverses only
when no ``(user_id, account_id, base-tuple)`` group holds more than one row: two
such rows recompute to the SAME old fingerprint, and the re-narrowed 3-column
constraint refuses them. That failure is LOUD by construction — ``downgrade()``
recomputes BEFORE narrowing, so SQLite/Postgres raises at the batch copy rather
than silently merging two real transactions. Same posture as 0020's note.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0025_fingerprint_separator_and_occurrence"
down_revision: str | Sequence[str] | None = "0024_add_pinned_to_merchant_maps"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UQ = "uq_transactions_user_account_fingerprint"

# Lightweight table construct (NOT the ORM model) so SQLAlchemy applies the Date /
# Integer result processors on both SQLite ('YYYY-MM-DD' text) and Postgres
# (native date), while staying frozen against the model's future shape.
_txn = sa.table(
    "transactions",
    sa.column("id", sa.Integer),
    sa.column("account_id", sa.Integer),
    sa.column("date", sa.Date),
    sa.column("amount_paise", sa.BigInteger),
    sa.column("merchant_normalized", sa.String),
    sa.column("fingerprint", sa.String),
)


def _recompute(separator: str) -> None:
    """Rewrite every fingerprint as ``sha256(separator.join(4 fields))``.

    Bidirectional by parameter — ``"\\x1f"`` is the new formula, ``""`` the old
    one — so both directions share one implementation, mirroring 0018's
    ``_remap(old, new)``. Rewrites unconditionally: unlike a user-picked category
    colour there is no hand-authored fingerprint to protect (test-fixture
    literals included, which is why the parity suite keys off ``id``).
    """
    bind = op.get_bind()
    rows = bind.execute(
        sa.select(
            _txn.c.id,
            _txn.c.date,
            _txn.c.amount_paise,
            _txn.c.merchant_normalized,
            _txn.c.account_id,
        )
    ).all()
    for row in rows:
        payload = separator.join(
            (
                row.date.isoformat(),
                str(row.amount_paise),
                row.merchant_normalized,
                str(row.account_id),
            )
        )
        bind.execute(
            _txn.update()
            .where(_txn.c.id == row.id)
            .values(fingerprint=hashlib.sha256(payload.encode("utf-8")).hexdigest())
        )


def upgrade() -> None:
    # Step 1 — schema. Both ops in ONE batch so the recreate emits the new column
    # and the 4-column constraint in a single CREATE TABLE (one rebuild, not two).
    # server_default is mandatory, not cosmetic: a NOT NULL add_column inside a
    # batch recreate would otherwise insert NULL into the copy, and the parity
    # suite inserts transactions rows by raw SQL without naming the column.
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.add_column(
            sa.Column(
                "occurrence",
                sa.SmallInteger(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.drop_constraint(_UQ, type_="unique")
        batch_op.create_unique_constraint(
            _UQ, ["user_id", "account_id", "fingerprint", "occurrence"]
        )

    # Step 2 — data.
    _recompute("\x1f")


def downgrade() -> None:
    # Recompute FIRST, narrow SECOND — see the module docstring's reversibility
    # note. This ordering is what makes a real-duplicate DB fail loudly at the
    # batch copy instead of losing a transaction.
    _recompute("")
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.drop_constraint(_UQ, type_="unique")
        batch_op.create_unique_constraint(_UQ, ["user_id", "account_id", "fingerprint"])
        batch_op.drop_column("occurrence")
