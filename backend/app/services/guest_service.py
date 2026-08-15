"""Guest service — ephemeral demo sandboxes & lifecycle pruning.

Implements multi-visitor demo isolation (ADR-0003) and 6-phase topological
teardown (ADR-0001) for ephemeral guest accounts.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.core import clock
from app.core.log_config import get_logger
from app.models import (
    Account,
    Category,
    ImportBatch,
    Instrument,
    InvestmentTransaction,
    Label,
    MerchantAlias,
    MerchantLabelMap,
    MerchantTagMap,
    RefreshSession,
    Transaction,
    TransactionLabel,
    User,
)
from app.services import auth_service
from app.services.demo_seed import seed_demo_data
from app.services.provisioning import (
    provision_default_categories,
    provision_seed_merchant_dictionary,
)

logger = get_logger(__name__)

DEFAULT_GUEST_TTL_HOURS = 2


def create_guest_sandbox(
    session: Session, ttl_hours: int = DEFAULT_GUEST_TTL_HOURS
) -> tuple[User, str]:
    """Provision a completely isolated guest user with seed data and a refresh session. Commits.

    Omits argon2 password hashing (guests authenticate via direct cookie issuance),
    dropping provisioning latency to < 30ms and preventing CPU exhaustion DOS attacks.
    """
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
    """Deterministically tear down a guest sandbox via 6-phase topological deletion. Commits.

    Respects strict foreign key constraints (SQLite PRAGMA foreign_keys=ON / Postgres)
    by breaking cyclic/self-referential FKs before deleting child tables and root entities.
    """
    # Phase 1: Break cyclic / self-referential foreign keys
    session.execute(
        update(Transaction).where(Transaction.user_id == user_id).values(transfer_pair_id=None)
    )
    session.execute(
        update(InvestmentTransaction)
        .where(InvestmentTransaction.user_id == user_id)
        .values(pair_id=None)
    )
    session.execute(
        update(Account).where(Account.user_id == user_id).values(parent_account_id=None)
    )

    # Phase 2: Delete join / secondary mapping tables (leaves)
    session.execute(delete(TransactionLabel).where(TransactionLabel.user_id == user_id))
    session.execute(delete(MerchantLabelMap).where(MerchantLabelMap.user_id == user_id))
    session.execute(delete(MerchantTagMap).where(MerchantTagMap.user_id == user_id))
    session.execute(delete(MerchantAlias).where(MerchantAlias.user_id == user_id))

    # Phase 3: Delete transactional records & import batches
    session.execute(delete(Transaction).where(Transaction.user_id == user_id))
    session.execute(delete(InvestmentTransaction).where(InvestmentTransaction.user_id == user_id))
    session.execute(delete(ImportBatch).where(ImportBatch.user_id == user_id))

    # Phase 4: Delete primary reference entities
    session.execute(delete(Label).where(Label.user_id == user_id))
    session.execute(delete(Instrument).where(Instrument.user_id == user_id))
    session.execute(delete(Account).where(Account.user_id == user_id))

    # Phase 5: Delete category taxonomy (subcategories first, root parents second)
    session.execute(
        delete(Category).where(Category.user_id == user_id, Category.parent_id.is_not(None))
    )
    session.execute(
        delete(Category).where(Category.user_id == user_id, Category.parent_id.is_(None))
    )

    # Phase 6: Delete auth sessions and the guest user record
    session.execute(delete(RefreshSession).where(RefreshSession.user_id == user_id))
    session.execute(delete(User).where(User.id == user_id, User.is_guest.is_(True)))

    session.commit()
    logger.info("guest_sandbox_deleted", user_id=str(user_id))


def cleanup_expired_guests(session: Session, batch_size: int = 25) -> int:
    """Find and delete expired guest accounts in bounded batches. Returns count cleaned."""
    now = clock.naive_utcnow()
    expired_ids = session.scalars(
        select(User.id)
        .where(User.is_guest.is_(True), User.guest_expires_at <= now)
        .limit(batch_size)
    ).all()

    cleaned = 0
    for uid in expired_ids:
        try:
            delete_guest_sandbox(session, uid)
            cleaned += 1
        except Exception:
            session.rollback()
            logger.exception("guest_cleanup_failed", user_id=str(uid))

    if cleaned > 0:
        logger.info("expired_guests_cleaned", count=cleaned)
    return cleaned
