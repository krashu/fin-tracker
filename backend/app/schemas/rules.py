"""Auto-tag rule schemas (PRD §F3 / §F3a).

Wire shapes for ``/api/v1/rules`` — the user-facing view of, and edits to, the two
per-merchant memory tables the import pipeline learns from:

* ``merchant_tag_map`` (F3) → merchant → *category* (one winner prefills).
* ``merchant_label_map`` (F3a) → merchant → *label set* (each label auto-applies
  once ``hit_count >= LABEL_PREFILL_MIN`` **or** it is pinned).

Beyond the read view, the user can now *author* rules by pinning a merchant→category
or merchant→label association (the ``*Create`` / ``RulePinPatch`` shapes). A pinned
rule outranks any higher-``hit_count`` learned row. This stays **merchant-exact**,
so it is **not** the user-authored *regex* rules that remain out of v1 scope
(PRD §F3), and it is **not** F4a reconciliation (hard-coded pipeline behaviour).

Read rows are built by hand in the route from two SELECTs, so no
``from_attributes`` bridge is needed; ``user_id`` is deliberately omitted from
every shape (narrow wire, clean single-user → multi-user v2 boundary — mirrors
``CategoryRead``).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class CategoryRuleRead(BaseModel):
    """One learned merchant→category association (a ``merchant_tag_map`` row)."""

    id: int  # merchant_tag_map.id — the DELETE / PATCH handle
    category_id: int
    category_name: str
    hit_count: int
    last_used: datetime
    # The row import auto-tag prefills for this merchant: the ordered-first by
    # (pinned DESC, hit_count DESC, last_used DESC, id DESC), matching
    # tag_service's winner pick — NOT "max hit_count" (ties resolve to exactly
    # one winner; a pin outranks any hit_count).
    is_winner: bool
    # True when the user manually pinned this merchant→category rule (F3 authoring).
    # A pinned row wins regardless of hit_count; un-pinning reverts to learned.
    pinned: bool


class LabelRuleRead(BaseModel):
    """One learned merchant→label association (a ``merchant_label_map`` row)."""

    id: int  # merchant_label_map.id — the DELETE / PATCH handle
    label_id: int
    label_name: str  # stored plain (no leading '#'); UI adds the '#'
    hit_count: int
    last_used: datetime
    # True when this label auto-applies in the import review queue: either
    # hit_count cleared LABEL_PREFILL_MIN (learned) OR the user pinned it (F3a
    # authoring). Below the bar and unpinned it is still learning.
    prefills: bool
    # The learned-prefill bar (LABEL_PREFILL_MIN) — carried so the client renders
    # the "learning · {hit_count}/{prefill_threshold}" hint from server truth
    # instead of hardcoding the number (which drifts if the backend bar changes).
    prefill_threshold: int
    # True when the user manually pinned this merchant→label rule; a pinned label
    # prefills even below LABEL_PREFILL_MIN. Un-pinning reverts to learned.
    pinned: bool


class MerchantRuleRead(BaseModel):
    """All rules for one normalized merchant (union of both maps)."""

    merchant_normalized: str
    categories: list[CategoryRuleRead]
    labels: list[LabelRuleRead]


# ``merchant`` is the *raw* string; the route normalizes it (lowercase + whitespace
# collapse today) before storing. min_length rejects a blank submission at the
# boundary; the route additionally 422s if it normalizes to empty (e.g. all
# whitespace). max_length matches the map tables' ``String(512)`` so an overlong
# free-entry value fails with 422 here, not an opaque DB error on Postgres.
_MerchantField = Field(min_length=1, max_length=512)


class CategoryRuleCreate(BaseModel):
    """Pin a merchant→category rule (create-new or re-point to a never-seen category)."""

    merchant: str = _MerchantField
    category_id: int


class LabelRuleCreate(BaseModel):
    """Pin a merchant→label rule. ``label_id`` must be an existing user label —
    the rules page does not create tags (no get-or-create side effect)."""

    merchant: str = _MerchantField
    label_id: int


class RulePinPatch(BaseModel):
    """Toggle ``pinned`` on an existing rule row (PATCH)."""

    pinned: bool


class RuleWriteResult(BaseModel):
    """Result of a pin/create/toggle write. Enough for the UI toast (the
    server-normalized ``merchant_normalized`` is echoed back so the client never
    reimplements ``normalize_merchant``); the client refetches ``GET /rules`` for
    the full grouped view."""

    id: int  # the merchant_tag_map / merchant_label_map row id
    merchant_normalized: str
    pinned: bool
