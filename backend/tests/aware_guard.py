"""Refuse a flush that would write an aware ``datetime`` to a ``DateTime`` column.

ADR-0001 rule 5b: only naive UTC reaches a column. SQLite strips the offset on the way in, so
once a value has been through the DB aware and naive are indistinguishable — the only place
the difference is observable is the in-memory value at flush time, which is where this looks.

Installed on a ``sessionmaker`` (``tests/api/conftest.py``'s ``session_factory``, so every
route write in the API suite passes through it) or on a single ``Session``
(``tests/services/test_datetime_boundary.py``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, event, inspect
from sqlalchemy.orm import Session, sessionmaker


def aware_column_writes(session: Session) -> list[str]:
    """``Model.column`` for every pending new/dirty value that is an aware ``datetime``.

    Reads only the loaded state (``InstanceState.dict``), so it never triggers a lazy load
    mid-flush; a value loaded from the DB is naive and passes.
    """
    offenders = []
    for obj in session.new | session.dirty:
        state = inspect(obj)
        for attr in state.mapper.column_attrs:
            if not any(isinstance(col.type, DateTime) for col in attr.columns):
                continue
            value = state.dict.get(attr.key)
            if isinstance(value, datetime) and value.tzinfo is not None:
                offenders.append(f"{type(obj).__name__}.{attr.key}")
    return sorted(offenders)


def _refuse_aware_writes(session: Session, _flush_context: Any, _instances: Any) -> None:
    offenders = aware_column_writes(session)
    assert offenders == [], (
        f"aware datetime written to {offenders} — write clock.naive_utcnow() (ADR-0001 rule 5b)"
    )


def install_aware_write_guard(target: Session | sessionmaker[Session]) -> None:
    """Make every flush on ``target`` fail if it carries an aware ``DateTime`` value."""
    event.listen(target, "before_flush", _refuse_aware_writes)
