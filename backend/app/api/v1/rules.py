"""Auto-tag rule routes (PRD §F3 / §F3a).

The user-facing view of — and edits to — the two per-merchant memory tables the
import pipeline learns from and prefills against:

* ``merchant_tag_map`` (F3) — merchant → *category*, one winner prefilled.
* ``merchant_label_map`` (F3a) — merchant → *label set*, each label auto-applies
  once its ``hit_count`` clears ``LABEL_PREFILL_MIN`` **or** it is pinned.

**Scope.** These are NOT F4a reconciliation rules (CC-bill auto-link etc. —
hard-coded pipeline behaviour) and NOT user-authored *regex* rules (still out of
v1 scope, PRD §F3). Beyond reading and pruning learned memory, the user can now
**author** a rule by *pinning* a merchant→category or merchant→label association
(``pinned=True``). A pinned rule outranks any higher-``hit_count`` learned row, so
a user's choice can't be silently overturned by future imports. This is a
deliberate extension of §F3's *learned* memory into *authored* memory — merchant
exact, never regex; the PRD §F3 note tracks the reconciliation. Pin/un-pin toggle
only the flag (never ``hit_count`` / ``last_used``), so un-pinning reverts cleanly
to the learned ranking, and deleting a rule still forgets the memory entirely
(the transactions that taught it keep their category / labels).

* ``GET /rules`` — all rules, grouped by merchant, merchant ASC.
* ``GET /rules/merchants`` — distinct observed merchants (create autocomplete).
* ``POST /rules/categories`` — pin/create a merchant→category rule.
* ``PATCH /rules/categories/{map_id}`` — toggle ``pinned`` on a category rule.
* ``POST /rules/labels`` — pin/create a merchant→label rule.
* ``PATCH /rules/labels/{map_id}`` — toggle ``pinned`` on a label rule.
* ``DELETE /rules/categories/{map_id}`` / ``.../labels/{map_id}`` — forget one row.
* ``GET /rules/aliases`` — list the user's merchant-alias table (ADR-0011).
* ``POST /rules/aliases`` — add a ``pattern -> canonical`` rule.
* ``PATCH /rules/aliases/{alias_id}`` — rename an alias's ``canonical`` target.
* ``DELETE /rules/aliases/{alias_id}`` — forget one alias.

Map-table *writes* live in the services (``tag_service.pin_tag`` /
``set_tag_pinned``, ``merchant_labels.pin_label`` / ``set_label_pinned``); the
handlers stay thin and own the single per-request commit. PII: merchant strings
can carry names, so no handler logs ``merchant_normalized`` (matches the
deliberate omission in ``record_tag`` / ``record_label``).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import CurrentUserId, SessionDep
from app.core.db_errors import is_unique_violation
from app.models import Category, Label, MerchantAlias, MerchantLabelMap, MerchantTagMap, Transaction
from app.schemas import (
    CategoryRuleCreate,
    CategoryRuleRead,
    LabelRuleCreate,
    LabelRuleRead,
    MerchantAliasCreate,
    MerchantAliasRead,
    MerchantAliasUpdate,
    MerchantRuleRead,
    RulePinPatch,
    RuleWriteResult,
)
from app.services.category_service import validate_category_ids
from app.services.merchant import normalize_merchant
from app.services.merchant_alias import (
    build_resolver,
    load_alias_resolver,
    tokenize,
    tokens_match,
)
from app.services.merchant_labels import LABEL_PREFILL_MIN, pin_label, set_label_pinned
from app.services.tag_service import (
    merchant_map_winner_order,
    pin_tag,
    prefetch_tag_map,
    set_tag_pinned,
)

router = APIRouter(prefix="/rules", tags=["rules"])


@router.get("", response_model=list[MerchantRuleRead])
def list_rules(
    session: SessionDep,
    user_id: CurrentUserId,
) -> list[MerchantRuleRead]:
    """Every learned rule for the current user, grouped by canonical merchant.

    Two user-scoped SELECTs, each ordered by the shared
    :func:`app.services.tag_service.merchant_map_winner_order` (merchant ASC,
    pinned DESC, hit_count DESC, last_used DESC, id DESC) — that ordering is
    now cosmetic display order within a canonical's row list, not the winner
    pick. As of Phase A3 (ADR-0011 merchant-alias layer) rows are bucketed by
    ``resolver.canonical(merchant_normalized)`` rather than the raw string, so
    several raw descriptors can land in one ``MerchantRuleRead``. ``is_winner``
    comes from :func:`app.services.tag_service.prefetch_tag_map` — the same
    per-canonical AGGREGATE winner (summed hit_count across every raw
    descriptor) the real import prefill uses — not first-row-seen, which
    would silently disagree with the prefill once aliasing folds rows
    together.

    Labels aggregate the same way, and for the same reason: one entry per
    ``(canonical, label_id)`` carrying the SUMMED ``hit_count``, ``pinned`` OR-ed
    and ``last_used`` maxed across every raw descriptor —
    :func:`app.services.merchant_labels.prefetch_label_map` evaluates
    ``LABEL_PREFILL_MIN`` on that sum, so a per-raw-row ``prefills`` flag reported
    the OPPOSITE of what the import does (three ``swiggy*x`` rows at 1 each
    auto-apply as canonical ``swiggy`` at 3 while every row reads "needs 2 more").
    Aggregating also collapses what would otherwise be several identical
    ``LabelRuleRead`` entries in one group.

    Deliberately asymmetric with the category rows, which stay per-row: a label
    set's threshold is evaluated on the sum, so a per-row number is a lie,
    whereas the categories' single-winner pick is already marked
    aggregate-correctly by ``is_winner`` and each row remains a distinct
    ``(merchant, category)`` association the user may want to prune on its own.

    The category JOIN filters ``Category.user_id == user_id`` in addition to
    ``MerchantTagMap.user_id`` — the same pair ``prefetch_tag_map`` now uses (this read
    was hardened first; the write-side prefill caught up in the A1.3/A3.3 pass). It
    closes a defence-in-depth gap where a corrupt cross-user map row could otherwise
    surface another user's ``category_name`` here. Archived categories are excluded
    (mirrors ``prefetch_tag_map``): DELETE /categories keeps their map rows
    (archive is a pure ``archived_at`` UPDATE), so this filter is the
    load-bearing guard that hides an archived category's rules from the list —
    they return if the category is ever un-archived.
    """
    resolver = load_alias_resolver(session, user_id=user_id)
    # {canonical: winning category_id} — the exact aggregate prefetch_tag_map
    # uses for the real import prefill; is_winner below must agree with it.
    winners = prefetch_tag_map(session, user_id=user_id, resolver=resolver)

    cat_stmt = (
        select(MerchantTagMap, Category.name)
        .join(Category, Category.id == MerchantTagMap.category_id)
        .where(
            MerchantTagMap.user_id == user_id,
            Category.user_id == user_id,
            Category.archived_at.is_(None),
        )
        .order_by(*merchant_map_winner_order(MerchantTagMap))
    )
    label_stmt = (
        select(MerchantLabelMap, Label.name)
        .join(
            Label,
            (Label.id == MerchantLabelMap.label_id) & (Label.user_id == MerchantLabelMap.user_id),
        )
        .where(MerchantLabelMap.user_id == user_id)
        .order_by(*merchant_map_winner_order(MerchantLabelMap))
    )

    grouped: dict[str, MerchantRuleRead] = {}
    # canonical -> how many alias patterns fold onto it. Counted from the alias
    # table, NOT from the map rows in the group: an aliased merchant the user has
    # learned exactly one rule for still has its patterns folding, and every
    # seeded fan-in has exactly one map row, so a map-row count read 1 for every
    # case the badge exists to show.
    alias_counts = resolver.pattern_counts()

    def _bucket(canonical: str) -> MerchantRuleRead:
        rule = grouped.get(canonical)
        if rule is None:
            rule = MerchantRuleRead(
                merchant_normalized=canonical,
                categories=[],
                labels=[],
                alias_count=alias_counts.get(canonical, 1),
                seeded=False,
            )
            grouped[canonical] = rule
        return rule

    for row, category_name in session.execute(cat_stmt):
        canonical = resolver.canonical(row.merchant_normalized)
        rule = _bucket(canonical)
        rule.categories.append(
            CategoryRuleRead(
                id=row.id,
                category_id=row.category_id,
                category_name=category_name,
                hit_count=row.hit_count,
                last_used=row.last_used,
                is_winner=row.category_id == winners.get(canonical),
                pinned=row.pinned,
            )
        )

    # Labels aggregate per (canonical, label_id) — see the docstring. The rows
    # arrive winner-ordered, so the FIRST one seen for a label is the one whose
    # id becomes the group's DELETE handle.
    label_slots: dict[str, dict[int, LabelRuleRead]] = {}
    for row, label_name in session.execute(label_stmt):
        canonical = resolver.canonical(row.merchant_normalized)
        rule = _bucket(canonical)
        by_label = label_slots.setdefault(canonical, {})
        merged = by_label.get(row.label_id)
        if merged is None:
            merged = LabelRuleRead(
                id=row.id,
                label_id=row.label_id,
                label_name=label_name,
                hit_count=row.hit_count,
                last_used=row.last_used,
                prefills=False,  # set once the group is fully summed, below
                prefill_threshold=LABEL_PREFILL_MIN,
                pinned=row.pinned,
            )
            by_label[row.label_id] = merged
            rule.labels.append(merged)
            continue
        merged.hit_count += row.hit_count
        merged.pinned = merged.pinned or row.pinned
        merged.last_used = max(merged.last_used, row.last_used)

    for by_label in label_slots.values():
        for merged in by_label.values():
            merged.prefills = merged.pinned or merged.hit_count >= LABEL_PREFILL_MIN

    for rule in grouped.values():
        # "Seeded" means an UNTOUCHED dictionary entry (Phase A5) — every
        # category row in the group at hit_count == 0 and none of them pinned.
        # A group with no category rows at all (labels-only) is not "seeded";
        # the seed dictionary only ever seeds categories, so the literal "every
        # category row is at hit_count == 0" would otherwise be vacuously
        # True on an empty list and mislabel a labels-only merchant.
        #
        # The `not any(pinned)` clause is load-bearing, not belt-and-braces:
        # pin_tag deliberately never bumps hit_count on an existing row, so
        # pinning a seed row leaves it at hit_count == 0, pinned=True. Without
        # this, the user's own authored rule renders with the dashed "seeded"
        # badge titled "not yet confirmed".
        rule.seeded = (
            bool(rule.categories)
            and all(c.hit_count == 0 for c in rule.categories)
            and not any(c.pinned for c in rule.categories)
        )

    return [grouped[m] for m in sorted(grouped)]


@router.get("/merchants", response_model=list[str])
def list_rule_merchants(
    session: SessionDep,
    user_id: CurrentUserId,
) -> list[str]:
    """Distinct normalized merchants the user has actually seen — the create-rule
    autocomplete source.

    Union of the three places a merchant key lives (``transactions`` + both memory
    maps), user-scoped, blanks excluded (a manual-entry row can carry an empty
    ``merchant_normalized``; the maps can't, but the filter is uniform). The keys
    are already server-normalized, so the client offers them verbatim and never
    reimplements :func:`app.services.merchant.normalize_merchant`.

    Under aliasing (ADR-0011) this list mixes **raw** descriptors and
    **canonicals** — both are legitimate authoring inputs (a raw descriptor is
    what a new alias's ``pattern`` targets; a canonical is what a category/label
    rule pins against), so the mix is intentional, not a bug. Once Phase A5's
    seed dictionary ships, a fresh user's ~100 seeded canonicals also appear
    here. If the mixed list proves confusing in practice, splitting the
    endpoint is a Stage B question — do **not** add a ``?kind=`` filter param
    for a hypothetical UX need that hasn't been observed (AGENTS.md
    §Simplicity first).
    """
    union_stmt = (
        select(Transaction.merchant_normalized)
        .where(
            Transaction.user_id == user_id,
            Transaction.merchant_normalized != "",
        )
        .union(
            select(MerchantTagMap.merchant_normalized).where(
                MerchantTagMap.user_id == user_id,
                MerchantTagMap.merchant_normalized != "",
            ),
            select(MerchantLabelMap.merchant_normalized).where(
                MerchantLabelMap.user_id == user_id,
                MerchantLabelMap.merchant_normalized != "",
            ),
        )
    )
    return sorted(session.scalars(union_stmt))


@router.post(
    "/categories",
    response_model=RuleWriteResult,
    status_code=status.HTTP_201_CREATED,
)
def create_category_rule(
    payload: CategoryRuleCreate,
    session: SessionDep,
    user_id: CurrentUserId,
) -> RuleWriteResult:
    """Pin a merchant→category rule (create-new, or re-point to a never-seen
    category). Re-pointing to an already-learned category uses PATCH instead.

    Normalizes the merchant, 422s if it is blank after normalization, validates
    the category via the shared :func:`validate_category_ids` (owned + not
    archived + ``kind="spend"`` — the merchant→category map is spend-only
    (refunds included: they are spend rows with a positive amount),
    so pinning a merchant to an income category is rejected here, not just in the
    UI), then delegates the upsert + single-pinned-per-merchant invariant to
    :func:`app.services.tag_service.pin_tag`.
    """
    merchant = normalize_merchant(payload.merchant)
    if not merchant:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="merchant is blank after normalization",
        )
    if payload.category_id not in validate_category_ids(
        session, category_ids=[payload.category_id], user_id=user_id, kind="spend"
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="category not found, archived, or not a spend category",
        )
    row = pin_tag(
        session,
        user_id=user_id,
        merchant_normalized=merchant,
        category_id=payload.category_id,
    )
    result = RuleWriteResult(
        id=row.id, merchant_normalized=row.merchant_normalized, pinned=row.pinned
    )
    session.commit()
    return result


@router.patch("/categories/{map_id}", response_model=RuleWriteResult)
def patch_category_rule(
    map_id: int,
    payload: RulePinPatch,
    session: SessionDep,
    user_id: CurrentUserId,
) -> RuleWriteResult:
    """Toggle ``pinned`` on an existing merchant→category row. Pinning makes it the
    merchant's suggestion (un-pinning its siblings); un-pinning reverts to learned.
    404 when the row is absent or not the caller's."""
    row = set_tag_pinned(session, user_id=user_id, map_id=map_id, pinned=payload.pinned)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="rule not found",
        ) from None
    result = RuleWriteResult(
        id=row.id, merchant_normalized=row.merchant_normalized, pinned=row.pinned
    )
    session.commit()
    return result


