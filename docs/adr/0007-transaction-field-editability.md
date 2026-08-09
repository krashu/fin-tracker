# ADR-0007: Transaction field editability on PATCH — the mutable set, identity recompute, and `origin_fingerprint`

**Status**: Accepted
**Date**: 2026-08-03

## Problem

**A transaction row reads as an ordinary editable record and is not one.** Four of its seven user-visible fields are read-only, with no on-screen reason, because they happen to be the [ADR-0006](0006-f4-dedup-key.md) hash inputs. The user's mental model — every finance app lets you correct a record — is right; the constraint they hit is an internal one that leaked out as a product rule.

The 2026-08-02 manual UI test reported this three separate times, from three angles, all rooted in the same two-field schema:

- a credit mis-classified `income` that cannot be re-typed to `refund`,
- a transfer row that cannot be corrected,
- amount / date / merchant / account rendered as static text in the edit dialog.

Litigating them as three tickets produces three inconsistent answers, so this ADR scopes the question **once**.

**What the code says today:**

- `TransactionUpdate` declares `labels` and `category_id` only (`app/schemas/transactions.py:195-229`) with `model_config = ConfigDict(extra="forbid")` at `:221`, so any other key 422s.
- The immutability is deliberate, not an omission: `app/api/v1/transactions.py:483` and `:521` hard-code `transaction_type` immutable in comments, and `frontend/app/expenses/transaction-dialog.tsx:6-9` states the reason verbatim — editing those fields "would recompute the PRD §F4 fingerprint and re-run sign validation, deliberately out of scope."
- ADR-0006 rule 1 confirms the four are the entire identity tuple: `sha256("\x1f".join(date_iso, amount_paise, normalized_merchant, account_id))`.

**At least one field must be editable on correctness grounds alone.** The parser cannot distinguish a refund from a cashback credit, ever. `_REFUND_RE` (`app/parsers/axis_cc.py:42`, `REFUND|REVERSAL|CHARGEBACK`) falls through `_classify` (`:54-63`) to `"other"`, and `app/services/import_service.py:366` then maps an unmatched credit to `income`. But cashback is a legitimate **income** category ([`PRD.md`](../../PRD.md) §F5) while a merchant refund is spend-negating — only the user knows which one a given credit is.

**And the type is load-bearing, contradicting the PRD.** `PRD.md` §F4a-3 justifies keyword-only classification by asserting "the type is informational only; reporting math is sign-based regardless." That is false in this codebase: every aggregate filters `transaction_type.in_(("spend", "refund"))` and routes `income` to a separate bucket (`app/api/v1/dashboards.py:151-168`, and the same filter at `:235, 338, 442, 562, 707, 843`). A refund typed `income` never enters the expense sum, so displayed spend is inflated by the refund's full magnitude.

### The edge case that shapes the decision

Widening PATCH naively breaks re-import. Statement `S` on account `A`, rows `R1`–`R10`:

1. Import `S` → batch `B`, ten rows staged pending at `occurrence 0`.
2. Commit `R1`–`R6`, cancel the rest. `DELETE /imports/{batch_id}` (`app/api/v1/imports.py:609-615`) hard-deletes the four pending rows; `remaining = 6 > 0`, so batch `B` survives as `completed`.
3. Edit `R1`'s amount → its fingerprint changes from `fp1` to `fp1'`.
4. Re-import `S`. The file hash matches a completed batch, so there is **no short-circuit** — the importer reconciles (`app/services/import_service.py:30-38`), and its dedup prefetch groups by fingerprint over the statement's date span with no `confirmed_at` filter (`:205-221`).
   - `R2`–`R6` are present → skipped.
   - `R7`–`R10` are absent → re-staged. **Intended**: cancel is not a tombstone (`:33-34`).
   - **`R1` is also re-staged**, because the DB holds `fp1'` and the file yields `fp1`.

The review queue now shows the four deliberately-cancelled rows *plus the row the user corrected*, wearing its original wrong value and visually indistinguishable from the other four. Commit them and the board holds both `R1'` and `R1` — two rows for one real transaction, with different fingerprints **by construction**, so F4 can never detect it and signed sums double-count.

