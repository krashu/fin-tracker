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
from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field

from app.services.merchant import merchant_too_long


class CategoryRuleRead(BaseModel):
    """One learned merchant→category association (a ``merchant_tag_map`` row)."""

    id: int  # merchant_tag_map.id — the DELETE / PATCH handle
    category_id: int
    category_name: str
    parent_id: int | None = None
    parent_name: str | None = None
    hit_count: int
    last_used: datetime
    # True when this row's category is the AGGREGATE winner for its canonical
    # merchant (ADR-0011 Phase A3: summed hit_count across every raw
    # merchant_normalized an alias folds together, ranked by
    # tag_service.merchant_agg_winner_key) — NOT "this exact row has the
    # highest hit_count" (two raw rows can share the winning category and both
    # read True; there is no single "the" winning row anymore, only a winning
    # category). Unaliased, this is identical to the pre-A3 per-row pick, except keys
    # that resolve to an unknown-via canonical, which are skipped (ADR-0011 rule 12).
    is_winner: bool
    # True when the user manually pinned this merchant→category rule (F3 authoring).
    # A pinned row wins regardless of hit_count; un-pinning reverts to learned.
    pinned: bool


class LabelRuleRead(BaseModel):
    """One learned merchant→label association, aggregated per canonical merchant.

    Not one ``merchant_label_map`` row: as of ADR-0011 the ``/rules`` list folds
    every raw descriptor an alias maps onto a canonical into a single entry per
    ``(canonical, label_id)``, mirroring
    :func:`app.services.merchant_labels.prefetch_label_map` — ``hit_count``
    summed, ``pinned`` OR-ed, ``last_used`` maxed. That is what makes
    ``prefills`` agree with the actual import behaviour.

    Consequence for ``id``: it is the DELETE handle of ONE underlying row (the
    winner-ordered first), not of the whole group. Deleting an entry that folds
    several rows drops the group's ``hit_count`` and may flip ``prefills`` rather
    than removing the entry outright — the remaining rows still carry the label.
    """

    id: int  # merchant_label_map.id of the group's winner row — DELETE / PATCH handle
    label_id: int
    label_name: str  # stored plain (no leading '#'); UI adds the '#'
    hit_count: int  # summed across the canonical's raw descriptors
    last_used: datetime  # max across them
    # True when this label auto-applies in the import review queue: either the
    # SUMMED hit_count cleared LABEL_PREFILL_MIN (learned) OR the user pinned it
    # on any of the folded rows (F3a authoring). Below the bar and unpinned it is
    # still learning.
    prefills: bool
    # The learned-prefill bar (LABEL_PREFILL_MIN) — carried so the client renders
    # the "learning · {hit_count}/{prefill_threshold}" hint from server truth
    # instead of hardcoding the number (which drifts if the backend bar changes).
    prefill_threshold: int
    # True when the user manually pinned this merchant→label rule; a pinned label
    # prefills even below LABEL_PREFILL_MIN. Un-pinning reverts to learned.
    pinned: bool


class MerchantRuleRead(BaseModel):
    """All rules for one canonical merchant (union of both maps).

    The grouping key is the CANONICAL merchant (ADR-0011 rule 8):
    ``AliasResolver.canonical()``'s output, which is the merchant itself when
    unaliased (decision 8). Two raw descriptors folded onto one canonical surface
    as a single ``MerchantRuleRead`` entry, not two. Named like
    ``TopMerchantRow.merchant_canonical``.
    """

    merchant_canonical: str
    categories: list[CategoryRuleRead]
    labels: list[LabelRuleRead]
    # How many alias PATTERNS resolve to this canonical (AliasResolver.
    # pattern_counts). 1 for an unaliased merchant — which is also the fallback
    # when the canonical has no alias row at all, since the group exists and one
    # descriptor fed it. >1 means several raw descriptors fold in.
    #
    # Deliberately not "distinct map-table keys in this group", which it was
    # first: a seeded fan-in seeds ONE merchant_tag_map row per canonical, so
    # that count read 1 for every case this number exists to surface.
    alias_count: int
    # True when every category row in the group is an unconfirmed seed
    # (hit_count == 0, decision 4's marker) — vacuously False for a group with
    # no category rows at all (a labels-only merchant), since "seeded" is
    # specifically about the seed *dictionary*, which only ever seeds
    # categories (Phase A5).
    seeded: bool


# ``merchant`` is the *raw* string; the route normalizes it (``normalize_merchant``) before
# storing. min_length rejects a blank submission at the
# boundary; the route additionally 422s if it normalizes to empty (e.g. all
# whitespace). max_length matches the map tables' ``String(512)``, and the validator refuses
# a value that normalizes past it (ADR-0006 rule 3b), so an overlong free-entry value fails
# with 422 here, not an opaque DB error on Postgres.


def _fits_merchant_columns(v: str) -> str:
    if merchant_too_long(v):
        raise ValueError("merchant is longer than 512 characters once normalized")
    return v


MerchantInput = Annotated[
    str, Field(min_length=1, max_length=512), AfterValidator(_fits_merchant_columns)
]


class CategoryRuleCreate(BaseModel):
    """Pin a merchant→category rule (create-new or re-point to a never-seen category)."""

    merchant: MerchantInput
    category_id: int


class LabelRuleCreate(BaseModel):
    """Pin a merchant→label rule. ``label_id`` must be an existing user label —
    the rules page does not create tags (no get-or-create side effect)."""

    merchant: MerchantInput
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


class MerchantAliasRead(BaseModel):
    """One user-authored ``pattern -> canonical`` row (a ``merchant_alias`` row,
    ADR-0011). ``is_seeded`` flags a dictionary entry from Phase A5, distinct
    from ``merchant_tag_map.hit_count == 0`` (decision 4) -- a different
    table's confidence marker."""

    id: int
    pattern: str
    canonical: str
    is_seeded: bool


class MerchantAliasCreate(BaseModel):
    """Create an alias. Both fields are raw merchant strings, normalized at the
    route boundary (:func:`app.services.merchant.normalize_merchant`) -- this
    schema does not normalize itself. The route additionally 422s if either
    normalizes to empty, if ``pattern`` tokenizes to ``()`` (the zero-token
    false-merge hazard), on a duplicate ``pattern``, or on decision 7's
    no-chaining conflict in either direction."""

    pattern: MerchantInput
    canonical: MerchantInput


class MerchantAliasUpdate(BaseModel):
    """PATCH: ``canonical`` only. ``pattern`` is the row's identity (part of its
    unique key) and is never edited -- delete and recreate instead."""

    canonical: MerchantInput
