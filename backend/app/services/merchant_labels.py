"""Merchant → label auto-learning (PRD §F3a Phase 2, exact-match).

Sibling to :mod:`app.services.tag_service` (merchant → *category*): that learns a
single winning category per merchant; this learns a *set* of user labels and the
import review queue prefills every one that clears :data:`LABEL_PREFILL_MIN`.

:func:`prefetch_label_map` is called once per import to build
``{merchant_normalized: [label_id, ...]}``, and those labels are written onto the
pending rows (mirroring the ``auto_category_id`` prefill). :func:`record_label` learns
each merchant→label decision. (No caller list: it drifts by construction — the routes
now go through :func:`learn_merchant_memory` rather than calling in directly. ``grep``
is authoritative.)

The eligibility gate is reused wholesale from :mod:`app.services.tag_service`
(:func:`should_learn_tag` — spend/refund + non-empty merchant): label learning
is deliberately spend/refund-only, matching category learning. Labels are
editable on *all* transaction types in the API, so a ``#salary`` on an income row
or ``#rent`` on a transfer **persists and filters but is never learned or
prefilled** — income/transfer are hand-classified and must not feed the
merchant→label suggestion path.
"""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core import clock
from app.core.db_errors import is_unique_violation, savepoint_insert
from app.core.log_config import get_logger
from app.models import Label, MerchantLabelMap
from app.services.tag_service import (
    merchant_map_winner_order,
    record_tag,
    should_learn_tag,
)

logger = get_logger(__name__)

# Prefill bar: a merchant→label pair auto-applies in the review queue only after
# the user has confirmed it on this merchant this many times. This is the single
# source of truth for the "N confirmations = established" threshold —
# ``imports.CONFIDENT_MIN`` (the category confidence "confident" tint) is defined
# as this value, and ``LabelRuleRead.prefill_threshold`` carries it to the client,
# so there is exactly one literal to change. Only strongly-established labels
# prefill, so one-offs stay suppressed (there is no
# decay in v1). Unlike ``prefetch_tag_map``'s single-winner pick, this returns a
# *set* that can only grow; the threshold is the noise cap. A top-N / dominance
# cap is a speculative knob rejected per CLAUDE.md §2 (no second concrete need).
LABEL_PREFILL_MIN: int = 3


def prefetch_label_map(session: Session, *, user_id: UUID) -> dict[str, list[int]]:
    """Return ``{merchant_normalized: [label_id, ...]}`` of prefill-worthy labels.

    One SELECT joining ``merchant_label_map`` → ``labels`` filtered to the given
    user and ``hit_count >= LABEL_PREFILL_MIN``. The JOIN is defence-in-depth: a
    label hard-delete cascades its map rows (composite FK, ON DELETE CASCADE), so
    an orphan should never exist, but the JOIN keeps the promise if a future path
    ever leaves one. Labels are ordered by hit_count so a merchant's strongest
    tags lead its list (cosmetic — the review queue applies the whole set).
    """
    stmt = (
        select(MerchantLabelMap.merchant_normalized, MerchantLabelMap.label_id)
        .join(
            Label,
            (Label.id == MerchantLabelMap.label_id) & (Label.user_id == MerchantLabelMap.user_id),
        )
        .where(
            MerchantLabelMap.user_id == user_id,
            # A pinned label prefills even below the learned bar (F3a authoring) —
            # this WHERE widening is the *only* behavioural change on this path;
            # unlike prefetch_tag_map, the ORDER BY here is cosmetic (see below).
            or_(
                MerchantLabelMap.hit_count >= LABEL_PREFILL_MIN,
                MerchantLabelMap.pinned,
            ),
        )
        # Shared winner ordering (merchant_map_winner_order) for lockstep symmetry
        # with prefetch_tag_map — though here it is cosmetic: prefetch_label_map
        # returns the whole label *set* per merchant, so intra-merchant order
        # doesn't change which labels prefill (the review queue applies them all).
        .order_by(*merchant_map_winner_order(MerchantLabelMap))
    )
    out: dict[str, list[int]] = {}
    # TODO(v2 postgres): SQLite has no window functions in older builds; v1 does
    # the group-by-merchant reduction in Python — trivially cheap for one user.
    for merchant_normalized, label_id in session.execute(stmt):
        out.setdefault(merchant_normalized, []).append(label_id)
    return out


def _is_merchant_label_conflict(orig: BaseException | None) -> bool:
    """True when an ``IntegrityError.orig`` is the
    ``uq_merchant_label_map_user_merchant_label`` uniqueness violation.

    Delegates the dialect-aware matching to
    :func:`app.core.db_errors.is_unique_violation`.
    """
    return is_unique_violation(
        orig,
        index_name="uq_merchant_label_map_user_merchant_label",
        columns=[
            "merchant_label_map.user_id",
            "merchant_label_map.merchant_normalized",
            "merchant_label_map.label_id",
        ],
    )


