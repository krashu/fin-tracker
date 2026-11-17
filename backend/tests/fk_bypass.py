"""Store a cross-user row the composite same-user FKs refuse (ADR-0003 rule 10).

For read-side isolation tests only. ADR-0003 rule 7 says every read restates ``user_id`` even
though rule 10 makes a cross-user link unstorable: the two are independent defences, so the rule-7
tests still need the corrupt row to exist. This turns SQLite's FK enforcement off on the test's
single StaticPool connection for the duration of the block.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.orm import Session


def _set_foreign_keys(session: Session, on: bool) -> None:
    # PRAGMA foreign_keys is a no-op inside an open transaction, so the caller's pending work is
    # committed first and the setting read back, never assumed.
    session.commit()
    session.execute(text(f"PRAGMA foreign_keys={'ON' if on else 'OFF'}"))
    assert session.scalar(text("PRAGMA foreign_keys")) == int(on), "PRAGMA ignored mid-transaction"


@contextmanager
def foreign_keys_off(session: Session) -> Iterator[None]:
    """Commit, disable FK checks, run the block, commit it, re-enable FK checks."""
    _set_foreign_keys(session, on=False)
    try:
        yield
    finally:
        _set_foreign_keys(session, on=True)
