"""ADR-0003 rule 11: every owned table carries ``user_id`` and the user teardown empties it.

The owned set is derived from ``Base.metadata``, not a hand-kept list, so a new table is covered
the moment it is declared: it either carries ``user_id`` or is added to ``GLOBAL_TABLES`` (an ADR
decision, not a test edit), and the teardown test refuses to pass until the guest owns a row in it.
It exercises ``teardown_user`` through guest expiry; account deletion runs the same function.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import User
from app.services.guest_service import create_guest_sandbox, delete_guest_sandbox
from tests.owned_tables import owned_tables, rows_owned_by, seed_rows_demo_seed_lacks


def test_every_non_global_table_has_user_id() -> None:
    missing = [t.name for t in owned_tables() if "user_id" not in t.c]
    assert missing == [], f"owned tables without user_id (ADR-0003 rule 11): {missing}"


def test_every_owned_table_is_torn_down(session: Session) -> None:
    guest, _ = create_guest_sandbox(session, ttl_hours=2)
    seed_rows_demo_seed_lacks(session, guest.id)
    session.commit()

    # The precondition is the point: a table the guest owns no row in proves nothing about
    # teardown. A new owned table lands here first — seed a row for it in the shared helper.
    unseeded = [t.name for t in owned_tables() if rows_owned_by(session, t, guest.id) == 0]
    assert unseeded == [], f"guest owns no rows in {unseeded}; seed them so teardown is tested"

    delete_guest_sandbox(session, guest.id)

    leftover = {t.name: n for t in owned_tables() if (n := rows_owned_by(session, t, guest.id)) > 0}
    assert leftover == {}, f"guest teardown left rows behind: {leftover}"
    assert session.get(User, guest.id) is None
