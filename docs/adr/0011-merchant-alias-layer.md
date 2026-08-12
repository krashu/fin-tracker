# ADR-0011: Merchant alias / canonicalisation layer + seeded Indian merchant dictionary

**Status**: Accepted
**Date**: 2026-08-12

## Problem

[ADR-0008](0008-f3-upi-merchant-normalisation-deferral.md) measured the shape of the problem and
deferred fixing it: `normalize_merchant()` is lowercase + whitespace-collapse only, and it removes
*nothing variable*. On one real card statement, 79 rows produced 49 distinct keys — 35 of them
singletons that can never reach `CONFIDENT_MIN` / `LABEL_PREFILL_MIN` (both 3) — and two cards'
key sets intersected at **zero**, so a second card learns nothing from the first. The learner
itself is healthy (a controlled re-import prefilled 15/79 categories and 8/79 tags on the merchants
that *did* repeat); the key is wrong. F3's miss rate is **100% by construction** on any merchant
whose descriptor embeds an order id, RRN, auth code or VPA — most Indian CC and UPI rows.

A second, distinct failure: F3 starts at literally 0% on a first import. India has no Plaid-style
merchant taxonomy to seed from, so there is no cold-start data source short of hand-building one.

A third failure, procedural rather than behavioural: `GET /dashboards/tagging-stats` measured only
**acceptance** (of what we auto-tagged, how much did the user keep?), with no denominator of all
imported rows. `PRD.md` §Success-metrics' ≥80% *pre-tag* bar — the bar this whole arc is justified
against — was not computable before this arc's Phase A0 added `coverage_rate`.

## Decision

**A third string layer, downstream of `normalize_merchant`, resolved at read time — not a
classifier and not a richer rules engine.**

```
merchant_raw -> normalize_merchant() -> merchant_normalized   [frozen — feeds the F4 fingerprint]
                                              |
                                      resolve_alias() -> merchant_canonical   [F3/F3a key]
```

**What this means concretely:**

- `app/models/merchant_alias.py` — a per-user `merchant_alias` table: `(user_id, pattern,
  canonical, is_seeded)`, unique on `(user_id, pattern)`. `normalize_merchant()` is never modified;
  both `pattern` and `canonical` are normalized *at* the API boundary, not inside the frozen
  function.
- `app/services/merchant_alias.py::AliasResolver` — **token-boundary contains matching**. Both the
  alias pattern and the merchant string are split on runs of non-alphanumerics (`tokenize`), and a
  pattern matches when its token sequence is a *contiguous subsequence* of the merchant's tokens.
  `('swiggy',)` matches `('upi', 'swiggy', '9876', 'ybl')`; `('ola',)` does **not** match
  `('chocolate', 'hut')` — that asymmetry is why this mode was chosen over a raw substring `in`
  check, since a false merge into a canonical is irreversible.
- **Longest-pattern-wins**, a total order derived from the data:
  `(token count DESC, char length DESC, pattern ASC)`. Not a user-editable priority field — that
  would be research §7's killed rules-engine expansion.
- **Resolution is single-pass.** `canonical()` applies at most one alias and never chains a
  canonical into another pattern. A cascade is not a feature deferred; it is a bug class, rejected
  at the `/rules/aliases` write boundary.
- **Identity is the default.** A merchant matching no alias resolves to itself, which is what makes
  "no aliases → byte-identical behaviour" a proven regression test rather than an assertion.
- **The zero-token hazard.** `normalize_merchant("***")` returns `"***"` (non-blank), but
  `tokenize("***")` returns `()` — and an empty tuple is a contiguous subsequence of *every*
  sequence, so an unfiltered zero-token pattern would match every merchant and, sorted last under
  longest-pattern-wins, fire on exactly the merchants nothing else matched. Two guards, both
  required: `load_alias_resolver` skips zero-token rows, and `POST`/`PATCH /rules/aliases` 422s on
  `tokenize(pattern) == ()`.
- **Per-user seed rows at registration**, mirroring `provisioning.py`'s seeded categories
  (`is_seeded=True`). Every owned table keeps `user_id` (ADR-0003 needs no new rule). ~96
  `(pattern, canonical, category)` entries covering the merchants research §13.6 identified
  (Swiggy/Zomato→Food, Blinkit/Zepto/BigBasket→Groceries, Uber/Ola/Rapido→Transport, etc.), each
  contributing a seeded `merchant_alias` row **and** a `merchant_tag_map` row at `hit_count = 0`.
  A backfill migration (`0032_seed_merchant_dictionary`) seeds existing users; the register-time
  path and the migration both **skip** — never merge, never bump — an existing
  `(user_id, merchant_normalized, category_id)` row, since the demo user's seed data already
  teaches several of the same merchants.
- **`hit_count == 0` is the seeded marker everywhere one is needed** — no new column on
  `merchant_tag_map`. Learning never writes 0 (`default=1`; `record_tag` bumps; `pin_tag` creates
  at 1), so `hit_count == 0` ⟺ *seeded and never confirmed*.
- **Writes stay on the raw key in Stage A, deliberately.** `record_tag` / `record_label` keep
  writing `merchant_normalized`; only the *read* side re-keys onto the canonical. The map grows one
  row per raw descriptor, and the aggregate sums them at read time — that is the desired learning
  behaviour, not a workaround.
- **Stage A ships with zero migration to the hot map tables.** An optional Stage B (re-key stored
  rows onto the canonical, deferred, gated on a measured `coverage_rate` before/after) is a
  compaction, not a removal — the read-time aggregator stays permanently, since an alias created
  after consolidation still fans out over rows written before it.