def record_label(
    session: Session,
    *,
    user_id: UUID,
    merchant_normalized: str,
    label_id: int,
) -> None:
    """Upsert a (user, merchant, label) row: insert or bump hit_count + last_used.

    No-op on empty merchant string (a blank-merchant row shouldn't pollute the
    map). Caller owns the commit.

    Race handling / asymmetric flush mirror ``tag_service.record_tag`` exactly:
    the INSERT runs in a ``begin_nested()`` SAVEPOINT so a concurrent-insert
    ``IntegrityError`` rolls back only the insert (caller-side pending state
    survives), then the winner is refetched and bumped. For the Postgres v2 swap see
    :func:`app.core.db_errors.savepoint_insert` — it is not a single edit, and that
    docstring is the one place scoping it.
    """
    if not merchant_normalized:
        return

    existing = session.scalar(
        select(MerchantLabelMap).where(
            MerchantLabelMap.user_id == user_id,
            MerchantLabelMap.merchant_normalized == merchant_normalized,
            MerchantLabelMap.label_id == label_id,
        )
    )
    if existing is not None:
        existing.hit_count += 1
        existing.last_used = clock.naive_utcnow()
        return

    # savepoint_insert mirrors record_tag: True → inserted; False → the
    # (user, merchant, label) uniqueness violation, rolled back — refetch + bump.
    if not savepoint_insert(
        session,
        MerchantLabelMap(
            user_id=user_id,
            merchant_normalized=merchant_normalized,
            label_id=label_id,
        ),
        is_conflict=_is_merchant_label_conflict,
    ):
        winner = session.scalar(
            select(MerchantLabelMap).where(
                MerchantLabelMap.user_id == user_id,
                MerchantLabelMap.merchant_normalized == merchant_normalized,
                MerchantLabelMap.label_id == label_id,
            )
        )
        if winner is None:
            # The refetch should always find the row the unique constraint just
            # collided with. A miss means the decision is being lost — surface it
            # (PII: never log merchant_normalized; it can carry names).
            logger.warning(
                "record_label_conflict_no_winner",
                user_id=str(user_id),
                label_id=label_id,
            )
            return
        winner.hit_count += 1
        winner.last_used = clock.naive_utcnow()


def learn_merchant_memory(
    session: Session,
    *,
    user_id: UUID,
    merchant_normalized: str,
    transaction_type: str,
    category_id: int | None = None,
    label_ids: Iterable[int] = (),
) -> None:
    """Teach merchant→category (F3) and merchant→label (F3a) from one confirmed
    spend/refund decision, behind the single :func:`should_learn_tag` gate.

    De-dups the gate + loop that ``create_transaction``, ``update_transaction``,
    and the demo seeder each hand-rolled (a divergence that already let one write
    path silently skip label learning). The order of the two writes is not
    load-bearing — ``merchant_tag_map`` and ``merchant_label_map`` are independent
    tables. The caller owns which labels it passes (all vs additions), any
    extra gates (PATCH's confirmed/changed, commit's not-defaulted), and the
    commit.

    Contract: ``label_ids`` must be unique — ``record_label`` bumps ``hit_count``
    once per call, so duplicates would overcount one user action. All current
    callers pass ids derived from :func:`resolve_label_names` or a diff set, both
    already unique.
    """
    if not should_learn_tag(
        transaction_type=transaction_type, merchant_normalized=merchant_normalized
    ):
        return
    if category_id is not None:
        record_tag(
            session,
            user_id=user_id,
            merchant_normalized=merchant_normalized,
            category_id=category_id,
        )
    for label_id in label_ids:
        record_label(
            session,
            user_id=user_id,
            merchant_normalized=merchant_normalized,
            label_id=label_id,
        )


def pin_label(
    session: Session,
    *,
    user_id: UUID,
    merchant_normalized: str,
    label_id: int,
) -> MerchantLabelMap:
    """Pin merchant→label: this label auto-applies for the merchant, user-authored.

    Upserts the ``(user, merchant, label)`` row with ``pinned=True``. No sibling
    un-pin — labels are a *set*, so a merchant can carry several pinned labels
    (unlike the single pinned category in :func:`app.services.tag_service.pin_tag`).

    Deliberately unlike :func:`record_label` (the *learning* path): sets
    ``pinned=True``, does **NOT** touch ``hit_count`` / ``last_used`` on an
    existing row (a pin is an assertion, not an observed decision — un-pinning must
    revert to the untouched learned ranking; a fresh row keeps the default
    ``hit_count=1``), and **fails loudly** on an insert-race refetch miss rather
    than the tolerated log-and-return.

    Precondition: ``merchant_normalized`` non-empty (endpoint 422s on blank).
    Caller owns the commit. Returns the pinned row.
    """
    existing = session.scalar(
        select(MerchantLabelMap).where(
            MerchantLabelMap.user_id == user_id,
            MerchantLabelMap.merchant_normalized == merchant_normalized,
            MerchantLabelMap.label_id == label_id,
        )
    )
    if existing is not None:
        existing.pinned = True
        return existing

    row = MerchantLabelMap(
        user_id=user_id,
        merchant_normalized=merchant_normalized,
        label_id=label_id,
        pinned=True,
    )
    if not savepoint_insert(session, row, is_conflict=_is_merchant_label_conflict):
        # Concurrent-insert race: the row now exists — refetch and pin it.
        refetched = session.scalar(
            select(MerchantLabelMap).where(
                MerchantLabelMap.user_id == user_id,
                MerchantLabelMap.merchant_normalized == merchant_normalized,
                MerchantLabelMap.label_id == label_id,
            )
        )
        if refetched is None:
            # Impossible: the unique constraint just proved the row exists. An
            # explicit pin must not "succeed" having created nothing.
            raise RuntimeError("pin_label: winner refetch missed after unique conflict")
        refetched.pinned = True
        return refetched
    return row


def set_label_pinned(
    session: Session,
    *,
    user_id: UUID,
    map_id: int,
    pinned: bool,
) -> MerchantLabelMap | None:
    """Toggle ``pinned`` on an existing merchant→label row (PATCH).

    No sibling handling (labels are a set). Never touches ``hit_count`` /
    ``last_used`` — un-pinning reverts to the learned ranking. Returns ``None``
    when the row is absent or not the caller's (endpoint 404s). Caller owns commit.
    """
    row = session.scalar(
        select(MerchantLabelMap).where(
            MerchantLabelMap.id == map_id,
            MerchantLabelMap.user_id == user_id,
        )
    )
    if row is None:
        return None
    row.pinned = pinned
    return row
