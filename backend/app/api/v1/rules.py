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

Map-table *writes* live in the services (``tag_service.pin_tag`` /
``set_tag_pinned``, ``merchant_labels.pin_label`` / ``set_label_pinned``); the
handlers stay thin and own the single per-request commit. PII: merchant strings
can carry names, so no handler logs ``merchant_normalized`` (matches the
deliberate omission in ``record_tag`` / ``record_label``).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.api.deps import CurrentUserId, SessionDep
from app.models import Category, Label, MerchantLabelMap, MerchantTagMap, Transaction
from app.schemas import (
    CategoryRuleCreate,
    CategoryRuleRead,
    LabelRuleCreate,
    LabelRuleRead,
    MerchantRuleRead,
    RulePinPatch,
    RuleWriteResult,
)
from app.services.category_service import validate_category_ids
from app.services.merchant import normalize_merchant
from app.services.merchant_labels import LABEL_PREFILL_MIN, pin_label, set_label_pinned
from app.services.tag_service import merchant_map_winner_order, pin_tag, set_tag_pinned

router = APIRouter(prefix="/rules", tags=["rules"])


@router.get("", response_model=list[MerchantRuleRead])
def list_rules(
    session: SessionDep,
    user_id: CurrentUserId,
) -> list[MerchantRuleRead]:
    """Every learned rule for the current user, grouped by normalized merchant.

    Two user-scoped SELECTs, each ordered by the shared
    :func:`app.services.tag_service.merchant_map_winner_order` (merchant ASC,
    pinned DESC, hit_count DESC, last_used DESC, id DESC) so the first category
    row seen per merchant is the one import auto-tag prefills (``is_winner``) —
    the exact ordering :func:`app.services.tag_service.prefetch_tag_map` uses,
    including the ``pinned DESC`` prepend that makes a user-pinned rule outrank a
    higher-hit_count learned row. Sharing the order-by keeps the "suggested" badge
    from diverging from the real prefill. Labels carry
    ``prefills = pinned or hit_count >= LABEL_PREFILL_MIN``.

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
    cat_stmt = (
        select(MerchantTagMap, Category.name)
        .join(Category, Category.id == MerchantTagMap.category_id)
        .where(
            MerchantTagMap.user_id == user_id,
            Category.user_id == user_id,
            Category.archived_at.is_(None),
        )
        # Same winner ordering as prefetch_tag_map — the first category row per
        # merchant is the prefill winner (is_winner below).
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

    def _bucket(merchant: str) -> MerchantRuleRead:
        rule = grouped.get(merchant)
        if rule is None:
            rule = MerchantRuleRead(merchant_normalized=merchant, categories=[], labels=[])
            grouped[merchant] = rule
        return rule

    # First category row per merchant is the prefill winner (ordered above). Rows
    # arrive in winner order, so the first one bucketed for a merchant (its
    # `categories` still empty) is the winner — no separate seen-set needed.
    for row, category_name in session.execute(cat_stmt):
        rule = _bucket(row.merchant_normalized)
        is_winner = not rule.categories
        rule.categories.append(
            CategoryRuleRead(
                id=row.id,
                category_id=row.category_id,
                category_name=category_name,
                hit_count=row.hit_count,
                last_used=row.last_used,
                is_winner=is_winner,
                pinned=row.pinned,
            )
        )

    for row, label_name in session.execute(label_stmt):
        rule = _bucket(row.merchant_normalized)
        rule.labels.append(
            LabelRuleRead(
                id=row.id,
                label_id=row.label_id,
                label_name=label_name,
                hit_count=row.hit_count,
                last_used=row.last_used,
                prefills=row.pinned or row.hit_count >= LABEL_PREFILL_MIN,
                prefill_threshold=LABEL_PREFILL_MIN,
                pinned=row.pinned,
            )
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
    archived + ``kind="spend"`` — the merchant→category map is spend/refund-only,
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