@router.post(
    "/labels",
    response_model=RuleWriteResult,
    status_code=status.HTTP_201_CREATED,
)
def create_label_rule(
    payload: LabelRuleCreate,
    session: SessionDep,
    user_id: CurrentUserId,
) -> RuleWriteResult:
    """Pin a merchant→label rule. The label must be an existing user label — the
    rules page never creates tags (no get-or-create side effect).

    Normalizes the merchant, 422s on a blank result or an unknown/cross-user label,
    then delegates to :func:`app.services.merchant_labels.pin_label`.
    """
    merchant = normalize_merchant(payload.merchant)
    if not merchant:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="merchant is blank after normalization",
        )
    label = session.scalar(
        select(Label).where(Label.id == payload.label_id, Label.user_id == user_id)
    )
    if label is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="label not found",
        )
    row = pin_label(
        session,
        user_id=user_id,
        merchant_normalized=merchant,
        label_id=payload.label_id,
    )
    result = RuleWriteResult(
        id=row.id, merchant_normalized=row.merchant_normalized, pinned=row.pinned
    )
    session.commit()
    return result


@router.patch("/labels/{map_id}", response_model=RuleWriteResult)
def patch_label_rule(
    map_id: int,
    payload: RulePinPatch,
    session: SessionDep,
    user_id: CurrentUserId,
) -> RuleWriteResult:
    """Toggle ``pinned`` on an existing merchant→label row. Pinning makes the label
    prefill even below the learned bar; un-pinning reverts to learned. 404 when the
    row is absent or not the caller's."""
    row = set_label_pinned(session, user_id=user_id, map_id=map_id, pinned=payload.pinned)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="rule not found",
        ) from None
    result = RuleWriteResult(
        id=row.id, merchant_normalized=row.merchant_normalized, pinned=row.pinned
    )
    session.commit()
    return result