That is strictly worse than the delete case (delete + re-upload restores one row, net zero), and the likeliest everyday use of merchant editing — cleaning up an unreadable UPI descriptor — is exactly its trigger.

## Decision

**Every user-visible column on a transaction is editable, because the dedup identity is an implementation detail that must never surface as a UI constraint. Identity edits recompute `fingerprint` and re-enter the F4 uniqueness contract at `occurrence = 0`, while a new immutable `origin_fingerprint` preserves which statement line produced the row — so an edit never resurrects its own pre-edit version on re-import.**

The principle comes before the mechanics: it is what pre-decides the next "why can't I edit X?" instead of answering it a fourth time. The ten rules below are how the codebase pays for it.

**Ten rules:**

1. **The mutable set is `date`, `amount_paise`, `merchant_raw`, `account_id`, `transaction_type`, `category_id`, `labels`.** Server-managed and never accepted in the body: `fingerprint`, `occurrence`, `merchant_normalized`, `origin_fingerprint`, `source`, `import_batch_id`, `confirmed_at`, `auto_category_id`, and `transfer_pair_id` (owned by `POST /transfer`, F4a auto-link, and `POST /{id}/unlink` — [ADR-0002](0002-transfer-pair-id-semantics.md)).

   The line is drawn on a principle, not on convenience: the widening covers **what the user asserts about the money**, and stops at **what the system asserts about the row**. `source` is the sharp case — it records where the record originally came from, so a user-editable `source` would let a row claim a provenance it does not have. Same argument for `import_batch_id` and `confirmed_at`. `fingerprint` / `merchant_normalized` / `occurrence` are *derived*, not asserted; rule 3 recomputes them.

2. **Two cost classes, one endpoint.** `transaction_type`, `category_id` and `labels` are free — the type is *absent* from the ADR-0006 payload, so none of them touches the hash. Only the four identity columns trigger recompute. The route branches on whether an identity input **actually changed** (compare post-`exclude_unset` values against the stored row), so a no-op PATCH stays a no-op.

