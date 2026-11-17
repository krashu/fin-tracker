"""ADR-0003 rule 10: every owned→owned FK is a composite same-user FK.

A plain ``(fk) → (id)`` FK lets a row point at another user's row, so the router's 422 check
(rule 9) is the only thing keeping tenants apart. The composite ``(fk, user_id) → (id, user_id)``
makes that link unstorable. Derived from ``Base.metadata`` like ``test_owned_tables``, so a new FK
is covered the moment it is declared.
"""

from __future__ import annotations

from sqlalchemy import Table, UniqueConstraint

from app.models import Base
from tests.owned_tables import GLOBAL_TABLES, TENANT_ROOT


def _is_owned(table: Table) -> bool:
    return table.name not in GLOBAL_TABLES and table.name != TENANT_ROOT


def _has_unique_on(table: Table, cols: set[str]) -> bool:
    unique_sets = [{c.name for c in ix.columns} for ix in table.indexes if ix.unique]
    unique_sets += [
        {c.name for c in uq.columns} for uq in table.constraints if isinstance(uq, UniqueConstraint)
    ]
    return cols in unique_sets


def test_every_owned_fk_is_composite_same_user() -> None:
    offenders: list[str] = []
    for table in Base.metadata.sorted_tables:
        if not _is_owned(table):
            continue
        for fk in table.foreign_key_constraints:
            target = fk.referred_table
            if not _is_owned(target):
                continue
            pairs = {el.parent.name: el.column.name for el in fk.elements}
            label = f"{table.name}({', '.join(fk.column_keys)}) → {target.name}"
            if pairs.get("user_id") != "user_id" or len(pairs) != 2:
                offenders.append(f"{label}: not (fk, user_id) → (id, user_id)")
            elif not _has_unique_on(target, set(pairs.values())):
                offenders.append(f"{label}: no unique (id, user_id) on {target.name}")
    assert offenders == [], "plain owned→owned FKs (ADR-0003 rule 10):\n" + "\n".join(offenders)