**What not to do:**

- Do not widen `normalize_merchant` itself, native or otherwise — that is precisely the recompute
  migration this design exists to avoid ([ADR-0006](0006-f4-dedup-key.md) §Recompute procedure).
- Do not add a user-editable rule-priority integer, or a condition on amount, account or date —
  research §7's killed rules-engine expansion, re-checked against this design at research §13.7
  without collision.
- Do not tune a raw substring/regex matcher against an untuned sample — the same irreversibility
  argument ADR-0008 made against a stripping regex applies here.

**Implementation approach:** seven phases (A0–A6), detailed in the (untracked) execution brief.
Phase A0 makes the bar measurable first; A1 lands the model/resolver with no caller (behaviour
unchanged); A2 re-keys the two prefetches with a byte-identity regression test; A3 fixes the two
read sites that would otherwise contradict the real prefill; A4 ships `/rules/aliases` CRUD; A5
ships the seed dictionary; A6 is this document.

---

## Trade-offs

**Benefits:** no migration to `merchant_tag_map` / `merchant_label_map` in Stage A; the output is
provably byte-identical to pre-alias behaviour when the alias table is empty (an empty resolver
makes `canonical(m) == m`, and the map's UNIQUE constraint guarantees exactly one row per
aggregate group, so the Python winner-pick reproduces the old SQL `ORDER BY` exactly); the cold
start is fixed by the seed dictionary instead of a classifier; and the ≥80% pre-tag bar is finally
measurable via `coverage_rate`.

**Drawbacks:** the map grows one row per raw descriptor in Stage A, and a seeded row is never
bumped by `record_tag` for the merchants this feature exists for (confirming `swiggy blr 12345`
writes a *new* raw row at 1; the seed row under canonical `swiggy` stays at 0 forever — the
aggregate is still correct, 0 + 1 = 1). `_unpin_sibling_tags`'s one-pinned-per-merchant invariant
is only approximate until Stage B: two raw merchants under one canonical could each pin a different
category, making "any pinned" true for both, falling through to a `hit_count` tiebreak — a soft
ambiguity, not a crash. A wrong alias merge into a canonical is irreversible
([ADR-0006](0006-f4-dedup-key.md) §Recompute (c) cannot un-sum a `hit_count`).

**Mitigation:** the zero-token 422 and the duplicate/conflict validation at the `/rules/aliases`
write boundary catch the highest-risk merges before they land. Stage B, if authorised, resolves the
pin ambiguity and stops the raw-row fan-out; it is not required for Stage A's benefits to hold.

## Alternatives Considered

**Alternative 1 — raw substring match (`in`, not token-boundary)**: rejected. `ola` would
false-merge into `chocolate hut`; token boundaries are what make the contains-check safe, and the
same false-merge is exactly what a naive regex risked in ADR-0008.

**Alternative 2 — exact-set aliases (a fixed list of known variants per canonical)**: rejected. It
cannot fire on a reference it hasn't seen before — `swiggy*blr*99999` needs to match without
`99999` having been enumerated — which restates the problem rather than solving it.

**Alternative 3 — widen `normalize_merchant` itself**: rejected. It is the fingerprint's frozen key
and both memory maps' key; changing it triggers [ADR-0006](0006-f4-dedup-key.md)'s recompute
migration, which this design's entire point is to avoid.

**Alternative 4 — a rules engine (ordered rules, boolean logic, regex, amount conditions)**:
rejected. Research §7 kills this with cited evidence, and §13.7 re-checked the collision against
this design and found none — this is a normalisation change, not a rules engine.

## Consequences

Three semantic changes this arc makes that nothing else documents:

1. **The archived-category filter now reaches `list_candidates`.** Dropping its `MerchantTagMap`
   LEFT JOIN in favour of `prefetch_tag_strength` inherits `prefetch_tag_map`'s archived-category
   filter (Phase A3) — a candidate pointing at a since-archived category now reads confidence
   `"none"` instead of showing `prior_matches`. Intentional, and more consistent with the real
   prefill than the old `NULL = NULL` join semantics were.
2. **`CONFIDENT_MIN` / `LABEL_PREFILL_MIN` change meaning** from "you confirmed *this exact
   descriptor* 3×" to "you confirmed *this merchant* 3×" — intended for F3a labels, where three
   `swiggy*x` rows at 1 each now cross the bar as canonical `swiggy` at 3; the F3 category
   confidence tint inherits the same re-meaning as a side effect.
3. **`merchant_tag_map.hit_count == 0` becomes load-bearing** as the seeded-and-never-confirmed
   marker (decision 4 of the execution brief) everywhere a read site needs to distinguish "seeded"
   from "learned": the `"seeded"` confidence state, the `/settings/rules` `seeded` field, and the
   `rules_count` exclusion.

Two decisions from the execution brief are recorded here as **deferred with a trigger, not built**:

- **Rename-to-rule UX** (an inline "apply this rename to future imports" prompt) — deferred past
  Stage B. Alias authoring lives on `/settings/rules` only, one explicit surface; a second
  authoring path is the item most likely to drift into research §7's killed list.
- **Retroactive apply** (rewriting a committed transaction's category when a new alias is added) —
  flagged, not proposed, per research §13.7. Read-time resolution already means a *future* import
  benefits from a new alias immediately without rewriting history; that is not retroactive apply,
  and no phase of this arc changes a committed row's category.
