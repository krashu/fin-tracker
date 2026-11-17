"""realised_lots.kind marks FIFO matches apart from intraday matches (ADR-0015 rules 5, 7, 8)

Revision ID: 0043_realised_lot_kind
Revises: 0042_drop_import_batch_error_message
Create Date: 2026-11-16

A same-day ``indian_equity`` buy netted against a sell is not a capital gain, so each
``realised_lots`` row is now marked ``fifo`` or ``intraday``; F11's capital-gains statement
reads only ``fifo``. A CHECK ties ``intraday`` to ``buy_date = sell_date``.

Existing rows become ``fifo``, which was correct before netting existed; stale dev rows for
same-day equity trades refresh on the next write to their instrument. No other backfill.

The column has **no default at head**, following 0038's drop of ``fx_rate_to_inr``'s
(ADR-0016): the kind is tax-relevant, so a writer that forgot it would silently file an
intraday match as a capital gain. With no default the insert fails NOT NULL instead. The
server default exists only here, to fill existing rows, and is dropped in a second batch.

Two batch blocks on SQLite (the table copy needs the default; then the default goes). No
inbound FKs and no partial index on ``realised_lots``. The Enum CHECK is written once by hand
(``create_constraint=False`` on the type), as in 0008 and 0038; CHECK names are suffix-only
(ADR-0001 rule 3b).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0043_realised_lot_kind"
down_revision: str | Sequence[str] | None = "0042_drop_import_batch_error_message"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KIND = "kind IN ('fifo', 'intraday')"
_INTRADAY_SAME_DAY = "kind = 'fifo' OR buy_date = sell_date"
_KIND_TYPE = sa.Enum(
    "fifo", "intraday", name="realised_lot_kind", native_enum=False, create_constraint=False
)


def upgrade() -> None:
    with op.batch_alter_table("realised_lots") as batch_op:
        batch_op.add_column(sa.Column("kind", _KIND_TYPE, nullable=False, server_default="fifo"))
        batch_op.create_check_constraint("realised_lot_kind", _KIND)
        batch_op.create_check_constraint("intraday_same_day", _INTRADAY_SAME_DAY)
    with op.batch_alter_table("realised_lots") as batch_op:
        batch_op.alter_column(
            "kind", existing_type=_KIND_TYPE, existing_nullable=False, server_default=None
        )


def downgrade() -> None:
    with op.batch_alter_table("realised_lots") as batch_op:
        batch_op.drop_constraint("intraday_same_day", type_="check")
        batch_op.drop_constraint("realised_lot_kind", type_="check")
        batch_op.drop_column("kind")
