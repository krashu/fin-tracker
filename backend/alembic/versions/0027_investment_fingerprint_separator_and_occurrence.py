"""investment dedup key: \x1f separator + investment_transactions.occurrence

Revision ID: 0027_investment_fingerprint_separator_and_occurrence
Revises: 0026_rename_switch_pair_id_to_pair_id
Create Date: 2026-07-30

ADR-0006 (``docs/adr/0006-f4-dedup-key.md``) applied to the *investment* table. That
ADR states the separator rule and the occurrence design as **project-wide
conventions**, and §Consequences filed these two defects explicitly, so this
revision decides nothing new — it is 0025's shape on a second table with a
different hash function and a **nullable** fingerprint. Two coupled changes, shipped
together because they are one semantic change to that key:

1. ``investment_transactions.occurrence`` (SMALLINT NOT NULL DEFAULT 0), with the
   unique index widened to
   ``(user_id, instrument_id, fingerprint, occurrence)``. Two genuinely-distinct
   investment transactions agreeing on all five hashed fields — two identical
   same-day lumpsum buys of one fund, or two same-day SIP instalments across folios
   resolving to one instrument — can now both be stored. The index NAME is
   deliberately unchanged (ADR-0006's "do not rename" rule).
2. Every **non-NULL** stored fingerprint is recomputed over the ``\x1f``-joined
   payload. Previously the five fields were concatenated with no separator, leaving
   ``amount_native_paise | units_scaled`` — two variable-length non-negative
   integers — ambiguous: ``amount=12, units_scaled=345`` hashed identically to
   ``amount=123, units_scaled=45``.

**Only ``fingerprint IS NOT NULL`` rows are touched.** Manual rows must stay NULL or
the "NULLs are distinct → the backstop is inert for manual rows" invariant the model
docstring depends on breaks.

The hash is inlined below rather than imported from
``app.services.investment_import_service``. A migration must be frozen against the
formula as it stood at its own revision, or a future formula change silently
rewrites history — the same reason no other migration in this tree imports app code.
It is also NOT shared with ``app.services.fingerprint``'s ``_SEP``: these are two
independent formulas over different field lists.

**No backfill is needed for ``occurrence``.** The pre-fix in-loop-mutation bug plus
the 3-column unique index made duplicates unstorable, so every existing row is
occurrence 0 and the server default covers them.

**The recompute cannot transiently violate the widened index.** Adding a separator is
injective, so it can only *reduce* collisions: two rows hashing to the same NEW value
share all five inputs, so they shared the OLD value too and could not both have
existed under the pre-0027 index.

**No ``batch_alter_table``, and that is the point.** The 3-column key here is a
``unique=True`` **Index**, not a ``UniqueConstraint``, so on SQLite it can be dropped
and recreated in place — which sidesteps the table rebuild entirely, and with it the
self-referential composite-FK hazard that
``test_cli_upgrade_with_investment_data_succeeds`` exists to catch (the thing 0019's
docstring warns about and 0026 avoided by renaming natively). ``op.add_column`` is
likewise fine standalone on SQLite for a non-FK column. Migration 0010 is the
in-repo precedent for exactly this "add column, then standalone CREATE UNIQUE INDEX"
pairing on this table.

**Reversibility.** Fully reversible per ADR-0006 §Recompute procedure (e): all five
hashed inputs (``instrument_id``, ``date``, ``transaction_type``,
``amount_native_paise``, ``units``) remain stored, so the old formula is computable.
The data half reverses only when no
``(user_id, instrument_id, 5-input-tuple)`` group holds more than one row — two such
rows recompute to the SAME old fingerprint and the re-narrowed 3-column index refuses
them. That failure is LOUD by construction: ``downgrade()`` recomputes BEFORE
narrowing, so ``CREATE UNIQUE INDEX`` raises rather than silently merging two real
transactions. Same posture as 0025 / 0020.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0027_investment_fingerprint_separator_and_occurrence"
down_revision: str | Sequence[str] | None = "0026_rename_switch_pair_id_to_pair_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UQ = "uq_investment_transactions_user_instrument_fingerprint"
_TABLE = "investment_transactions"

# Lightweight table construct (NOT the ORM model) so SQLAlchemy applies the Date /
# Integer result processors on both SQLite ('YYYY-MM-DD' text) and Postgres (native
# date), while staying frozen against the model's future shape.
#
# ``units`` is declared sa.Integer ON PURPOSE. The column stores a scaled int (the
# ORM's ``Units`` TypeDecorator, 8 dp), and the payload hashes that RAW scaled value.
# Reading it through the decorator would unscale it to a Decimal and silently change
# every hash. ``test_0027_recomputes_investment_fingerprints`` pins a known scaled
# value so this cannot drift unnoticed.
_itxn = sa.table(
    _TABLE,
    sa.column("id", sa.Integer),
    sa.column("instrument_id", sa.Integer),
    sa.column("date", sa.Date),
    sa.column("transaction_type", sa.String),
    sa.column("amount_native_paise", sa.BigInteger),
    sa.column("units", sa.Integer),
    sa.column("fingerprint", sa.String),
)


def _recompute(separator: str) -> None:
    """Rewrite every non-NULL fingerprint as ``sha256(separator.join(5 fields))``.

    Bidirectional by parameter — ``"\\x1f"`` is the new formula, ``""`` the old one —
    so both directions share one implementation, mirroring 0025's ``_recompute`` and
    0018's ``_remap(old, new)``.

    The ``IS NOT NULL`` filter is load-bearing, not an optimisation: a manual row's
    NULL fingerprint is what keeps the unique index inert for manual entry, and
    hashing one would silently enrol it in dedup.
    """
    bind = op.get_bind()
    rows = bind.execute(
        sa.select(
            _itxn.c.id,
            _itxn.c.instrument_id,
            _itxn.c.date,
            _itxn.c.transaction_type,
            _itxn.c.amount_native_paise,
            _itxn.c.units,
        ).where(_itxn.c.fingerprint.is_not(None))
    ).all()
    for row in rows:
        payload = separator.join(
            (
                str(row.instrument_id),
                row.date.isoformat(),
                row.transaction_type,
                str(row.amount_native_paise),
                str(row.units),
            )
        )
        bind.execute(
            _itxn.update()
            .where(_itxn.c.id == row.id)
            .values(fingerprint=hashlib.sha256(payload.encode("utf-8")).hexdigest())
        )


def upgrade() -> None:
    # Step 1 — schema. server_default is mandatory, not cosmetic: the parity suite's
    # test_cli_upgrade_with_investment_data_succeeds inserts an
    # investment_transactions row by raw SQL without naming the column.
    op.add_column(
        _TABLE,
        sa.Column("occurrence", sa.SmallInteger(), nullable=False, server_default=sa.text("0")),
    )
    op.drop_index(_UQ, table_name=_TABLE)
    op.create_index(
        _UQ, _TABLE, ["user_id", "instrument_id", "fingerprint", "occurrence"], unique=True
    )

    # Step 2 — data.
    _recompute("\x1f")


def downgrade() -> None:
    # Recompute FIRST, narrow SECOND — see the module docstring's reversibility note.
    # This ordering is what makes a real-duplicate DB fail loudly at CREATE UNIQUE
    # INDEX instead of losing a transaction.
    _recompute("")
    op.drop_index(_UQ, table_name=_TABLE)
    op.create_index(_UQ, _TABLE, ["user_id", "instrument_id", "fingerprint"], unique=True)
    op.drop_column(_TABLE, "occurrence")
