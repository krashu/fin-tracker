"""asset-class currency CHECKs: us_* are USD, indian_*/nps are INR

Revision ID: 0045_asset_class_currency
Revises: 0044_pre_link_category
Create Date: 2026-11-16

**C8** (PRD §F7). The rule that ``us_equity``/``us_etf`` hold USD and ``indian_equity``/
``indian_mf``/``nps`` hold INR lived only in the instrument API validator and the investment CSV
parser. Two CHECKs on ``instruments`` now back both: ``usd_only_asset_class`` and
``inr_only_asset_class`` (ADR-0001 rules 2, 3 and 3b). ``nps`` joins INR (a rupee-only scheme);
``fd``, ``bond``, ``gold`` and ``other`` take either currency. The API and the CSV parser stay as
boundary checks and read the same tuples from ``app.models.instrument``.

**No backfill.** A dev database holding a mismatched row fails the preflight, by design. The
preflight is count-only, as 0037 and 0038 (no symbols, no ids). There is no in-app fix, since
``currency`` and ``asset_class`` are locked on PATCH and DELETE only archives, so the message says
``fix in SQL``.

**FK target.** ``instruments`` is the target of
``fk_investment_transactions_instrument_id_instruments``; the batch rebuild keeps
``uq_instruments_id_user`` and is safe with ``foreign_keys=OFF`` (ADR-0001 rule 4b).

**SQLite batch hazard** (same as 0029 / 0037 / 0044): the partial index
``uq_instruments_active_user_symbol_currency`` is dropped before the batch and recreated after, in
both directions. On SQLAlchemy 2.1.8 / Alembic 1.20.1 the batch copy keeps the predicate
(mutation run 2026-11-16: drop and re-create omitted,
``test_instruments_partial_index_where_clause_preserved`` and
``test_asset_class_currency_migration_adds_the_checks_and_keeps_rows`` green), so the drop and
re-create is defensive, kept as precedent.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0045_asset_class_currency"
down_revision: str | Sequence[str] | None = "0044_pre_link_category"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen literals, never imported from the model.
_USD_ONLY = "asset_class NOT IN ('us_equity', 'us_etf') OR currency = 'USD'"
_INR_ONLY = "asset_class NOT IN ('indian_equity', 'indian_mf', 'nps') OR currency = 'INR'"
_ACTIVE = "archived_at IS NULL"


def _preflight() -> None:
    bind = op.get_bind()
    rules = (
        ("a USD-only asset class (us_equity, us_etf)", "USD", "usd_only_asset_class", _USD_ONLY),
        (
            "an INR-only asset class (indian_equity, indian_mf, nps)",
            "INR",
            "inr_only_asset_class",
            _INR_ONLY,
        ),
    )
    lines: list[str] = []
    for what, currency, name, clause in rules:
        sql = f"SELECT count(*) FROM instruments WHERE NOT ({clause})"
        n = bind.execute(sa.text(sql)).scalar_one()
        if n:
            lines.append(
                f"{n} instrument row(s) of {what} are not {currency}, which blocks CHECK {name}"
            )
    if lines:
        raise RuntimeError(
            "asset-class currency CHECKs (PRD §F7) cannot be added; fix in SQL: set currency, "
            "or delete the row and its investment_transactions:\n" + "\n".join(lines)
        )


def _drop_partial_index() -> None:
    op.drop_index("uq_instruments_active_user_symbol_currency", table_name="instruments")


def _create_partial_index() -> None:
    op.create_index(
        "uq_instruments_active_user_symbol_currency",
        "instruments",
        ["user_id", "symbol", "currency"],
        unique=True,
        sqlite_where=sa.text(_ACTIVE),
        postgresql_where=sa.text(_ACTIVE),
    )


def upgrade() -> None:
    _preflight()
    _drop_partial_index()
    with op.batch_alter_table("instruments") as batch_op:
        batch_op.create_check_constraint("usd_only_asset_class", _USD_ONLY)
        batch_op.create_check_constraint("inr_only_asset_class", _INR_ONLY)
    _create_partial_index()


def downgrade() -> None:
    _drop_partial_index()
    with op.batch_alter_table("instruments") as batch_op:
        batch_op.drop_constraint("usd_only_asset_class", type_="check")
        batch_op.drop_constraint("inr_only_asset_class", type_="check")
    _create_partial_index()
