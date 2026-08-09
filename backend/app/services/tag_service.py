"""Merchant → category auto-tagging (per PRD §F3, exact-match v1).

:func:`prefetch_tag_map` is called once per import to populate a
``{merchant_normalized: category_id}`` dict that the row loop checks with
``dict.get(...)``. It mirrors the ``existing_fps`` prefetch in ``import_service`` —
one SELECT, O(1) per-row lookups — which is the non-obvious part worth recording.

A single-row ``lookup_category_id`` variant is intentionally absent —
no concrete caller today needs it (CLAUDE.md §2).

(No caller list here: a hand-maintained one drifts by construction — the previous
version named a module that no longer imports this one, undercounted the importers,
and described a shipped caller in the future tense. ``grep`` is authoritative.)
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import UnaryExpression

from app.core import clock
from app.core.db_errors import is_unique_violation, savepoint_insert
from app.core.log_config import get_logger
from app.models import Category, MerchantLabelMap, MerchantTagMap, TransactionTypeStr

logger = get_logger(__name__)


def merchant_map_winner_order(
    model: type[MerchantTagMap] | type[MerchantLabelMap],
) -> list[UnaryExpression[Any]]:
    """The per-merchant winner ordering shared by every merchant→(category|label)
    read: prefill (:func:`prefetch_tag_map` / :func:`prefetch_label_map`) and the
    rules list (:mod:`app.api.v1.rules`).

    ``merchant ASC`` groups; ``pinned DESC`` makes a user-authored row outrank a
    higher-``hit_count`` learned one (F3/F3a authoring); then ``hit_count DESC``,
    ``last_used DESC``, and a final ``id DESC`` deterministic tiebreak
    (``server_default=now()`` is second-precision on SQLite, so ties happen in
    tests and rapid re-tagging). Defining it once keeps prefill and the "suggested"
    badge from silently diverging — a change here moves both.
    """
    return [
        model.merchant_normalized.asc(),
        model.pinned.desc(),
        model.hit_count.desc(),
        model.last_used.desc(),
        model.id.desc(),
    ]


# Transaction types that participate in F3 auto-tagging — both the learning
# signal (every record_tag call site: import commit + manual POST/PATCH) and the
# import auto-suggest (import_service). Income and transfer are hand-classified
# in a separate taxonomy, so a manual income/transfer row with a category must
# NOT feed the spend/refund merchant→category map (it could only ever resurface
# as a cross-taxonomy spend suggestion). Manual entry was previously type-agnostic
# here, unlike import — this set now gates all three call sites alike.
# Typed against TransactionTypeStr so a future widening of that Literal
# (e.g. adding "adjustment") surfaces in grep / ty when re-evaluating this set.
AUTO_TAGGABLE_TYPES: frozenset[TransactionTypeStr] = frozenset({"spend", "refund"})


def should_learn_tag(*, transaction_type: str, merchant_normalized: str) -> bool:
    """True when a row's type + merchant make it eligible to teach F3.

    The shared eligibility core for every :func:`record_tag` call site — POST /
    PATCH manual entry (:mod:`app.api.v1.transactions`), import commit
    (:mod:`app.api.v1.imports`), and the demo seeder
    (:mod:`app.services.demo_seed`): a non-empty normalized merchant and a
    spend/refund type. ``income`` / ``transfer`` are hand-classified and must
    never feed the spend→category map.

    The caller additionally checks ``category_id is not None`` (there is no tag
    to learn without a category, and :func:`record_tag` needs the concrete id),
    plus its own extra guards — PATCH: confirmed + changed; commit:
    not-Other-defaulted.
    """
    return bool(merchant_normalized) and transaction_type in AUTO_TAGGABLE_TYPES


def prefetch_tag_map(session: Session, *, user_id: UUID) -> dict[str, int]:
    """Return ``{merchant_normalized: category_id}`` of the winning tag per merchant.

    One SELECT joining ``merchant_tag_map`` → ``categories`` filtered to the
    given user and non-archived categories. The ORDER BY puts the winner
    first per merchant; the Python reducer keeps the first row seen.

    ``Category.user_id`` is restated alongside ``MerchantTagMap.user_id`` even though
    the ``category_id`` came off the user's own map row (ADR-0003 defence-in-depth).
    ``merchant_tag_map.category_id`` is a plain FK — unlike the sibling
    ``merchant_label_map``, which carries ADR-0002's composite same-user FK, which is
    why :func:`app.services.merchant_labels.prefetch_label_map` needs no equivalent —
    so the DB would permit a cross-user row. No write path can create one today, but
    this is the one merchant-memory read whose result is WRITTEN back, onto imported
    rows as ``auto_category_id``, so a dangling ref degrading to "absent" rather than
    to another user's bucket is worth the clause.

    The archived-category filter is **load-bearing**: ``DELETE /categories/{id}``
    keeps the tag-map rows (archive is a pure ``archived_at`` UPDATE — see
    :mod:`app.api.v1.categories`), so archived categories DO have live tag-map
    rows and this JOIN filter is the sole thing that stops F3 prefilling an
    archived bucket. It also means a user-authored ``pinned`` rule survives an
    archive and returns if the category is ever un-archived.
    """
    stmt = (
        select(MerchantTagMap.merchant_normalized, MerchantTagMap.category_id)
        .join(Category, Category.id == MerchantTagMap.category_id)
        .where(
            MerchantTagMap.user_id == user_id,
            Category.user_id == user_id,
            Category.archived_at.is_(None),
        )
        # Shared winner ordering (see merchant_map_winner_order): pinned rows win,
        # then hit_count/last_used, with an id DESC deterministic tiebreak. When no
        # row is pinned the pinned key is a constant within each merchant group → a
        # no-op tiebreak, so the winner is byte-identical to the pre-authoring
        # behaviour (guarded by an all-unpinned regression test).
        .order_by(*merchant_map_winner_order(MerchantTagMap))
    )
    out: dict[str, int] = {}
    # TODO(v2 postgres): push the winner-per-merchant pick into SQL via
    # ``DISTINCT ON (merchant_normalized)``. SQLite has no DISTINCT ON, so v1
    # does the reduction in Python — small enough to be free for a single user.
    for merchant_normalized, category_id in session.execute(stmt):
        out.setdefault(merchant_normalized, category_id)
    return out


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

    Race handling: SELECT-then-INSERT-or-UPDATE. If two near-simultaneous
    PATCHes hit the same triple, both SELECT miss; the loser's INSERT trips
    ``uq_merchant_tag_map_user_merchant_category`` → ``IntegrityError``. The
    INSERT is wrapped in a ``begin_nested()`` SAVEPOINT so the failure rolls
    back ONLY the insert — pending caller-side state (e.g. the PATCH route's
    ``setattr(txn, "category_id", …)``) survives. A naive ``session.rollback()``
    would discard the parent transaction and silently drop the user's
    category change. For the Postgres v2 swap see
    :func:`app.core.db_errors.savepoint_insert` — it is not a single edit, and that
    docstring is the one place scoping it.

    Asymmetric flush: the INSERT branch flushes to surface the IntegrityError
    here (otherwise it'd raise at commit time, outside our handler). The
    UPDATE branch does not — caller's commit flushes the dirty hit_count.
    """
    if not merchant_normalized:
        return

    existing = session.scalar(
        select(MerchantTagMap).where(
            MerchantTagMap.user_id == user_id,
            MerchantTagMap.merchant_normalized == merchant_normalized,
            MerchantTagMap.category_id == category_id,
        )
    )
    if existing is not None:
        existing.hit_count += 1
        existing.last_used = clock.naive_utcnow()
        return

    # savepoint_insert isolates the INSERT in a SAVEPOINT: True → inserted;
    # False → the (user, merchant, category) uniqueness violation (the
    # concurrent-insert race), rolled back, parent transaction intact — refetch
    # the winner and bump. Any other IntegrityError re-raises inside the helper.
    if not savepoint_insert(
        session,
        MerchantTagMap(
            user_id=user_id,
            merchant_normalized=merchant_normalized,
            category_id=category_id,
        ),
        is_conflict=_is_merchant_tag_conflict,
    ):
        winner = session.scalar(
            select(MerchantTagMap).where(
                MerchantTagMap.user_id == user_id,
                MerchantTagMap.merchant_normalized == merchant_normalized,
                MerchantTagMap.category_id == category_id,
            )
        )
        if winner is None:
            # The refetch should always find the row the unique constraint just
            # collided with. If it doesn't, the user's decision is being lost —
            # surface it (PII: never log merchant_normalized; it can carry names).
            logger.warning(
                "record_tag_conflict_no_winner",
                user_id=str(user_id),
                category_id=category_id,
            )
            return
        winner.hit_count += 1
        winner.last_used = clock.naive_utcnow()


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
