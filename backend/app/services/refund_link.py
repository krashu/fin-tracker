"""The refund → original link rules (ADR-0009 rule 13), shared by every writer of the link.

``PUT /transactions/{id}/refund-link`` and backup restore both link a refund to the spend it
reverses, and must apply the same checks; they live here so the two cannot drift. Ownership
(404 / 422 for another user's row) is the caller's concern — these helpers see only two rows.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Transaction
from app.services.merchant_alias import AliasResolver

NOT_A_REFUND = "only a refund (a positive spend) can link to an original"
BAD_ORIGINAL = "the original must be a confirmed spend with a negative amount"


def is_refund(transaction_type: str, amount_paise: int) -> bool:
    """A refund is a positive ``spend`` (ADR-0009)."""
    return transaction_type == "spend" and amount_paise > 0


def is_refundable_original(transaction_type: str, amount_paise: int) -> bool:
    """What a refund may link to: a negative ``spend`` (ADR-0009 rule 13)."""
    return transaction_type == "spend" and amount_paise < 0


def refund_link_problem(refund: Transaction, original: Transaction) -> str | None:
    """Why ``refund`` cannot link to ``original``, or ``None`` when the link is valid.

    The original must be confirmed, because the review-queue discard deletes pending rows
    only, so a confirmed original cannot disappear under its refund that way. The route checks
    the refund half (:data:`NOT_A_REFUND`) before it loads the original, so that message wins
    over "original not found"; the two messages live here so neither copy can drift.
    """
    if not is_refund(refund.transaction_type, refund.amount_paise):
        return NOT_A_REFUND
    if (
        not is_refundable_original(original.transaction_type, original.amount_paise)
        or original.confirmed_at is None
    ):
        return BAD_ORIGINAL
    return None


# The most rows `GET /transactions/{id}/refund-candidates` returns, and how far back from the
# refund's date an original is looked for.
REFUND_CANDIDATES_LIMIT = 20
REFUND_CANDIDATES_WINDOW_DAYS = 180


def refund_candidates(
    session: Session, *, user_id: UUID, refund: Transaction, resolver: AliasResolver
) -> list[Transaction]:
    """The originals ``refund`` can link to, at most :data:`REFUND_CANDIDATES_LIMIT`.

    One SQL prefilter (same user, confirmed negative ``spend`` dated within
    :data:`REFUND_CANDIDATES_WINDOW_DAYS` before the refund), ranked in Python: the current
    original first (even outside the window), then the same canonical merchant, an equal
    magnitude, the nearest date, the newest id. Each row is vetted by :func:`refund_link_problem`,
    so the list and ``PUT .../refund-link`` agree by construction. A row that is not a refund
    returns nothing.
    """
    if not is_refund(refund.transaction_type, refund.amount_paise):
        return []
    rows = list(
        session.scalars(
            select(Transaction).where(
                Transaction.user_id == user_id,
                Transaction.confirmed_at.is_not(None),
                Transaction.transaction_type == "spend",
                Transaction.amount_paise < 0,
                Transaction.id != refund.id,
                Transaction.date.between(
                    refund.date - timedelta(days=REFUND_CANDIDATES_WINDOW_DAYS), refund.date
                ),
            )
        )
    )
    if refund.refund_of_id is not None and all(r.id != refund.refund_of_id for r in rows):
        current = session.scalar(
            select(Transaction).where(
                Transaction.user_id == user_id, Transaction.id == refund.refund_of_id
            )
        )
        if current is not None:
            rows.append(current)
    merchant = resolver.canonical(refund.merchant_normalized)

    def rank(row: Transaction) -> tuple[bool, bool, bool, int, int]:
        return (
            row.id != refund.refund_of_id,
            resolver.canonical(row.merchant_normalized) != merchant,
            -row.amount_paise != refund.amount_paise,
            abs((row.date - refund.date).days),
            -row.id,
        )

    found = [r for r in sorted(rows, key=rank) if refund_link_problem(refund, r) is None]
    return found[:REFUND_CANDIDATES_LIMIT]
