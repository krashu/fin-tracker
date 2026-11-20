"""Bulk categorisation of board rows — the shared write behind three gestures.

``POST /transactions/bulk-categorise``, "apply to past rows" (``GET
/transactions/{id}/past-rows`` then the bulk POST) and "archive with reassign"
(``DELETE /categories/{id}?reassign_to=``) all move many board rows onto one
category. ADR-0004 rule 4: a bulk gesture teaches **once per canonical merchant**,
not once per row — one user decision, one ``hit_count`` bump — so the three routes
share this one function rather than each re-deriving the rule.

Lives here, not in :mod:`app.services.tag_service`: ``merchant_labels`` imports
``tag_service``, so ``tag_service`` cannot call ``learn_merchant_memory`` (ADR-0004
rule 1's single gate) without an import cycle.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Transaction
from app.services.category_service import fallback_category_id, kind_for_type
from app.services.merchant_alias import AliasResolver, is_unknown_via
from app.services.merchant_labels import learn_merchant_memory
from app.services.tag_service import AUTO_TAGGABLE_TYPES
from app.services.transaction_queries import confirmed_only

# Ids per bulk request (bulk-categorise and bulk-delete) — matches the list endpoint's
# ``limit`` ceiling.
BULK_MAX = 500

# "Recent" for the "apply to past rows" offer: only the last year of history.
PAST_ROWS_WINDOW_DAYS = 365


@dataclass(frozen=True, slots=True)
class BulkOutcome:
    updated: int  # rows whose category actually changed
    taught: int  # distinct canonical merchants taught (gateway handles never are)


def bulk_categorise(
    session: Session,
    *,
    user_id: UUID,
    rows: Sequence[Transaction],
    category_id: int,
    resolver: AliasResolver,
) -> BulkOutcome:
    """Move ``rows`` onto ``category_id`` and teach each changed merchant once.

    Precondition: the caller has validated that every row is an owned **board** row
    of a kind the category accepts, and that the category is live and owned. Rows
    already on the category are skipped — a re-send is not a new decision (ADR-0004
    rule 2b). ``auto_category_id`` (the machine's suggestion) is never touched.
    A categorisation is the user's newer choice, so it also discards a category a link set
    aside on a linked leg (ADR-0002 rule 9; the DB CHECK requires it). A linked leg reads
    as uncategorised, so "apply to past rows" may offer one and discard its saved category:
    consistent with "the newer choice wins".
    Only ``AUTO_TAGGABLE_TYPES`` rows teach, and ``taught`` counts the merchants the gate
    in ``learn_merchant_memory`` accepted (a gateway handle is never taught, ADR-0011
    rule 12); the filter here only picks the representative row per canonical. The caller
    owns the commit.
    """
    updated = 0
    by_canonical: dict[str, Transaction] = {}
    for row in rows:
        if row.category_id == category_id:
            continue
        row.category_id = category_id
        row.pre_link_category_id = None
        updated += 1
        if row.transaction_type not in AUTO_TAGGABLE_TYPES:
            continue
        by_canonical.setdefault(resolver.canonical(row.merchant_normalized), row)

    taught = 0
    for canonical in sorted(by_canonical):
        rep = by_canonical[canonical]
        taught += learn_merchant_memory(
            session,
            user_id=user_id,
            resolver=resolver,
            merchant_normalized=rep.merchant_normalized,
            transaction_type=rep.transaction_type,
            category_id=category_id,
        )
    return BulkOutcome(updated=updated, taught=taught)


def past_rows_like(
    session: Session,
    *,
    user_id: UUID,
    source: Transaction,
    resolver: AliasResolver,
    since: date,
    limit: int = BULK_MAX,
) -> list[Transaction]:
    """Board rows to offer after the user categorised ``source`` ("apply to past rows").

    Same canonical merchant and ``transaction_type`` as ``source``, dated on or after
    ``since``, newest first, at most ``limit``. Only rows still on a
    **machine-assigned** category are offered — uncategorised, the kind's fallback
    "Other", or the row's own ``auto_category_id`` — so a row the user deliberately
    put elsewhere is never overwritten. Rows already on ``source``'s category are
    excluded (nothing to change). A pending or uncategorised ``source`` offers
    nothing: only a board row's categorisation is a user decision (ADR-0004 rule 3).
    """
    if source.confirmed_at is None or source.category_id is None:
        return []
    canonical = resolver.canonical(source.merchant_normalized)
    if not canonical or is_unknown_via(canonical):
        return []
    fallback_id = fallback_category_id(
        session, user_id=user_id, kind=kind_for_type(source.transaction_type)
    )
    candidate_conditions = [
        Transaction.merchant_normalized == canonical,
        Transaction.merchant_normalized == source.merchant_normalized,
    ]
    for p_tokens in resolver.patterns_for(canonical):
        if p_tokens:
            candidate_conditions.append(Transaction.merchant_normalized.contains(p_tokens[0]))

    stmt = confirmed_only(
        select(Transaction).where(
            Transaction.user_id == user_id,
            Transaction.id != source.id,
            Transaction.transaction_type == source.transaction_type,
            Transaction.merchant_normalized != "",
            Transaction.date >= since,
            or_(*candidate_conditions),
            or_(
                Transaction.category_id.is_(None),
                Transaction.category_id == fallback_id,
                Transaction.category_id == Transaction.auto_category_id,
            ),
            or_(
                Transaction.category_id.is_(None),
                Transaction.category_id != source.category_id,
            ),
        )
    ).order_by(Transaction.date.desc(), Transaction.id.desc())
    matches: list[Transaction] = []
    for txn in session.scalars(stmt):
        if resolver.canonical(txn.merchant_normalized) == canonical:
            matches.append(txn)
            if len(matches) == limit:
                break
    return matches
