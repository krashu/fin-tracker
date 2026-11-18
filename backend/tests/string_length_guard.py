"""Refuse a flush that would write an over-length string to a length-capped String column.

ADR-0001: SQLite silently ignores String(N) length constraints, allowing writes that exceed
column limits to succeed locally on SQLite but fail with a 500 on Postgres. This guard catches
length overflows at flush time.

Installed on a ``sessionmaker`` (``tests/api/conftest.py``'s ``session_factory``, so every
route write in the API suite passes through it) or on a single ``Session``.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import String, event, inspect
from sqlalchemy.orm import Session, sessionmaker


def string_overflow_column_writes(session: Session) -> list[str]:
    """``Model.column (len X > Y)`` for every pending new/dirty value exceeding String(length).

    Reads only the loaded state (``InstanceState.dict``), so it never triggers a lazy load
    mid-flush.
    """
    offenders = []
    for obj in session.new | session.dirty:
        state = inspect(obj)
        for attr in state.mapper.column_attrs:
            for col in attr.columns:
                if not isinstance(col.type, String) or col.type.length is None:
                    continue
                value = state.dict.get(attr.key)
                if isinstance(value, str) and len(value) > col.type.length:
                    offenders.append(
                        f"{type(obj).__name__}.{attr.key} (len {len(value)} > {col.type.length})"
                    )
    return sorted(offenders)


def _refuse_string_overflow_writes(session: Session, _flush_context: Any, _instances: Any) -> None:
    offenders = string_overflow_column_writes(session)
    assert offenders == [], (
        f"string length overflow written to {offenders} — stay within String(N) capacity (ADR-0001)"
    )


def install_string_length_guard(target: Session | sessionmaker[Session]) -> None:
    """Make every flush on ``target`` fail if it carries an over-length String value."""
    event.listen(target, "before_flush", _refuse_string_overflow_writes)
