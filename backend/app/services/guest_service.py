"""Guest service — ephemeral demo sandboxes & lifecycle pruning.

Implements ephemeral guest sandboxes (ADR-0003 rule 15). Their teardown is
``user_teardown.teardown_user`` (shared with account deletion), behind the guest-only guard
in ``delete_guest_sandbox``.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core import clock
from app.core.config import get_settings
from app.core.log_config import get_logger
from app.models import User
from app.services import auth_service
from app.services.demo_seed import seed_demo_data
from app.services.provisioning import (
    provision_default_categories,
    provision_seed_merchant_dictionary,
)
from app.services.user_teardown import teardown_user

logger = get_logger(__name__)

DEFAULT_GUEST_TTL_HOURS = 2


class GuestCapReachedError(Exception):
    """Raised when active guest accounts reach max_guest_accounts."""


def create_guest_sandbox(
    session: Session, ttl_hours: int = DEFAULT_GUEST_TTL_HOURS
) -> tuple[User, str]:
    """Provision a completely isolated guest user with seed data and a refresh session. Commits.

    Omits argon2 password hashing (guests authenticate via direct cookie issuance),
    dropping provisioning latency to < 30ms and preventing CPU exhaustion DOS attacks.
    """
    # Live guests only (ADR-0003 rule 15): an expired guest the sweeper has not
    # pruned yet holds no seat.
    active_count = (
        session.scalar(
            select(func.count())
            .select_from(User)
            .where(User.is_guest.is_(True), User.guest_expires_at > clock.naive_utcnow())
        )
        or 0
    )
    if active_count >= get_settings().max_guest_accounts:
        raise GuestCapReachedError("Guest account limit reached")

    guest_id = uuid4()
    now = clock.naive_utcnow()
    expires_at = now + timedelta(hours=ttl_hours)

    user = User(
        id=guest_id,
        email=None,
        password_hash=None,
        display_name="Demo Guest",
        is_guest=True,
        guest_expires_at=expires_at,
    )
    session.add(user)
    session.flush()

    # Provision standard category tree and seed merchant dictionary
    provision_default_categories(session, user.id)
    session.flush()
    provision_seed_merchant_dictionary(session, user.id)

    # Seed demo accounts, transactions, instruments, investment transactions
    seed_demo_data(session, user_id=user.id)

    # Issue refresh session family
    refresh_token = auth_service.start_session(session, user.id)
    session.commit()
    session.refresh(user)

    logger.info("guest_sandbox_created", user_id=str(user.id), expires_at=expires_at.isoformat())
    return user, refresh_token


def delete_guest_sandbox(session: Session, user_id: UUID) -> None:
    """Tear down a guest sandbox via ``teardown_user``. Commits.

    The guard lives here, not in the shared teardown: only a guest may be deleted this way.
    """
    # Guard: verify the target is actually an existing guest BEFORE any destructive operation.
    user = session.get(User, user_id)
    if user is None or not user.is_guest:
        logger.warning("delete_guest_sandbox_skipped_non_guest", user_id=str(user_id))
        return

    teardown_user(session, user_id)
    session.commit()
    logger.info("guest_sandbox_deleted", user_id=str(user_id))


def cleanup_expired_guests(session: Session, batch_size: int = 25) -> int:
    """Find and delete expired guest accounts in bounded batches until drained.

    Returns count cleaned.
    """
    now = clock.naive_utcnow()
    total_cleaned = 0
    while True:
        expired_ids = session.scalars(
            select(User.id)
            .where(User.is_guest.is_(True), User.guest_expires_at <= now)
            .limit(batch_size)
        ).all()
        if not expired_ids:
            break

        batch_cleaned = 0
        for uid in expired_ids:
            try:
                delete_guest_sandbox(session, uid)
                batch_cleaned += 1
            except Exception:
                session.rollback()
                logger.exception("guest_cleanup_failed", user_id=str(uid))
                # Push the poisoned row's expiry forward so it doesn't wedge the queue.
                # It will be retried in a future cycle window.
                try:
                    session.execute(
                        update(User)
                        .where(User.id == uid)
                        .values(guest_expires_at=now + timedelta(hours=1))
                    )
                    session.commit()
                except Exception:
                    session.rollback()

        total_cleaned += batch_cleaned
        if batch_cleaned == 0:
            break  # Every row in this batch failed — break to avoid infinite loop

    if total_cleaned > 0:
        logger.info("expired_guests_cleaned", count=total_cleaned)
    return total_cleaned