@router.delete("/categories/{map_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_category_rule(
    map_id: int,
    session: SessionDep,
    user_id: CurrentUserId,
) -> None:
    """Forget one merchant→category association. Idempotent-404 on re-delete."""
    rule = session.scalar(
        select(MerchantTagMap).where(
            MerchantTagMap.id == map_id,
            MerchantTagMap.user_id == user_id,
        )
    )
    if rule is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="rule not found",
        ) from None
    session.delete(rule)
    session.commit()
    return None


@router.delete("/labels/{map_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_label_rule(
    map_id: int,
    session: SessionDep,
    user_id: CurrentUserId,
) -> None:
    """Forget one merchant→label association. Idempotent-404 on re-delete."""
    rule = session.scalar(
        select(MerchantLabelMap).where(
            MerchantLabelMap.id == map_id,
            MerchantLabelMap.user_id == user_id,
        )
    )
    if rule is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="rule not found",
        ) from None
    session.delete(rule)
    session.commit()
    return None


def _alias_conflict(
    session: Session,
    *,
    user_id: UUID,
    pattern: str,
    canonical: str,
    check_duplicate: bool,
    exclude_id: int | None,
) -> str | None:
    """Decision 7's no-chaining conflict check, enforced where it can be.

    Returns a 422 detail string on the first conflict found, else ``None``.
    ``pattern`` is always the submission's pattern (on PATCH the row's own,
    since pattern is immutable); ``exclude_id`` skips the row being edited so
    its stored canonical isn't compared against its own replacement. Three
    checks:

    * ``check_duplicate`` (create only) — an exact duplicate pattern.
    * **Direction 1, the canonical must be a FIXED POINT.** Resolve the new
      canonical through the *prospective* rule set — every other row the user
      owns, PLUS the row being written — and reject only if it comes back as
      something else. That is what decision 7's "no chaining" actually
      forbids: a canonical another rule rewrites, so the fan-in the alias was
      meant to create silently lands somewhere else.

      This deliberately does NOT reject a canonical merely *matched* by an
      existing pattern, which is what an earlier draft did. Because Phase A5's
      seed dictionary makes all ~94 canonicals patterns too, that rejected
      every alias targeting a seeded brand — the feature's headline authoring
      flow. Both shapes it wrongly blocked are harmless under single-pass
      longest-pattern-first resolution: a redundant
      ``swiggy blr 998877 -> swiggy`` lands on the canonical the seed pattern
      would have produced anyway, and a NARROWING ``uber eats -> uber eats``
      out-ranks the shorter ``uber`` for exactly the strings it should claim.
      Both are fixed points, so both now pass.
    * **Direction 2, unchanged** — whether the new ``pattern`` would match an
      existing row's ``canonical``. This is the genuine reverse chain (rows
      folding onto ``foo`` while ``foo`` itself rewrites to ``bar``), and it
      permits both shapes above.
    """
    stmt = select(MerchantAlias.pattern, MerchantAlias.canonical).where(
        MerchantAlias.user_id == user_id
    )
    if exclude_id is not None:
        stmt = stmt.where(MerchantAlias.id != exclude_id)
    # Materialised as real tuples, not Rows: reused twice below, and
    # build_resolver's signature is tuple-typed (a Row is not a tuple to `ty`).
    others: list[tuple[str, str]] = [
        (other_pattern, other_canonical) for other_pattern, other_canonical in session.execute(stmt)
    ]

    pattern_tokens = tokenize(pattern)
    for other_pattern, other_canonical in others:
        if check_duplicate and other_pattern == pattern:
            return "an alias for this pattern already exists"
        other_canonical_tokens = tokenize(other_canonical)
        if other_canonical_tokens and tokens_match(pattern_tokens, other_canonical_tokens):
            return "pattern would match an existing alias's canonical"

    prospective = build_resolver([*others, (pattern, canonical)])
    if prospective.canonical(canonical) != canonical:
        return "canonical would be rewritten by another alias"
    return None


