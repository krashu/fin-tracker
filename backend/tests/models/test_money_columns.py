"""ADR-0001 rule 1: money is ``BigInteger``.

``Integer`` is 64-bit on SQLite whatever you declare and 32-bit on Postgres, where it overflows
at ₹21,474,836 — so an ``Integer`` money column passes every SQLite test and breaks on v2. This
walks the model metadata rather than a hand-kept list, so a new ``*_paise`` column is covered the
moment it is declared.
"""

from __future__ import annotations

from sqlalchemy import BigInteger

from app.models import Base


def test_every_paise_column_is_biginteger() -> None:
    money = [
        (table.name, column.name, column.type)
        for table in Base.metadata.sorted_tables
        for column in table.columns
        if column.name.endswith("_paise")
    ]
    assert money, "no *_paise columns found — the scan is broken, not the schema"

    wrong = [
        f"{t}.{c}: {type(ct).__name__}" for t, c, ct in money if not isinstance(ct, BigInteger)
    ]
    assert wrong == [], f"money columns must be BigInteger (ADR-0001 rule 1): {wrong}"
