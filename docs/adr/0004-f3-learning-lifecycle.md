# ADR-0004: A tag rule is taught once per user decision, and only by a user decision

**Status**: Accepted
**Date**: 2026-07-19 (decided) · **Revised**: 2026-11-17
**Scope**: PRD §F3, §Success metrics · `app/services/tag_service.py` (`should_learn_tag`,
`AUTO_TAGGABLE_TYPES`, `record_tag`, `prefetch_tag_map`), `app/services/merchant_labels.py`
(`learn_merchant_memory`), `app/api/v1/transactions.py`, `app/api/v1/imports.py`
(`commit_import_batch`), `app/api/v1/dashboards.py` (`tagging_stats`), `app/services/demo_seed.py`
**Supersedes / Related**: ADR-0011 (canonical merchant), ADR-0012 (fallback, archive), ADR-0009

## Decision

A [tag rule](../../GLOSSARY.md#auto-tagging) is keyed on the user's canonical merchant and is
[taught](../../GLOSSARY.md#auto-tagging) exactly once per real user decision, at one site per row
lifecycle. Staged, discarded, defaulted and non-spend data never teach. The acceptance metric and
the rule count measure current health: rules and suggestions pointing at archived categories drop out.

## Rules

1. **Every teach goes through one gate**: `learn_merchant_memory` → `should_learn_tag` (a non-empty
   canonical merchant and `transaction_type` in `AUTO_TAGGABLE_TYPES = {"spend"}`). The rule key is the
   [canonical merchant](../../GLOSSARY.md#transactions--import) (ADR-0011), never the raw
   `merchant_normalized`. — *Enforced by*: service ·
   *Locked by*: `tests/services/test_tag_service.py::test_should_learn_tag`,
   `::test_learn_writes_the_canonical_key`
2. **There are exactly three teach sites, all user decisions:**
   a. F2 manual POST with a non-null category;
   b. a categorisation of [board](../../GLOSSARY.md#auto-tagging) rows (`confirmed_at IS NOT NULL`)
      whose category actually changes — a single PATCH, a bulk gesture, or "apply to past rows";
   c. import commit, the only teach site for staged rows, where a
      [passive accept](../../GLOSSARY.md#auto-tagging) counts.
   — *Enforced by*: service · *Locked by*: `tests/api/test_transactions.py::test_post_with_category_sets_tag_map`,
   `::test_patch_category_records_new_tag`, `::test_patch_same_category_does_not_bump_hit_count`,
   `tests/api/test_imports_review.py::test_patched_pending_row_learns_once_at_commit`,
   `::test_e2e_learn_then_prefill_and_passive_accept`
3. **A PATCH on a staged row never teaches**, so discarding or cancelling it cannot leave a rule
   behind, and review-queue confidence never counts the user's own in-session edit. — *Enforced by*:
   service · *Locked by*: `tests/api/test_imports_review.py::test_patch_pending_row_does_not_learn`,
   `::test_e2e_discarded_pending_row_does_not_teach`, `::test_candidates_confidence_not_self_inflated_by_own_patch`
4. **One gesture teaches once per distinct canonical merchant.** A bulk re-categorise of board rows, or
   "apply to past rows", is one atomic request (all rows or none) and adds +1 per merchant, however
   many rows it touches. "Apply to past rows" offers only recent rows still on a machine-assigned
   category (uncategorised, the fallback "Other", or the row's own auto suggestion), never one the user
   put elsewhere. — *Enforced by*: service (bulk endpoint) ·
   *Locked by*: `tests/api/test_transactions.py::test_bulk_categorise_teaches_once_per_merchant`,
   `::test_apply_to_past_rows_teaches_once`, `::test_past_rows_skip_hand_categorised_rows`,
   `frontend/app/expenses/selection-bar.test.tsx`, `frontend/app/expenses/expenses-board.test.tsx`
5. **A row the server defaulted at commit does not teach a category.** A spend row whose category is
   null or archived at commit lands on the spend [fallback category](../../GLOSSARY.md#categories)
   ("Other", ADR-0012, which always exists); its labels still teach. A cashback credit committed still
   in the spend "Cashback" its merchant text pre-filled (ADR-0013) is a server default too. —
   *Enforced by*: service ·
   *Locked by*: `tests/api/test_imports_review.py::test_commit_defaults_archived_category_row_to_other`,
   `::test_commit_keeps_the_cashback_prefill_and_does_not_teach_it`
6. **Income and transfer rows never teach**, judged on the type after the F4a flip at commit and after
   the update at PATCH. — *Enforced by*: service ·
   *Locked by*: `tests/api/test_transactions.py::test_post_income_with_category_does_not_learn`,
   `tests/api/test_imports_review.py::test_commit_f4a_does_not_learn_tag_for_flipped_row`
7. **N teaches add exactly N to `hit_count`, never fewer**, including N same-rule teaches in one commit
   and concurrent commits on Postgres. The increment is a single atomic SQL statement (upsert), portable
   to both dialects (ADR-0001). — *Enforced by*: DB unique constraint + service ·
   *Locked by*: `tests/api/test_imports_review.py::test_commit_same_merchant_twice_bumps_hit_count_per_row`,
   `tests/services/test_tag_service.py::test_record_tag_repeated_bumps_accumulate`,
   `::test_record_tag_handles_concurrent_insert_race`, `::test_record_tag_increment_is_a_single_statement`
8. **Prefill never suggests an archived category, nor any category on a flagged card-bill debit**
   (ADR-0002 rule 7b); the rule rows survive the archive. — *Enforced by*: service · *Locked by*:
   `tests/services/test_tag_service.py::test_prefetch_skips_archived_category`,
   `tests/api/test_imports_review.py::test_import_never_prefills_a_category_onto_a_card_bill_debit`
9. **[Acceptance rate](../../GLOSSARY.md#auto-tagging) = kept / suggested over board rows whose frozen
   `auto_category_id` inner-joins a live category of the same user.** The join key is the suggestion,
   never the final `category_id`; a reject to a since-archived category stays counted. `None` at zero.
   — *Enforced by*: query · *Locked by*: `tests/api/test_dashboards.py::test_tagging_stats_excludes_archived_suggestion_rows`,
   `::test_tagging_stats_reject_to_since_archived_stays_counted`
10. **`rules_count` counts taught rules (non-seeded, per ADR-0011 rule 11) on live categories only.** — *Enforced by*: query ·
   *Locked by*: `tests/api/test_dashboards.py::test_tagging_stats_rules_count_excludes_zero_hit_rows`,
   `::test_tagging_stats_rules_count_excludes_archived_categories`,
   `::test_tagging_stats_rules_count_counts_pinned_seed_rows`
11. **Spend-by-category keeps archived categories** (outer join). The asymmetry with rules 9–10 is
   intended. — *Enforced by*: query ·
   *Locked by*: `tests/api/test_dashboards.py::test_archived_category_surfaces_with_stored_name`
12. **Seeding writes rule rows directly and never teaches.** The seed dictionary (ADR-0011) and the
   demo seeder insert `merchant_tag_map` rows with chosen hit counts. — *Enforced by*: service ·
   *Locked by*: `tests/services/test_demo_seed.py::test_seeder_never_calls_learn_merchant_memory`,
   `::test_reseed_does_not_inflate_rule_hit_counts`

## Out of bounds

- Deleting a transaction never un-learns, and rules do not decay in v1.
- Income and transfer rows are classified by hand; F3 does not suggest for them.
- Which canonical merchant a descriptor resolves to: ADR-0011.

## Why

"Learn on every touch" is the intuitive default, and it shipped five bugs. One in-queue edit taught
twice and crossed `CONFIDENT_MIN` a batch early. A PATCH on a staged row left a rule behind after a
discard. Commit re-taught a category archived mid-review. The confidence chip counted the user's own
edit. The metric read green for dead categories. Each fix is one rule above.

`hit_count` drives [confident rules](../../GLOSSARY.md#auto-tagging), so it must count independent
judgements. Five Swiggy rows re-categorised in one gesture are one judgement, not five. A rule that
can never prefill again, because its category is archived, is not a rule the user has.

## Alternatives rejected

- **Teach on every PATCH, staged rows included** — double-teaches and orphans rules from discarded work.
- **Final-state metric** — archiving never retracts a past accept, so stale green votes accumulate.
- **Python `+= 1` read-modify-write** — loses bumps under Postgres concurrency.
- **Bulk gesture = N teaches** — one click makes a rule confident.
- **Seeder calls the teach function** — a fourth teach site that is not a user decision.
- **422 on an archived category at commit** — hostile mid-review; default to "Other" instead.

## Consequences

- `hit_count` means "distinct user decisions", so `CONFIDENT_MIN` behaves.
- The frontend sends one bulk request where it used to send N PATCHes.
- Reopen if learning needs decay, or if income gets auto-tagging.

## History

- 2026-07-19 — Accepted after five learning bugs: one teach per decision, current-health metric.
- 2026-11-15 — Rewritten to the rules-first template. Rules key on the canonical merchant. The atomic
  increment is no longer deferred to v2. A bulk gesture or "apply to past rows" teaches once per
  merchant. `rules_count` uses current-health semantics. The demo seeder writes rows instead of
  teaching. The "no active Other → 422" path is gone, because the fallback is unarchivable (ADR-0012).
- 2026-11-15 — Amended rule 5: a cashback credit committed still in its keyword-prefilled Cashback
  category does not teach, since the server chose it, not the user (ADR-0013).
- 2026-11-17 — Amended rule 8: prefill never puts a category on a flagged card-bill debit, so a rule learned from one bill never decides the next (ADR-0002 rule 7b, A76).
