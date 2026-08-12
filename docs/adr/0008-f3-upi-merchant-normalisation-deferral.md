# ADR-0008: F3 UPI merchant normalisation — measured non-convergence, deferred with a trigger

**Status**: Superseded — see [ADR-0011](0011-merchant-alias-layer.md)
**Date**: 2026-08-03

## Problem

The limitation is already documented. What is new is its **magnitude**, and the fact that the PRD's own escalation trigger has fired on the user's primary real-world statement type.

**The normaliser does exactly what it advertises.** `app/services/merchant.py:40-42` is `" ".join(merchant_raw.lower().split())` — lowercase plus whitespace collapse, nothing else. No digit stripping, no prefix set, no reference removal. Its docstring says "v1 is intentionally minimal". **This is not a bug.**

**Why UPI defeats it, from the code rather than from assumption.** The function removes *nothing variable*. A UPI credit-card descriptor has the shape `fixed prefix + per-transaction reference + counterparty handle`, so every varying character survives into the key and each row mints a unique one. Learning needs a repeating key; there isn't one.

**Measured on real statements** (shape and counts only, no merchant strings recorded):

- One card: **79 rows across 49 distinct keys**, multiplicity `{1: 35, 2: 8, 3: 3, 4: 1, 7: 1, 8: 1}`.
- **35 of 49 merchants are singletons** — they can never reach `LABEL_PREFILL_MIN` or `CONFIDENT_MIN` (both 3), and several are the same real payee under a different reference.
- The two cards' key sets intersect at **0**, so a second card learns nothing from the first.
- The learner itself is healthy: a controlled re-import of an already-learned statement prefilled 15/79 categories and 8/79 tags. The 7× and 8× merchants *did* learn. **The 0% is the key, not the learner.**

**The failure mode is not the one the PRD models.** `PRD.md` §Success-metrics sets tagging precision at ≥80% after 3 months and says "below this, fuzzy/rules need to come forward" — framing the risk as *gradual decay over months*. What actually happens on UPI-heavy rows is **structural non-convergence**: 0% at any usage volume, forever, because the key can never repeat. That silently undercuts the "< 2 min import-to-categorised" loop the same section names as the core daily-driver metric.

**A fourth consumer that ADR-0006 does not name.** `merchant_normalized` is also the `GROUP BY` key for the top-merchants dashboard (`app/api/v1/dashboards.py:403-458`, backed by `ix_transactions_user_merchant_normalized`). So the same key problem degrades F8: the chart renders 49 one-off strings instead of a merchant ranking. Any future fix has to decide which key that dashboard groups on.

**One correction to the obvious fallback.** "Rely on `/settings/rules` pinning" does not work here. Pinning is merchant-**exact** (`PRD.md` §F3 — "Still merchant-**exact** — not the regex rules below"), so pinning a key that never repeats buys nothing. It helps only the ~14 keys that already learn on their own.

## Decision

**Do not change `normalize_merchant` in v1. Mitigate in the UI instead, and record the trigger that reopens this.**

**What this means concretely:**

- No regex stripping, no second normalisation key, no recompute migration in v1.
- The user-facing pain — a 79-row statement needing 79 manual categorisations — is a **review-queue** problem, and it is fixed there: select-all and range-select in the import review screen. The board already has bulk category assignment (`frontend/app/expenses/selection-bar.tsx:102`); the queue does not, and that gap is what makes the workload feel structural.
- The measurement above is recorded here precisely so it is not rediscovered from scratch in three months.

**The trigger that resolves this ADR** (whichever fires first):

- A real merchant corpus large enough to tune against. `PRD.md` §Anti-goals sets the bar: "Don't perfect the auto-tag normaliser regex before you have 500 real merchants to test against." Today's evidence is 79 rows from two statements.
- Tagging precision measured below the `PRD.md` §Success-metrics 80% bar via `GET /dashboards/tagging-stats` after 3 months of real use.

