"""The one delete for ``transactions`` rows (ADR-0002 rule 5, ADR-0009 rule 13).

Four callers share it: ``DELETE /transactions/{id}``, ``POST /transactions/bulk-delete``, the
review-queue discard (``POST /imports/{batch_id}/commit``) and the demo reseed
(``demo_seed._reset_demo_transactions``). Import cancel and user teardown
deliberately do not call it (the ADR-0002 rule 5 exemptions): import cancel deletes only pending
rows, which nothing can point at, and teardown deletes all of a user's rows in one statement.
"""

from __future__ import annotations

from collections.abc import Collection
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.models import InvestmentTransaction, PeerEntry, Transaction
from app.services.reconciliation_service import release_leg


def delete_owned_transactions(session: Session, *, user_id: UUID, ids: Collection[int]) -> None:
    """Hard-delete the user's rows ``ids``; the caller has checked ownership and owns the commit.

    Hard, not soft: the only references to ``transactions.id`` are the two self-FKs
    (``transfer_pair_id``, ``refund_of_id``, both ON DELETE NO ACTION) and ``transaction_labels``
    (CASCADE, so labels go with the row). Four steps:

    1. Null ``refund_of_id`` on every refund pointing into the set (ADR-0009 rule 13); a refund
       outside the set survives unlinked. Nulling a refund that is itself deleted is harmless.
    2. Release each partner outside the set with :func:`release_leg` (ADR-0002 rules 5 and 9): its
       pair is nulled and its pre-link type and category come back.
    3. Flush. Sessions run ``autoflush=False``, so without this the released partner still points
       at a row about to be deleted and the DELETE fails on the foreign key.
    4. Delete the whole set in ONE statement. NO ACTION foreign keys are checked at the end of the
       statement, so both legs of a pair, or a refund with its original, go together and neither
       is released.

    No ``merchant_tag_map`` write. Per PRD §F3 ``hit_count`` is a monotonic positive signal; PATCH
    writes to the map because it expresses a new positive decision about the merchant→category
    binding, while a delete expresses no decision (the user is removing a row, not retracting
    their judgement about what the merchant means). Not symmetric ops on the same signal.

    An empty ``ids`` renders ``IN ()`` as false, so the call does nothing.
    """
    session.execute(
        update(PeerEntry)
        .where(PeerEntry.user_id == user_id, PeerEntry.transaction_id.in_(ids))
        .values(transaction_id=None)
    )
    session.execute(
        update(InvestmentTransaction)
        .where(
            InvestmentTransaction.user_id == user_id,
            InvestmentTransaction.linked_spend_txn_id.in_(ids),
        )
        .values(linked_spend_txn_id=None)
    )
    session.execute(
        update(Transaction)
        .where(Transaction.user_id == user_id, Transaction.refund_of_id.in_(ids))
        .values(refund_of_id=None)
    )
    partners = session.scalars(
        select(Transaction).where(
            Transaction.user_id == user_id,
            Transaction.transfer_pair_id.in_(ids),
            Transaction.id.not_in(ids),
        )
    ).all()
    for partner in partners:
        release_leg(partner)
    session.flush()
    session.execute(
        delete(Transaction).where(Transaction.user_id == user_id, Transaction.id.in_(ids))
    )
