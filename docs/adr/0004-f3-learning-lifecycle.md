# ADR-0004: F3 auto-tag learning lifecycle — one teach per decision, current-health metric

**Status**: Accepted
**Date**: 2026-07-19

## Problem

F3 auto-tagging ([PRD §F3](../../PRD.md)) maintains a `merchant_tag_map`
(`user_id, merchant_normalized, category_id, hit_count, last_used`) that the import
pipeline reads to prefill categories. The plumbing (savepoint recovery, frozen-suggestion
metric, cross-user scoping) was sound, but *when and how often a decision gets taught* had
several genuine bugs — and they're re-litigation-prone because "learn on every user
touch" is the intuitive-but-wrong default. Concretely, on the pre-fix build:

- **Double-teach.** One in-queue category edit taught the tag **twice** — the review-queue
  PATCH bumped `hit_count`, then commit pass-3 bumped it again — so a corrected merchant
  crossed the `CONFIDENT_MIN=3` threshold a full batch early.
- **Orphan from discarded work.** A PATCH on a **pending** review-queue row wrote a map row
  immediately; discarding the row (or cancelling the batch) left that map row behind →
  future imports auto-tagged from a decision the user threw away.
- **Zombie archived bucket.** Commit pass-3 re-taught a category the user **archived**
  mid-review, resurrecting a live map row pointing at a dead bucket and inflating
  `rules_count`.
- **Self-vote.** The review-queue `confidence` / `prior_matches` chip counted the user's own
  just-made PATCH as if it were prior history.
- **Metric reads green for dead buckets.** `GET /dashboards/tagging-stats` counted a row
  whose frozen `auto_category_id` pointed at a since-archived category as "kept" forever
  (unchanged) — or, after the reconciliation fix below re-buckets it to *Other*, as a
  spurious "not-kept" that dragged the rate *down*.

`CLAUDE.md` is gitignored in this environment, so the contract can't live there; this ADR is
its committed home.

## Decision

**`record_tag` fires once per real user decision, at one authoritative site per lifecycle;
data the user hasn't committed (or has discarded / archived) never teaches; the acceptance
metric reflects current tagging health.**

**Where a decision is taught (and only there):**
- **F2 manual POST** with a non-null category — immediate user-yes.
- **PATCH `category_id` on a *board* row** (`confirmed_at IS NOT NULL`) when the value
  actually changed — the post-board correction. A PATCH on a **pending** review-queue row
  does **not** teach.
- **Bulk commit** of an import batch (pass-3) — each committed row is a user-yes (passive
  accept counts), which is the **sole** learning site for pending rows.

**What must not teach:**
- Pending review-queue PATCHes (they learn at commit instead) — so discarding / cancelling
  a staged row can't orphan a map entry, and the confidence chip can't count a self-vote.
- Commit rows **defaulted to *Other*** because their category was null or pointed at an
  archived/absent bucket at commit time (F4a reconciliation, tracked in `defaulted_ids`) —
  so an archived bucket is never re-learned, and the board row lands on *Other* rather than a
  dead bucket.
- `income` / `transfer` rows (hand-classified in a separate taxonomy; `AUTO_TAGGABLE_TYPES`
  gates every site).

**Acceptance metric — current-health semantics.** `tagging_stats`
([dashboards.py](../../backend/app/api/v1/dashboards.py)) **inner-joins `Category` on
`auto_category_id`** filtered to the user's live categories, so rows whose frozen suggestion
points at a since-archived category leave **both** numerator and denominator. The join is
keyed on `auto_category_id` (the suggestion), **never** the final `category_id` — a row the
user changed to a since-archived bucket is a genuine reject and must stay counted as
not-kept.

**Implementation approach:** the shared `should_learn_tag(...)` predicate
([tag_service.py](../../backend/app/services/tag_service.py)) is the eligibility core at all
sites; each site `and`s its extras (PATCH: confirmed + changed; commit: not-Other-defaulted).
`record_tag` keeps its SELECT-then-INSERT-or-UPDATE with a `begin_nested()` savepoint for the
concurrent-insert race, narrowing its `IntegrityError` catch to the
`uq_merchant_tag_map_user_merchant_category` conflict.

---

## Trade-offs

**Benefits:** one teach per decision makes `hit_count` mean "distinct user-yeses", so the
`CONFIDENT_MIN` threshold behaves; discarded / archived data no longer corrupts future
auto-tags; the metric tracks live health instead of accumulating stale green votes for dead
buckets.

**Drawbacks / accepted asymmetries:**
- **`hit_count += 1` is a non-atomic read-modify-write** — correct on SQLite's single writer,
  but lost-update-prone under Postgres concurrency. Deferred (below); the naive SQL-atomic
  form (`hit_count = MerchantTagMap.hit_count + 1`) is **not** an acceptable stopgap: under
  `autoflush=False` it's a deferred, idempotent expression, so several same-`(merchant,
  category)` bumps in one commit pass collapse to `+1` instead of `+N`. Two tests lock the
  `+N` behavior so the collapse can't be reintroduced.
- **The metric deliberately filters `archived_at IS NULL`, whereas spend-by-category
  deliberately keeps archived buckets** (outer join, no archived filter — see
  [dashboards.py](../../backend/app/api/v1/dashboards.py)). This is intentional: they answer
  different questions — "where did money go, ever" (keep the history) vs. "is auto-tagging
  healthy *now*" (drop dead buckets). Do **not** "fix" one to match the other.
- **Archived-category default-to-Other is spend/refund-only** (mirrors the null-category
  fallback). An `income` / `transfer` row whose category is archived mid-review still commits
  pointing at the archived bucket — cosmetic only, since those types never feed `record_tag`.

**Mitigation:** the atomic-`hit_count` upgrade has a defined home (below); the metric
asymmetry and the spend/refund-only scope are documented here and in the route docstring so a
future reader doesn't undo them.

## Deferred to Postgres v2

- **Atomic `hit_count`** via native `INSERT … ON CONFLICT
  (uq_merchant_tag_map_user_merchant_category) DO UPDATE SET hit_count =
  merchant_tag_map.hit_count + 1` — atomic *and* correct per statement, which also retires
  the savepoint dance in `record_tag`. Until then keep `+= 1`.

## Alternatives Considered

**Alternative 1 — Learn on every PATCH (including pending rows).** The intuitive default, but
it double-teaches corrected rows and orphans map rows from discarded work. Rejected: teach at
one authoritative site per lifecycle instead.

**Alternative 2 — Final-state metric (archiving never retracts a past accept).** Simpler
(no join), but leaves stale green votes for dead buckets and lets the F4a re-bucket-to-Other
noise drag the rate down. Rejected in favour of current-health semantics.

**Alternative 3 — Ship the SQL-atomic `hit_count` bump in v1.** Fixes the Postgres
lost-update, but regresses v1 by collapsing same-triple bumps within one `autoflush=False`
commit pass to `+1`. Rejected; deferred to the v2 `ON CONFLICT` rewrite.

**Alternative 4 — Block the archived-bucket commit instead of defaulting to Other.** A 422
mid-review is hostile UX for a self-host single user. Rejected: default to *Other* (mirrors
the existing null-category fallback) and skip learning.
