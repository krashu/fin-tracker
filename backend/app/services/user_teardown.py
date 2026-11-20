"""Hard-delete one user and everything they own (ADR-0003 rules 11 and 13b).

Shared by guest sandbox expiry (``guest_service``) and self-service account deletion
(``auth_service``). It is the only hard delete of categories (ADR-0012 rule 11): subcategories go
before roots, because ``parent_id`` is ``ON DELETE NO ACTION`` (ADR-0012 rule 1).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete, update
from sqlalchemy.orm import Session

from app.models import (
    Account,
    Category,
    ImportBatch,
    Instrument,
    InvestmentAccount,
    InvestmentTransaction,
    Label,
    MerchantAlias,
    MerchantLabelMap,
    MerchantTagMap,
    PeerContact,
    PeerEntry,
    RealisedLot,
    RefreshSession,
    Transaction,
    TransactionLabel,
    User,
)


def teardown_user(session: Session, user_id: UUID) -> None:
    """Hard-delete ``user_id``'s rows in every owned table, then the users row.

    Six topological phases, respecting strict foreign keys (SQLite ``PRAGMA foreign_keys=ON`` /
    Postgres) by breaking cyclic and self-referential FKs before deleting children and roots.
    Executes only and never commits: the caller owns the transaction. One explicit delete per
    owned table; ``tests/models/test_owned_tables.py`` fails when a table is missed.
    """
    # Phase 1: Break cyclic / self-referential foreign keys
    session.execute(
        update(Transaction)
        .where(Transaction.user_id == user_id)
        .values(transfer_pair_id=None, pre_link_category_id=None)
    )
    session.execute(
        update(InvestmentTransaction)
        .where(InvestmentTransaction.user_id == user_id)
        .values(pair_id=None, linked_spend_txn_id=None)
    )
    session.execute(
        update(Account).where(Account.user_id == user_id).values(parent_account_id=None)
    )

    # Phase 2: Delete join / secondary mapping tables (leaves)
    session.execute(delete(TransactionLabel).where(TransactionLabel.user_id == user_id))
    session.execute(delete(MerchantLabelMap).where(MerchantLabelMap.user_id == user_id))
    session.execute(delete(MerchantTagMap).where(MerchantTagMap.user_id == user_id))
    session.execute(delete(MerchantAlias).where(MerchantAlias.user_id == user_id))
    session.execute(delete(RealisedLot).where(RealisedLot.user_id == user_id))
    session.execute(delete(PeerEntry).where(PeerEntry.user_id == user_id))

    # Phase 3: Delete transactional records & import batches
    session.execute(delete(Transaction).where(Transaction.user_id == user_id))
    session.execute(delete(InvestmentTransaction).where(InvestmentTransaction.user_id == user_id))
    session.execute(delete(ImportBatch).where(ImportBatch.user_id == user_id))

    # Phase 4: Delete primary reference entities
    session.execute(delete(Label).where(Label.user_id == user_id))
    session.execute(delete(Instrument).where(Instrument.user_id == user_id))
    session.execute(delete(InvestmentAccount).where(InvestmentAccount.user_id == user_id))
    session.execute(delete(Account).where(Account.user_id == user_id))
    session.execute(delete(PeerContact).where(PeerContact.user_id == user_id))

    # Phase 5: Delete category taxonomy (subcategories first, root parents second)
    session.execute(
        delete(Category).where(Category.user_id == user_id, Category.parent_id.is_not(None))
    )
    session.execute(
        delete(Category).where(Category.user_id == user_id, Category.parent_id.is_(None))
    )

    # Phase 6: Delete auth sessions and the user record
    session.execute(delete(RefreshSession).where(RefreshSession.user_id == user_id))
    session.execute(delete(User).where(User.id == user_id))
