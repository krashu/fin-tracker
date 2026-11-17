"""The one builder of :class:`~app.schemas.TransactionRead` rows.

``TransactionRead`` carries fields no ORM ``Transaction`` can supply: the category names
(resolved without the ``archived_at`` filter, ADR-0012) and ``card_bill_link_offered``
(ADR-0002 rule 11b). Both are batched reads, so they are computed once per response here, and
every route that returns the schema goes through :func:`transaction_reads`.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy.orm import Session

from app.models import Transaction
from app.schemas import TransactionRead
from app.services.category_service import resolve_category_labels
from app.services.reconciliation_service import card_bill_link_offered_ids

# Every field the ORM row supplies as an attribute; the rest come from the batched reads.
_ORM_FIELDS = TransactionRead.model_fields.keys() - {
    "category_name",
    "category_parent_name",
    "card_bill_link_offered",
}


def transaction_reads(
    session: Session, txns: Sequence[Transaction], *, user_id: UUID
) -> list[TransactionRead]:
    """Project ORM rows onto the wire schema, naming each row's category and marking the rows
    that offer the manual card-bill link.

    The category names cannot be read off the ORM object: ``Transaction`` has no ``category``
    relationship (``labels`` is its only one), and ADR-0012 keeps ORM cascades off
    ``categories`` after a ``delete-orphan`` there turned a reparent into silent row deletion.
    They arrive from one batched query that deliberately ignores ``archived_at``
    (:func:`~app.services.category_service.resolve_category_labels`), which lets a transaction
    on an archived category still render its real name instead of "Uncategorized". A miss
    means the id is not owned by this user, so it is left unnamed rather than reaching for a
    name across the tenant boundary.
    """
    names = resolve_category_labels(
        session,
        category_ids=[t.category_id for t in txns if t.category_id is not None],
        user_id=user_id,
    )
    offered = card_bill_link_offered_ids(session, user_id=user_id, txns=txns)
    reads: list[TransactionRead] = []
    for t in txns:
        category_name, category_parent_name = names.get(t.category_id, (None, None))
        reads.append(
            TransactionRead.model_validate(
                {
                    **{f: getattr(t, f) for f in _ORM_FIELDS},
                    "category_name": category_name,
                    "category_parent_name": category_parent_name,
                    "card_bill_link_offered": t.id in offered,
                },
                from_attributes=True,
            )
        )
    return reads