**When it fires, prefer the split-key shape.** Freeze `normalize_merchant` as the identity key and add a separate, more aggressive key for F3 / F3a learning (and decide the dashboard's grouping key at the same time), rather than widening `normalize_merchant` itself. The advantage is *decoupling*, not a large dedup-risk delta — ADR-0006's multiset difference absorbs most of the coarsening — but two residual risks do disappear: F2 manual entry 409-ing on genuinely distinct same-day, same-amount payments to different collapsed payees, and the irreversibility of a merged `hit_count`. The cost is honest: it is *more* code than changing the one function (a new column plus backfill, both map tables, and the lookup sites across `tag_service`, `merchant_labels`, `rules.py` and the import prefill), in exchange for permanently defusing the CHANGE HAZARD.

**What not to do:**

- Do not tune a stripping regex against the current sample. A regex that over-collapses merges two real payees into one learned rule, and the merge is irreversible: [ADR-0006](0006-f4-dedup-key.md) §Recompute (c) requires summing `hit_count`, and a summed count cannot be un-summed by a downgrade.
- Do not treat this as a defect against the learner. F3/F3a learning was verified end-to-end and is correct; regressions there would be worse than the gap this ADR defers.
- Do not "fix" it by lowering `CONFIDENT_MIN` / `LABEL_PREFILL_MIN`. Singleton keys never repeat at *any* threshold, so this buys nothing and weakens the thresholds for merchants that do repeat.

**Implementation approach:** none in v1 — that is the decision. When the trigger fires, the work is gated by ADR-0006 §Recompute procedure, and the shape above (split key) is the recommended starting point rather than a settled design; reopen this ADR and move it to Accepted or Superseded.

---

## Trade-offs

**Benefits:** zero data risk and zero migration now; the regex gets designed against a corpus large enough to validate it; the actual user pain is addressed at a fraction of the cost by bulk selection in the review queue; and `PRD.md`'s own anti-goal is respected rather than quietly overruled by a single unrepresentative sample.

**Drawbacks:** F3 stays near 0% on UPI-heavy statements for v1, so imported UPI rows are categorised by hand; the top-merchants chart stays unusable for the same rows; and the "< 2 min import-to-categorised" metric will not be met on those statements.

**Mitigation:** review-queue bulk selection collapses the per-row cost, which is where the time actually goes. The 14 repeating keys continue to learn and prefill normally, so the gap is bounded to genuinely one-off descriptors.

## Alternatives Considered

**Alternative 1 — strip references inside `normalize_merchant` now**: rejected. It is squarely against `PRD.md` §Anti-goals ("don't perfect the normaliser regex before you have 500 real merchants"), it would be tuned on 79 rows from two statements, and a wrong merge of learned history is irreversible.

**Alternative 2 — split the identity and learning keys now**: rejected as premature. It is the right end state and is named above as the preferred shape *when the trigger fires*, but today it is more total work than the deferral it would replace, for a regex nobody can yet validate (`CLAUDE.md` §2, simplicity first).

**Alternative 3 — rely on `/settings/rules` pinning as the v1 workaround**: rejected on the facts. Pinning is merchant-exact, so it cannot help a key that never repeats.

**Alternative 4 — lower the confidence / prefill thresholds**: rejected. Singleton keys repeat at no threshold; this weakens the signal for merchants that do repeat and fixes nothing.

## Consequences

- `PRD.md` §F3's "Future: stripping trailing transaction IDs / dates / reference numbers via regex" stays accurate and stays future. This ADR adds the measurement and the trigger, not a change of plan.
- Assembling the tuning corpus when the trigger fires does **not** require the `scripts/redact_fixture.py` pipeline: a VPA is a payment handle, not one of the identifiers `PRD.md` §Production-grade essentials requires masking (PAN, account number, card last-4). That standing rule is unchanged — none of PAN, account number or card last-4 belongs in a log, a fixture, or an error event.
- The top-merchants dashboard is a second beneficiary whenever this is reopened; whichever key it groups on is part of that decision, not a separate one. **Correction (2026-08-12):** false for [ADR-0011](0011-merchant-alias-layer.md)'s Stage A specifically — `app/api/v1/dashboards.py` still groups the top-merchants chart on `Transaction.merchant_normalized`, unchanged by Stage A, since Stage A never touches a transaction's stored key. This chart is not a beneficiary of Stage A; regrouping it onto `merchant_canonical` (joining through the alias resolver at query time, or persisting the canonical) remains an open, later decision.

## Superseding note (2026-08-12)

[ADR-0011](0011-merchant-alias-layer.md) ships the split-key shape this ADR's own Decision section named as the preferred fallback ("when it fires, prefer the split-key shape"): a separate `merchant_alias` table resolves a canonical key downstream of `normalize_merchant`, and F3/F3a re-key on it at read time. This routes *around* the deferral above rather than reversing it — `normalize_merchant` itself is still frozen, which is what this ADR actually decided. The trigger recorded in §Decision (a real merchant corpus, or tagging precision measured below 80%) did not need to fire; ADR-0011's decision 11 found the ≥80% bar itself was not yet computable, which was reason enough to build the cheaper, non-regex route now instead of waiting on either trigger.