3. **Recompute rule.** On a real identity change: recompute `merchant_normalized` (reusing the create path's `None → ""` convention, `app/api/v1/transactions.py:239`), recompute `fingerprint` via `app/services/fingerprint.transaction_fingerprint`, **reset `occurrence = 0`**, and let the unique index adjudicate — `IntegrityError` → 409 `"transaction already exists"`. `origin_fingerprint` is **never** touched here; that is the whole point of rule 9.

   Resetting to 0 rather than carrying the ordinal is ADR-0006 rule 4's epistemics applied unchanged: a PATCH is a lone operation, it cannot count a file's multiset, so it proves nothing about distinctness and must *ask*, exactly like a lone `POST`. Carrying the old ordinal would silently land the row in a gap of the target group and defeat the double-submit guard. Vacating the old group may leave a gap, which is already correct — the allocator tracks `MAX`, not `COUNT` (`app/services/occurrence.py:74-78`).

   Reuse `_is_fingerprint_conflict` (`app/api/v1/transactions.py:177-189`) **unchanged**. ADR-0006 deliberately kept the index *name* when the constraint widened to four columns, and its SQLite branch is a subset test over `table.col` tokens, so the matcher still fires. Do not "fix" it.

4. **Sign and type are validated as a post-patch pair, in the route.** The F2 rule (`spend < 0`, `income > 0`, `refund > 0`, `transfer ≠ 0`) applies to the *merged* state, which `TransactionUpdate` cannot see — a schema validator has no access to the stored row. Extract the predicate into one shared helper called by both `TransactionCreate._check_sign` (`app/schemas/transactions.py:104-113`) and the route; that is a second concrete use, so it clears `CLAUDE.md` §2. No existing row is stranded by this: `RawTransaction.__post_init__` already enforces `purchase ≤ 0` and `payment` / `refund ≥ 0`, and zero-paise rows are filtered upstream.

5. **The category kind follows the post-patch type.** `_assert_category_id_or_422` currently reads the *pre*-patch type (`app/api/v1/transactions.py:485-490`); it must read the merged one. When the type flips kind (income ↔ spend-side), the same PATCH must supply a compatible `category_id` — or an explicit `null` — and 422s otherwise. **No silent clearing**: the frontend picker already filters by `categoryKindForType`, so one round-trip covers it, and clearing would destroy a choice the user made.

6. **An `account_id` change re-runs the create path's four account checks** — owned, non-archived, non-investment, INR (`app/api/v1/transactions.py:198-227`). A cross-user id is 422, never silently honoured (`backend/CLAUDE.md` tenant rule 3).

7. **Transfer guard.** A row with a non-null `transfer_pair_id` rejects edits to the four identity columns and to `transaction_type` → 422, unlink first (`POST /{id}/unlink` exists, `app/api/v1/transactions.py:611-664`); `category_id` and `labels` stay editable, since neither participates in the pairing. `transfer` is also not a valid PATCH *target* — pairs are born via `POST /transfer`, and a lone transfer minted by PATCH would have no second leg. Unpaired transfers remain legal (they are the survivors of delete and unlink), so this forbids only *creating* one this way — and it enables the relabel the DELETE docstring already anticipates at `:567`.

8. **Learning is unchanged** ([ADR-0004](0004-f3-learning-lifecycle.md), one teach per decision). Two consequences follow, and are accepted rather than patched with new teach sites: a `merchant_raw` edit does **not** retract the old merchant's learned rule (there is no decay in v1), and renaming a merchant while keeping the category teaches the *new* merchant nothing, because the existing `new_category_id != prev_category_id` gate holds. `auto_category_id` stays frozen on every edit — it is the import-time suggestion the acceptance metric measures. The learn block at `:532-539` already reads the post-`setattr` type, so only the stale comment at `:522` needs correcting.

9. **`origin_fingerprint` — provenance, kept separate from identity.** `String(64)`, **nullable**, written once by the statement importer to the same value as `fingerprint` at *stage* time, and never written again by anything. `NULL` means "no external source line": manual entry, both transfer legs, F4a, the demo seeder, backup CSV import.

   - **Both file-dedup prefetches key on `COALESCE(origin_fingerprint, fingerprint)`** — `import_service` (`:205-221`) and `backup_import_service`. This sits squarely inside the architecture `app/services/occurrence.py` already documents: each importer owns its own prefetch `SELECT` because "the key tuple … is a per-source decision". `OccurrenceAllocator` itself needs no change.
   - **A row carries both hashes, and for an imported row they are equal at birth** — they diverge only on an edit. `fingerprint` answers *what does this row say* (unique-constrained, recomputed); `origin_fingerprint` answers *which statement line produced it* (immutable, dedup-only).
   - **Do not stamp `origin_fingerprint` on manual rows, and do not "simplify" the `COALESCE` away.** This is the rule most likely to be misread as an inconsistency, so here is the case that decides it. A user logs a UPI spend manually as ₹500, corrects it to ₹550, then imports the statement carrying the real ₹550 line. With `NULL` + coalesce the key is the row's *current* `fp(₹550)`, the line matches, and nothing is staged. Had the row been stamped at creation, the key would be frozen at `fp(₹500)`, the ₹550 line would look new, and the user would get a duplicate. Imported rows want the exact opposite — freeze the source line — which is *why* the two cases key differently: a manual row has no external artifact, so its own current assertion is the only honest key. It is the same argument that keeps `source` server-managed under rule 1.
   - **The unique constraint stays on `fingerprint`.** `origin_fingerprint` is neither unique nor indexed: the prefetch is already bounded by `(user_id, account_id, date-range)` riding `ix_transactions_user_account_date`, and the `GROUP BY` runs over that bounded set. Do not add an index speculatively.
   - **The coalesced key stays correct for duplicate groups.** When one of N identical statement lines is edited, all N rows still coalesce to the same key, so `db_count` remains N and the file's N lines are all accounted for — nothing re-stages. The group's `MAX(occurrence)` may now span rows with differing `fingerprint` values, which can only *skip* an ordinal, never reuse an occupied one, and ADR-0006 rule 3 already permits gaps.
   - **Known residual gap**: a *manual* row (NULL origin) edited after a backup was exported will duplicate if that stale backup is re-imported. Out of scope — a backup file is a snapshot of our own data, not an immutable external artifact.

10. **Pending and confirmed rows are equally editable — no `confirmed_at` gate on PATCH.** This is inherited, not new: `update_transaction` already scopes on `id` + `user_id` alone, and the DELETE docstring (`:576-580`) names the symmetry as deliberate. The review queue is a confirmation gate, not a lock. Four interactions were checked and are clean:
    - Rule 9 covers pending rows, because `origin_fingerprint` is stamped at stage time rather than at commit — editing a pending row's amount and re-uploading the file before committing still matches on the coalesced key.
    - Cancel still wins: cancelling the batch hard-deletes the edited pending row, and a re-upload re-stages the original.
    - F4a auto-link (`reconciliation_service.auto_link_cc_bill`) gates on `transaction_type == "income"` at commit, so re-typing a pending row income → refund correctly makes it ineligible. That is the user overruling the parser, which is the point.
    - Commit-time learning (imports pass 3/4) teaches the *edited* `merchant_normalized`, which is the right key. Pending edits still never teach at PATCH time (ADR-0004, gate at `:528`).

**What not to do:**

- Do not change the ADR-0006 formula, `occurrence` semantics, the import multiset rule, or the re-upload contract. **None of ADR-0006's five rules is amended here** — `origin_fingerprint` stores an earlier *value* of the same formula and adds nothing to the *payload*.
- Do not make `origin_fingerprint` unique, do not backfill it for manual rows, and do not ever recompute it.
- Do not add a second write path (`PUT`). `exclude_unset` already expresses partial edits.
- Do not gate any of this on `source`. One schema governs manual and imported rows alike.

**Implementation approach:** the migration lands **first**, so the widening never ships without its safety net.

- Migration `0028_add_origin_fingerprint` — `op.add_column`, nullable `String(64)`. No batch rebuild: SQLite supports a plain `ADD COLUMN` for a nullable unconstrained column, so none of `0025`'s self-referential-composite-FK hazards apply. Backfill `origin_fingerprint = fingerprint WHERE source = 'import'`. Per ADR-0006 §Recompute rule e, the docstring must state that **the downgrade is one-way for already-edited rows** — dropping the column loses provenance that is no longer recomputable once `fingerprint != origin_fingerprint`.
- Backend — `import_service` stamps the column at stage time; both file-dedup prefetches switch to the coalesced key; schema widen; route recompute/validation branch; shared sign predicate.
- Frontend, board dialog — the four `ReadOnlyField`s become inputs, the 409 maps to a readable message, and the `transaction-dialog.tsx:6-9` docstring is rewritten **in the same commit**, since it currently states the opposite as settled policy.
- Frontend, review queue — deferred, deliberately. Rule 10 means the capability is already reachable over HTTP; only the surface waits, because the pending select-all / range-select work reworks `review-queue.tsx` anyway and four more editable fields in a dense table needs a row-expand. Until then the correction path is commit-then-edit on the board. This is sequencing, not a lifecycle rule.
- `PRD.md` §F4a-3's "the type is informational only" sentence must be corrected in the same arc — this ADR's justification depends on the type being load-bearing — and §F4 gains a sentence on `origin_fingerprint`.

**Verification** (`CLAUDE.md` §4 — `PRD.md` §Verification has no existing step for this):

1. Import a statement → commit some rows, cancel the rest → PATCH an identity field on a committed row → re-import the same file → **the edited row is not re-staged, and the cancelled rows are.** This is the one test that proves rule 9; without it the widening is unsafe.
2. PATCH a mis-typed `income` credit to `refund` with a spend category → the month's expense total drops by the refund magnitude.
3. PATCH an amount to collide with an existing row → 409. PATCH one leg of a live transfer pair → 422. Flip the kind without supplying a compatible `category_id` → 422.
4. `test_migration_parity` green; ruff / `ty` / `tsc` clean; coverage ≥ 75%.

**Implementation notes** (added while building the migration commit; both correct the
*mechanism* of rule 9, neither amends a decision):

- **Rule 9 bullet 5's "can only *skip* an ordinal, never reuse an occupied one" is false
  across groups.** It holds for the case the bullet describes — N identical lines, one
  edited, all still coalescing to one key. It fails when an edit moves a row's `fingerprint`
  *into* a group whose coalesced key sits elsewhere: import `L→(fp X, occ 0)` and
  `M→(fp Y, occ 0)`, delete `L`, PATCH `M` until its fingerprint is `X` (legal — nothing
  holds `(X, 0)` any more, so rule 3 does not 409), then re-upload. Group `X` is absent from
  a coalesced `MAX`, the allocator returns `0`, and the insert collides. There is no per-row
  SAVEPOINT to recover (`app/services/occurrence.py`), so it is a 500 that fails the whole
  batch. **The two aggregates therefore key differently**: `COUNT` over
  `COALESCE(origin_fingerprint, fingerprint)` — multiplicity follows the source line — and
  `MAX(occurrence)` over the current `fingerprint`, which is what the unique constraint
  actually holds. `OccurrenceAllocator` is unchanged.
  Pinned by `test_reupload_after_an_edit_into_a_deleted_rows_fingerprint_does_not_collide`.
- **Rule 9 bullet 4's scope is wrong.** "The prefetch is already bounded by
  `(user_id, account_id, date-range)` riding `ix_transactions_user_account_date`" describes
  the pre-ADR code, whose date window was *exact* only because nothing could edit a date:
  the hash contained `date_iso` and the row stored that same date. Editable identity columns
  break that premise in both directions — `origin_fingerprint` freezes the original date and
  account while the row stores the corrected ones — so a date fixed outside the statement's
  period, or a row moved to another account, falls outside the scope, its provenance goes
  unread, and the file line re-stages as exactly the duplicate this rule exists to prevent.
  **The prefetch is scoped to the parsed file's fingerprint set instead**
  (`origin_fingerprint IN fps OR fingerprint IN fps`, user-scoped), which is exact rather
  than a proxy: the hash already encodes `account_id`, so a file fingerprint can only ever
  belong to that account and both old predicates were redundant given this one. It costs the
  index range-scan — no index is added, per this rule's own instruction — and bounds load by
  the file (2 binds per parsed row) rather than by account history. `backup_import_service`
  needed the sibling change: its key drops from `(account_id, fingerprint)` to the bare
  coalesced fingerprint, for the same reason.
  Pinned by `test_date_edit_outside_the_statement_window_does_not_restage` and
  `test_row_moved_to_another_account_is_not_restaged`.

---

## Trade-offs

**Benefits:**

- The record behaves the way the user already assumes it does, so the question stops recurring — and the three findings that produced this ADR are closed by one rule set rather than three.
- The parser's unavoidable refund-vs-cashback ambiguity becomes user-correctable, which no amount of regex widening can achieve.
- `PRD.md` §F4a-4's named v1 resolution — "user **edits** the wrong row via the transaction UI, or deletes it" — becomes real; only the delete half was shipped.
- Delete-and-re-create stops being the only recovery. That path is lossy (it drops `id`, labels, `confirmed_at`, `auto_category_id` and any transfer link) and undiscoverable.
- The re-import duplicate is **prevented**, not merely documented.

**Drawbacks:** PATCH gains two cost classes; sign validation leaves the schema for the route; one migration and one extra `String(64)` column on the busiest table; and two hashes on a row is a thing every future reader must understand.

**Mitigation:** the two columns answer visibly different questions, and the docstrings on the model column and on the importer prefetch must both say which is which — `fingerprint` = what this row says, `origin_fingerprint` = which statement line produced it. ADR-0006's identity-vs-multiplicity split is the precedent to point at; this is the same move applied to provenance.

**What is deliberately NOT solved:** rows the user **cancelled** in the review queue still re-surface on re-upload, exactly as before. Cancel is not a tombstone (`import_service` `:33-34`) and ADR-0006 refuses to change that — suppressing it needs soft-delete tombstones. Step 2 of the trace above relies on that behaviour. What rule 9 removes is narrower and different: an **edit** no longer masquerades as a deletion.

With the duplicate prevented there is nothing left to warn about, so `source` stays off the wire — `TransactionRead` keeps its deliberately tight field set and the dialog needs no provenance-conditional copy.

## Alternatives Considered

**Alternative 1 — `transaction_type` only, leaving the identity tuple frozen**: rejected. It is ~20 lines with no recompute and it does fix the one provable correctness bug, but a record where the *type* is editable and the *amount* beside it is not is more confusing than either extreme — it makes the hidden constraint visible without making it explicable.

**Alternative 2 — identity edits restricted to `source == "manual"` rows**: rejected. It adds a third behaviour to a path that never reads `source`, and it denies the correction to exactly the imported rows most likely to need one.

**Alternative 3 — delete + re-create as the sanctioned correction path**: rejected. Lossy and undiscoverable; see Benefits.

**Alternative 4 — carry `occurrence` across a fingerprint change**: rejected. It silently lands the row in a gap of the target group and defeats the double-submit guard that ADR-0006 rule 4 exists to preserve.

**Alternative 5 — auto-clear `category_id` on a kind flip**: rejected. Silently destroys a choice the user made, when the picker already knows the required kind and can send both fields in one request.

**Alternative 6 — allow editing one leg of a live transfer pair**: rejected. The two legs are one money movement with server-derived signs; editing one alone breaks ADR-0002's symmetry invariant, and `unlink` already exists as the explicit way to dissolve the relationship first.

**Alternative 7 — a separate `PUT` replace endpoint**: rejected. Two write paths for one resource, when `exclude_unset` already distinguishes "omitted" from "explicit null".

**Alternative 8 — expose `source` read-only on `TransactionRead` so the dialog can warn only on imported rows**: rejected. A record is a record; a provenance-conditional warning rebuilds the two-class model this ADR abolishes, DELETE ships without the equivalent warning, and a wire field with no renderer is exactly the drift `frontend/CLAUDE.md` documents (a backend field the TS type never declares is invisible to `tsc`, which is how `fx_unavailable_count` was computed and silently discarded).

**Alternative 9 — freeze `fingerprint` on edit instead of adding a column**: rejected, and it was the closest call. It removes the re-staging problem for free, with no migration, by letting the hash mean "the statement line this row came from". But then the hash stops describing the row: two rows edited to identical visible values keep different fingerprints, so the duplicate becomes invisible and no 409 fires, and the manual double-submit guard silently weakens after any edit. It trades a *detectable* duplicate for an *undetectable* one, and contradicts ADR-0006 rule 2 — "a fingerprint answers *what is this transaction?*".

**Alternative 10 — accept the re-import duplicate and document it**: rejected. The failure is silent, it double-counts signed sums, and it lands on precisely the row the user cared enough to correct. The re-staged candidate is indistinguishable in the queue from a deliberately-cancelled one, so there is no point at which the product tells the truth about it.

## Consequences

- **Not decided here:** the same principle points straight at `investment_transactions`, which carries its own fingerprint and `occurrence` (ADR-0006 §Consequences, migration `0027`) and whose manual rows write `fingerprint = NULL`. Whether F7 rows get the same treatment is a separate decision — naming it here stops it being assumed either way.
- The review-queue editing surface is sequencing debt, tracked with the select-all / range-select work, not a permanent asymmetry. Rule 10 is the contract; the UI catches up.
- `PRD.md` §F4a-3 and §F4 both need amending (see Implementation approach). Until §F4a-3's "informational only" sentence is corrected, the PRD contradicts both this ADR and the dashboards code.