@router.get("/aliases", response_model=list[MerchantAliasRead])
def list_aliases(session: SessionDep, user_id: CurrentUserId) -> list[MerchantAliasRead]:
    """Every alias row for the current user, pattern ASC (ADR-0011)."""
    rows = session.scalars(
        select(MerchantAlias)
        .where(MerchantAlias.user_id == user_id)
        .order_by(MerchantAlias.pattern)
    )
    return [
        MerchantAliasRead(
            id=row.id, pattern=row.pattern, canonical=row.canonical, is_seeded=row.is_seeded
        )
        for row in rows
    ]


@router.post("/aliases", response_model=MerchantAliasRead, status_code=status.HTTP_201_CREATED)
def create_alias(
    payload: MerchantAliasCreate,
    session: SessionDep,
    user_id: CurrentUserId,
) -> MerchantAliasRead:
    """Add a ``pattern -> canonical`` rule (ADR-0011).

    Four 422 checks, all required: blank-after-normalize on either field; a
    zero-token ``pattern`` (``tokenize(pattern) == ()`` — the false-merge
    hazard: an empty token tuple is a contiguous subsequence of every
    sequence, so an unguarded pattern like ``"***"`` would match everything
    and, sorted last by :func:`app.services.merchant_alias.build_resolver`,
    fire on exactly the merchants nothing else matched); a duplicate
    ``(user_id, pattern)``; and decision 7's no-chaining conflict in either
    direction (:func:`_alias_conflict` — read its direction-1 note before
    tightening anything here). A same-pattern ``IntegrityError`` that slips
    past the pre-check (a concurrent double-submit) maps to 409, not 422 — the
    pre-check is the expected, tested path.
    """
    pattern = normalize_merchant(payload.pattern)
    canonical = normalize_merchant(payload.canonical)
    if not pattern or not canonical:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="pattern/canonical is blank after normalization",
        )
    pattern_tokens = tokenize(pattern)
    if not pattern_tokens:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="pattern has no matchable characters",
        )
    conflict = _alias_conflict(
        session,
        user_id=user_id,
        pattern=pattern,
        canonical=canonical,
        check_duplicate=True,
        exclude_id=None,
    )
    if conflict is not None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=conflict)

    row = MerchantAlias(user_id=user_id, pattern=pattern, canonical=canonical)
    session.add(row)
    try:
        session.commit()
    except IntegrityError as e:
        session.rollback()
        if is_unique_violation(
            e.orig,
            index_name="uq_merchant_alias_user_pattern",
            columns=["merchant_alias.user_id", "merchant_alias.pattern"],
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="an alias for this pattern already exists",
            ) from e
        raise
    session.refresh(row)
    return MerchantAliasRead(
        id=row.id, pattern=row.pattern, canonical=row.canonical, is_seeded=row.is_seeded
    )


