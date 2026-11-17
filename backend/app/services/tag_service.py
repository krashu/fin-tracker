"""Merchant → category auto-tagging (per PRD §F3, exact-match v1).

:func:`prefetch_tag_map` is called once per import to populate a
``{canonical: category_id}`` dict that the row loop checks with ``dict.get(...)``
against ``resolver.canonical(merchant_normalized)`` — never the raw merchant
string (PRD §F3 / ADR-0011 merchant-alias layer, Phase A2). It mirrors the
``existing_fps`` prefetch in ``import_service`` — one SELECT, O(1) per-row
lookups — which is the non-obvious part worth recording.

A single-row ``lookup_category_id`` variant is intentionally absent —
no concrete caller today needs it (CLAUDE.md §2).

(No caller list here: a hand-maintained one drifts by construction — the previous
version named a module that no longer imports this one, undercounted the importers,
and described a shipped caller in the future tense. ``grep`` is authoritative.)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import UnaryExpression

from app.core import clock
from app.core.db_errors import insert_or_increment, is_unique_violation, savepoint_insert
from app.core.log_config import get_logger
from app.models import Category, MerchantLabelMap, MerchantTagMap, TransactionTypeStr
from app.services.merchant_alias import AliasResolver, is_unknown_via

logger = get_logger(__name__)


def merchant_map_winner_order(
    model: type[MerchantTagMap] | type[MerchantLabelMap],
) -> list[UnaryExpression[Any]]:
    """The per-merchant winner ordering used by the ``/rules`` list
    (:mod:`app.api.v1.rules`) to rank raw ``merchant_tag_map`` /
    ``merchant_label_map`` rows within one ``merchant_normalized`` group.

    ``merchant ASC`` groups; ``pinned DESC`` makes a user-authored row outrank a
    higher-``hit_count`` learned one (F3/F3a authoring); then ``hit_count DESC``,
    ``last_used DESC``, and a final ``id DESC`` deterministic tiebreak
    (``server_default=now()`` is second-precision on SQLite, so ties happen in
    tests and rapid re-tagging).

    As of Phase A2 this is **no longer** what ranks a prefill winner:
    :func:`prefetch_tag_map` and :func:`app.services.merchant_labels.prefetch_label_map`
    both sum raw rows onto one canonical BEFORE ranking (an alias can fold many
    raw merchants into one group), so the winner pick moved to Python —
    :func:`_aggregate_tag_rows` + :func:`merchant_agg_winner_key` express the
    same ``(pinned, hit_count, last_used, id)`` ranking over an aggregated
    group instead of a single row. This function now backs only the raw,
    unaliased ``/rules`` listing (Phase A3 moves that onto the same aggregate).
    A ranking change here must be mirrored in :func:`merchant_agg_winner_key`,
    or the two will silently diverge again — exactly what this function was
    written to prevent.
    """
    return [
        model.merchant_normalized.asc(),
        model.pinned.desc(),
        model.hit_count.desc(),
        model.last_used.desc(),
        model.id.desc(),
    ]


# Transaction types that participate in F3 auto-tagging — both the learning
# signal (every teach: import commit, manual POST/PATCH, bulk) and the
# import auto-suggest (import_service). Income and transfer are hand-classified
# in a separate taxonomy, so a manual income/transfer row with a category must
# NOT feed the spend merchant→category map (it could only ever resurface as a
# cross-taxonomy spend suggestion). This set gates every teach alike.
#
# Refunds are still auto-tagged: a refund is a ``spend`` row with a positive
# amount (ADR-0009), so it is inside this set by construction, and teaching the
# refund's merchant→category pair is exactly what makes §F4a netting work.
# Kept as a named frozenset rather than an `== "spend"` comparison even at one
# element: it is typed against TransactionTypeStr so a future widening of that
# Literal (e.g. adding "adjustment") surfaces in grep / ty when re-evaluating it.
AUTO_TAGGABLE_TYPES: frozenset[TransactionTypeStr] = frozenset({"spend"})


def is_seeded(*, hit_count: int, pinned: bool) -> bool:
    """True for a never-confirmed seed-dictionary row: ``hit_count == 0`` and not pinned.

    One definition for every read site (ADR-0011 rule 11). A pin is user-authored —
    ``pin_tag`` never bumps ``hit_count``, so a pinned seed row sits at zero — and
    must not read as a dictionary suggestion. ``dashboards.tagging_stats`` states the
    same predicate in SQL (``hit_count > 0 OR pinned``).
    """
    return hit_count == 0 and not pinned


def should_learn_tag(*, transaction_type: str, merchant_normalized: str) -> bool:
    """True when a row's type + canonical merchant make it eligible to teach F3.

    The shared eligibility core for every teach, reached through
    :func:`app.services.merchant_labels.learn_merchant_memory` (which passes the
    *canonical*, so a descriptor an alias folds to nothing never teaches): a
    non-empty canonical merchant that is not an unknown-via gateway handle
    (ADR-0011 rule 12) and a
    ``spend`` type (refunds included — they are spend rows carrying a positive
    amount). ``income`` / ``transfer`` are hand-classified and must never feed
    the spend→category map.

    The caller additionally checks ``category_id is not None`` (there is no tag
    to learn without a category, and :func:`record_tag` needs the concrete id),
    plus its own extra guards — PATCH: confirmed + changed; commit:
    not-Other-defaulted.
    """
    return (
        bool(merchant_normalized)
        and transaction_type in AUTO_TAGGABLE_TYPES
        and not is_unknown_via(merchant_normalized)
    )


@dataclass(slots=True)
class _TagAgg:
    """One (canonical, category_id) group's merged merchant_tag_map rows --
    the read-time application of merchant.py's CHANGE HAZARD merge rule (sum
    hit_count, max last_used, OR pinned). Mutable (unlike AliasResolver): built
    once per _aggregate_tag_rows call and updated in place as rows fold in.
    """

    hit_count: int
    last_used: datetime
    pinned: bool
    id: int


def _aggregate_tag_rows(
    session: Session, *, user_id: UUID, resolver: AliasResolver
) -> dict[str, dict[int, _TagAgg]]:
    """Group live merchant_tag_map rows by (canonical, category_id).

    Same SELECT as the pre-alias ``prefetch_tag_map`` (JOIN ``categories``, the
    archived-category filter, both ``user_id`` restated per ADR-0003) — the
    ORDER BY is dropped, since aggregation makes raw row order irrelevant.
    ``resolver.canonical()`` re-keys each raw ``merchant_normalized``; rows
    landing in the same ``(canonical, category_id)`` bucket merge per
    ``merchant.py``'s CHANGE HAZARD rule, applied here at READ time — Stage A
    never rewrites a stored row (decision 10).

    ``Category.user_id`` is restated alongside ``MerchantTagMap.user_id`` even
    though the ``category_id`` came off the user's own map row (ADR-0003
    defence-in-depth) — see :func:`prefetch_tag_map`'s docstring for why this
    one merchant-memory read closes that gap when its siblings don't.

    With ``EMPTY_RESOLVER`` every group has exactly one member (the map's
    UNIQUE constraint), so this reduces to that row's own fields unchanged —
    identical to the pre-alias pick, except keys that resolve to an unknown-via
    canonical, which are skipped (ADR-0011 rule 12); the empty-resolver tests pin it
    rather than trust it.
    """
    stmt = (
        select(
            MerchantTagMap.merchant_normalized,
            MerchantTagMap.category_id,
            MerchantTagMap.hit_count,
            MerchantTagMap.last_used,
            MerchantTagMap.pinned,
            MerchantTagMap.id,
        )
        .join(Category, Category.id == MerchantTagMap.category_id)
        .where(
            MerchantTagMap.user_id == user_id,
            Category.user_id == user_id,
            Category.archived_at.is_(None),
        )
    )
    groups: dict[str, dict[int, _TagAgg]] = {}
    for merchant_normalized, category_id, hit_count, last_used, pinned, row_id in session.execute(
        stmt
    ):
        canonical = resolver.canonical(merchant_normalized)
        if is_unknown_via(canonical):
            continue  # a gateway handle names no merchant (ADR-0011 rule 12)
        by_category = groups.setdefault(canonical, {})
        agg = by_category.get(category_id)
        if agg is None:
            by_category[category_id] = _TagAgg(
                hit_count=hit_count, last_used=last_used, pinned=pinned, id=row_id
            )
        else:
            agg.hit_count += hit_count
            agg.last_used = max(agg.last_used, last_used)
            agg.pinned = agg.pinned or pinned
            agg.id = max(agg.id, row_id)
    return groups


def merchant_agg_winner_key(agg: _TagAgg) -> tuple[bool, int, datetime, int]:
    """The Python half of the ranking ``merchant_map_winner_order`` expresses in
    SQL for the raw, per-row case — the same ``(pinned, hit_count, last_used,
    id)`` ranking, over one aggregated group instead of one row. ``max()`` over
    this tuple picks the winner: Python compares tuples lexicographically and
    every field here is already "bigger wins" (``False < True``, so an
    unpinned group never outranks a pinned one). A test asserts the two
    rankings agree on unaliased data.
    """
    return (agg.pinned, agg.hit_count, agg.last_used, agg.id)


def prefetch_tag_map(session: Session, *, user_id: UUID, resolver: AliasResolver) -> dict[str, int]:
    """Return ``{canonical: category_id}`` of the winning tag per canonical merchant.

    Aggregates every live merchant_tag_map row onto its canonical
    (:func:`_aggregate_tag_rows`), then picks the winning category per
    canonical via :func:`merchant_agg_winner_key` — the archived-category
    filter and the ranking are unchanged from the pre-alias version; only
    WHERE the winner pick happens moved (SQL ORDER BY + first-row-seen ->
    Python aggregate + max).

    ``merchant_tag_map.category_id`` is a composite same-user FK, so the DB
    refuses a cross-user row (ADR-0003 rule 10). The join still restates
    ``Category.user_id`` (rule 7, in ``_aggregate_tag_rows``): this is the one
    merchant-memory read whose result is WRITTEN back, onto imported rows as
    ``auto_category_id``, so it must not depend on the FK alone.

    The archived-category filter is **load-bearing**: ``DELETE /categories/{id}``
    keeps the tag-map rows (archive is a pure ``archived_at`` UPDATE — see
    :mod:`app.api.v1.categories`), so archived categories DO have live tag-map
    rows and this JOIN filter is the sole thing that stops F3 prefilling an
    archived bucket. A user-authored ``pinned`` rule row survives the archive, but
    archive is one-way (ADR-0012), so it never prefills again.

    ``resolver`` is a REQUIRED keyword argument, not defaulted: a default
    would let a production call site read ``prefetch_tag_map(session,
    user_id=x)``, compile, type-check, pass tests, and silently disable
    aliasing entirely. Pass :data:`app.services.merchant_alias.EMPTY_RESOLVER`
    explicitly for unaliased behaviour — identical to the pre-alias output, except
    keys that resolve to an unknown-via canonical, which are skipped (ADR-0011
    rule 12; see :func:`_aggregate_tag_rows`).
    """
    groups = _aggregate_tag_rows(session, user_id=user_id, resolver=resolver)
    return {
        canonical: max(by_category.items(), key=lambda kv: merchant_agg_winner_key(kv[1]))[0]
        for canonical, by_category in groups.items()
    }


def prefetch_tag_strength(
    session: Session, *, user_id: UUID, resolver: AliasResolver
) -> dict[tuple[str, int], tuple[int, bool]]:
    """Return ``{(canonical, category_id): (summed hit_count, any pinned)}``.

    Every ``(canonical, category)`` pair with at least one live
    merchant_tag_map row, not just the per-canonical winner
    :func:`prefetch_tag_map` returns — Phase A3's ``list_candidates`` needs
    each candidate category's OWN strength, not only the winner's. A pair
    ABSENT from this dict means no rule at all; that is distinct from present
    at ``(0, False)``, which is a seeded, never-confirmed row (decision 4,
    arriving in Phase A5) — a ``dict.get`` with a default would collapse that
    distinction, so callers must use ``in`` / a sentinel, never
    ``.get(..., (0, False))``.
    """
    groups = _aggregate_tag_rows(session, user_id=user_id, resolver=resolver)
    return {
        (canonical, category_id): (agg.hit_count, agg.pinned)
        for canonical, by_category in groups.items()
        for category_id, agg in by_category.items()
    }


def _is_merchant_tag_conflict(orig: BaseException | None) -> bool:
    """True when an ``IntegrityError.orig`` is the
    ``uq_merchant_tag_map_user_merchant_category`` uniqueness violation.

    Delegates the dialect-aware matching to
    :func:`app.core.db_errors.is_unique_violation`.
    """
    return is_unique_violation(
        orig,
        index_name="uq_merchant_tag_map_user_merchant_category",
        columns=[
            "merchant_tag_map.user_id",
            "merchant_tag_map.merchant_normalized",
            "merchant_tag_map.category_id",
        ],
    )


def record_tag(
    session: Session,
    *,
    user_id: UUID,
    merchant_normalized: str,
    category_id: int,
) -> None:
    """Upsert a (user, merchant, category) row: insert or bump hit_count + last_used.

    No-op on empty merchant string (manual-entry rows with blank merchants
    shouldn't pollute the map). Caller owns the commit.

    One ``INSERT … ON CONFLICT DO UPDATE`` statement (ADR-0004 rule 7), so two
    concurrent teaches of the same triple both land: there is no SELECT-then-write
    window to race in and no SAVEPOINT, which also means the caller's pending state
    (e.g. the PATCH route's ``setattr(txn, "category_id", …)``) is untouched. A
    non-conflict ``IntegrityError`` (e.g. a foreign ``category_id``) propagates.
    """
    if not merchant_normalized:
        return

    now = clock.naive_utcnow()
    insert_or_increment(
        session,
        MerchantTagMap,
        values={
            "user_id": user_id,
            "merchant_normalized": merchant_normalized,
            "category_id": category_id,
            "hit_count": 1,
            "last_used": now,
        },
        conflict_cols=["user_id", "merchant_normalized", "category_id"],
        counter="hit_count",
        touch={"last_used": now, "updated_at": now},
        label="merchant_tag_map",
    )


def _unpin_sibling_tags(
    session: Session,
    *,
    user_id: UUID,
    merchant_normalized: str,
    keep_id: int,
) -> None:
    """Un-pin every *other* pinned category row for this merchant.

    Enforces the single-pinned-category-per-merchant invariant that ``pin_tag`` /
    ``set_tag_pinned`` rely on. ORM load-and-set (not a bulk Core UPDATE): a
    merchant has a handful of category rows at most, and staying in the unit of
    work keeps the identity map consistent for anything the request reads later.

    TODO(v2 postgres): this reads only committed siblings, so two concurrent pins
    on the same merchant / different categories could each miss the other's
    uncommitted pin and both commit ``pinned=True``. Harmless in v1 (SQLite is
    single-writer; and ``prefetch_tag_map``'s ``id DESC`` tiebreak still yields one
    deterministic winner), but the Postgres cutover should back this with a partial
    unique index ``(user_id, merchant_normalized) WHERE pinned`` and map the
    resulting IntegrityError.
    """
    siblings = session.scalars(
        select(MerchantTagMap).where(
            MerchantTagMap.user_id == user_id,
            MerchantTagMap.merchant_normalized == merchant_normalized,
            MerchantTagMap.pinned.is_(True),
            MerchantTagMap.id != keep_id,
        )
    ).all()
    for sibling in siblings:
        sibling.pinned = False


def pin_tag(
    session: Session,
    *,
    user_id: UUID,
    merchant_normalized: str,
    category_id: int,
) -> MerchantTagMap:
    """Pin merchant→category: make it this merchant's suggestion, user-authored.

    Upserts the ``(user, merchant, category)`` row with ``pinned=True``, then
    un-pins any sibling category rows so exactly one category is pinned per
    merchant (F3 authoring — ``/settings/rules``). Serves both *create-new* and
    *re-point-to-a-never-seen-category*; re-pointing to an already-learned category
    goes through :func:`set_tag_pinned` (PATCH).

    A pin never bumps ``hit_count``, so a pinned seed row stays at zero yet is
    user-authored at every read site (:func:`is_seeded` is false for it); un-pinning
    it reverts it to seeded.

    Deliberately unlike :func:`record_tag` (the *learning* path):

    * sets ``pinned=True`` (learning never does);
    * does **NOT** touch ``hit_count`` / ``last_used`` on an existing row — a pin
      is an explicit assertion, not another observed decision, so un-pinning must
      revert to the untouched learned ranking. A freshly-created row keeps the
      default ``hit_count=1`` (the honest floor for "asserted once");
    * **fails loudly** on an insert-race winner-refetch miss (raises) rather than
      ``record_tag``'s tolerated log-and-return — a success shape that created
      nothing would be a lie for an explicit user action.

    Precondition: ``merchant_normalized`` is non-empty (the endpoint 422s on blank
    after normalization). Caller owns the commit. Returns the pinned row.
    """
    existing = session.scalar(
        select(MerchantTagMap).where(
            MerchantTagMap.user_id == user_id,
            MerchantTagMap.merchant_normalized == merchant_normalized,
            MerchantTagMap.category_id == category_id,
        )
    )
    if existing is not None:
        existing.pinned = True
        row = existing
    else:
        row = MerchantTagMap(
            user_id=user_id,
            merchant_normalized=merchant_normalized,
            category_id=category_id,
            pinned=True,
        )
        if not savepoint_insert(session, row, is_conflict=_is_merchant_tag_conflict):
            # Concurrent-insert race: the row now exists — refetch and pin it.
            refetched = session.scalar(
                select(MerchantTagMap).where(
                    MerchantTagMap.user_id == user_id,
                    MerchantTagMap.merchant_normalized == merchant_normalized,
                    MerchantTagMap.category_id == category_id,
                )
            )
            if refetched is None:
                # Impossible: the unique constraint just proved the row exists.
                # An explicit pin must not "succeed" having created nothing.
                raise RuntimeError("pin_tag: winner refetch missed after unique conflict")
            refetched.pinned = True
            row = refetched

    _unpin_sibling_tags(
        session,
        user_id=user_id,
        merchant_normalized=merchant_normalized,
        keep_id=row.id,
    )
    return row


def set_tag_pinned(
    session: Session,
    *,
    user_id: UUID,
    map_id: int,
    pinned: bool,
) -> MerchantTagMap | None:
    """Toggle ``pinned`` on an existing merchant→category row (PATCH).

    Pinning un-pins the merchant's sibling category rows (single pinned category);
    un-pinning just clears the flag and reverts to the learned ranking. Never
    touches ``hit_count`` / ``last_used``. Returns ``None`` when the row is absent
    or not the caller's (endpoint 404s). Caller owns the commit.
    """
    row = session.scalar(
        select(MerchantTagMap).where(
            MerchantTagMap.id == map_id,
            MerchantTagMap.user_id == user_id,
        )
    )
    if row is None:
        return None
    row.pinned = pinned
    if pinned:
        _unpin_sibling_tags(
            session,
            user_id=user_id,
            merchant_normalized=row.merchant_normalized,
            keep_id=row.id,
        )
    return row