@router.patch("/aliases/{alias_id}", response_model=MerchantAliasRead)
def patch_alias(
    alias_id: int,
    payload: MerchantAliasUpdate,
    session: SessionDep,
    user_id: CurrentUserId,
) -> MerchantAliasRead:
    """Rename an alias's ``canonical`` target. ``pattern`` is immutable — it is
    the row's identity; delete and recreate to change it. 404 when the row is
    absent or not the caller's (ADR-0003 — never 403). A successful edit clears
    ``is_seeded``: the row is user data from here on."""
    row = session.scalar(
        select(MerchantAlias).where(MerchantAlias.id == alias_id, MerchantAlias.user_id == user_id)
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="alias not found"
        ) from None

    canonical = normalize_merchant(payload.canonical)
    if not canonical:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="canonical is blank after normalization",
        )
    conflict = _alias_conflict(
        session,
        user_id=user_id,
        pattern=row.pattern,
        canonical=canonical,
        check_duplicate=False,
        exclude_id=alias_id,
    )
    if conflict is not None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=conflict)

    row.canonical = canonical
    # A user edit makes the row user data, whatever it started as. Leaving
    # is_seeded set would keep the "dictionary" badge on it AND put it in reach
    # of migration 0032's `DELETE FROM merchant_alias WHERE is_seeded = TRUE`.
    row.is_seeded = False
    session.commit()
    session.refresh(row)
    return MerchantAliasRead(
        id=row.id, pattern=row.pattern, canonical=row.canonical, is_seeded=row.is_seeded
    )


@router.delete("/aliases/{alias_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_alias(alias_id: int, session: SessionDep, user_id: CurrentUserId) -> None:
    """Forget one alias. Idempotent-404 on re-delete."""
    row = session.scalar(
        select(MerchantAlias).where(MerchantAlias.id == alias_id, MerchantAlias.user_id == user_id)
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="alias not found"
        ) from None
    session.delete(row)
    session.commit()
    return None
