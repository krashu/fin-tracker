# ADR-0009: A refund is a signed spend, not a `transaction_type`

**Status**: Accepted
**Date**: 2026-08-12

## Problem

`transaction_type` carried four values — `spend | income | transfer | refund` — and an
uncommitted, in-progress pass on this branch had gone further still, promoting refund a
*second* time onto the category axis: a seeded spend-kind "Refund" category, with
`imports.py` defaulting every untagged refund into it. That duplicated the concept on two
axes at once and actively worked against the netting `PRD.md` §F4a already asks for — a
Groceries refund parked in a generic "Refund" bucket leaves Groceries permanently inflated
by the refunded amount, which is the opposite of what a refund is supposed to do to a
category total.

**The type was never earning its keep.** `sign_error()` (`app/schemas/transactions.py`)
already required `refund > 0` and `spend < 0` — disjoint signs, same shape as `spend` and
`income`. Nothing downstream used the *type* `refund` for anything a *sign check on
`spend`* couldn't do instead: every F8 aggregate filtered `transaction_type IN
('spend', 'refund')` as a single bucket (`app/api/v1/dashboards.py`, pre-ADR — 11 sites),
`AUTO_TAGGABLE_TYPES` included both, and `_map_type` (`import_service.py`) only ever
produced `refund` as a *fallback* when a credit couldn't be classified as cashback income.
The type was a label for "spend row, positive sign" wearing a different name.

**Five competitor apps agree, at schema level, that this is the wrong axis:**

- **Firefly III** — types are structural (`Withdrawal` / `Deposit` / `Transfer`); "Refund" is
  a seeded **`LinkType`** *between* journals (`database/seeders/LinkTypeSeeder.php`), not a
  transaction type. Issue **#769** asked the maintainer for a `refund` type outright; it was
  rejected, citing GnuCash's "negative expense" convention and naming the exact failure mode
  this ADR is written to avoid — *"a huge expense and a huge income, skewing all charts."*
- **Actual Budget** — a refund is a positive transaction in the **same category** as the
  spend it reverses, documented explicitly as "isn't income."
- **YNAB** — a refund is an inflow to the original spend category, same mechanism.
- **Lunch Money** — has no `type` column on transactions at all; `is_income` lives on the
  *category*, not the row.
- **GnuCash** — books a refund as a negative expense (the double-entry equivalent of a
  positive spend row here).

None of the five gives refund a type slot of its own. `PRD.md` §F4a already documents the
sign convention as policy ("refunds carry the opposite sign in the same category so signed
sums net naturally") — this ADR makes that convention structural instead of advisory, and
undoes the category-axis duplication in the same pass (see Alternatives, "the path not
taken twice").

## Decision

**`transaction_type` narrows to `spend | income | transfer`. A refund is derived, never
stored: `transaction_type == "spend" AND amount_paise > 0`.**

- **Identity is untouched.** [ADR-0006](0006-f4-dedup-key.md)'s fingerprint is
  `sha256("\x1f".join(date_iso, amount_paise, normalized_merchant, account_id))` — it never
  hashed `transaction_type`. No fingerprint recompute, no `origin_fingerprint` change, no
  `occurrence` change. Migration `0029_refund_as_signed_spend` is a pure `UPDATE …
  SET transaction_type = 'spend' WHERE transaction_type = 'refund'` plus a narrowed CHECK
  constraint — no identity column moves.
- **`sign_error()` becomes asymmetric, deliberately** ([ADR-0007](0007-transaction-field-editability.md)
  rule 4, amended): `spend` accepts any non-zero sign (negative = outflow, positive =
  refund); `income` still requires `> 0`; `transfer` still accepts any non-zero sign; zero is
  rejected for all three. Loosening `spend` is the point. `income` keeps `> 0` because an
  income *reversal* (a salary clawback) is out of scope for v1 — there is no symmetric
  "negative income" concept this ADR is trying to create. The accepted cost: a fat-fingered
  positive spend now reads as a refund instead of 422ing. There is no server-side way to
  distinguish "user meant to log an outflow and typed the wrong sign" from "user meant to
  log a refund" — both are a positive `spend` row, and only the user knows which.
- **Every F8 aggregate discriminates on sign, not on type.** The dashboards YTD block is the
  one non-mechanical case: `gross_spend` becomes `spend AND amount_paise < 0`, `refund`
  becomes `spend AND amount_paise > 0`, `cashback` (already `income`) is unchanged. The wire
  fields `gross_spend_ytd_paise` / `refund_ytd_paise` / `spend_ytd_paise` keep their names
  and meanings — `spend_ytd = gross + refund` still holds — only the discriminator moved from
  type to sign.
- **F4a auto-reconciliation needed an explicit sign guard, not just a comment rewrite.**
  `reconciliation_service.py`'s CC-bill candidate query filtered `transaction_type IN
  ('spend', 'transfer')` and derived "the target is negative" from the fact that the CC-side
  row was `income` (`> 0` per `sign_error`) and equality pins the sign. That derivation holds
  **only while `sign_error` holds** — but `POST /backup/import` persists `transaction_type`
  and `amount_paise` verbatim, with no `sign_error` call (a **file-upload boundary**, which
  `AGENTS.md` §Simplicity first explicitly scopes validation to). A hand-edited backup file
  can therefore store a negative `income` row; before this ADR the type filter still excluded
  a positive `refund` from matching it, but after the collapse a positive `spend` (a refund)
  would satisfy `amount_paise == -txn.amount_paise` and could be mis-paired as a bill payment.
  The fix is an explicit `Transaction.amount_paise < 0` clause in the candidate query — stated
  rather than derived, because the invariant it depends on is no longer guaranteed at every
  call site.
- **Every other consumer collapses mechanically once the type does:**
  - `_map_type` (`import_service.py`): the parser's own sign already carries refund-ness, so
    the fallback for an unmatched credit is `income` for a cashback-named merchant, `spend`
    otherwise — never a manufactured `refund`.
  - `AUTO_TAGGABLE_TYPES` (`tag_service.py`): `frozenset({"spend"})`. Refunds still
    auto-tag — they are spend rows now, by construction.
  - `backup_csv.py`: `_TXN_TYPES` is `frozenset(get_args(TransactionTypeStr))` and narrows for
    free. A **read-only legacy alias** — `refund` → `spend` on read — lets an already-exported
    backup zip predating this ADR still import cleanly; nothing writes the value `refund`
    again, so the alias is a one-way ramp, not a live vocabulary.
  - Frontend: `EntryDirection` (`lib/transaction-types.ts`) is the UI-level three-way
    Spend/Refund/Income choice PRD §F2's manual-entry form still needs, mapped to
    `{type, sign}` — `spend → (spend, −)`, `refund → (spend, +)`, `income → (income, +)`. The
    stored vocabulary narrows; the *form* does not, because the ambiguity `EntryDirection`
    resolves (is this outflow or reversal?) is real and user-supplied, not derivable from the
    amount alone until after the choice is made.
  - Review queue: the flat "Refund" shortcut (PATCH `category_id: null`) is replaced by a
    merged spend + income category dropdown on a credit row — picking a spend category makes
    the row a positive `spend` (a refund, netting against that category); picking an income
    category makes it `income`. This restores the "which spend category did this refund
    reduce" precision an earlier pass had traded away for a flat default, and costs nothing
    extra now that there is no separate type to toggle.

**What not to do:**

- Do not resurrect a seeded "Refund" category, on this axis or any other — that is the exact
  duplication this ADR undoes (see Problem).
- Do not touch `investment_transactions.transaction_type` (`buy | sell | dividend | ...`) —
  disjoint vocabulary, a different table, no refund concept.
- Do not touch `parsers/base.py`'s `TxnType` (`purchase | payment | refund | other`) — this is
  statement-side classification vocabulary, upstream of `_map_type`, and `refund` stays a
  valid `TxnType` value forever: `_classify` still needs to name "this credit's merchant text
  matched a refund/reversal/chargeback keyword" before `_map_type` decides what stored type
  and sign that becomes. T3, in the working notes for this change.
- Do not widen `income`'s sign rule to match `spend`'s. Income reversal is a different,
  unscoped problem; conflating the two erases the one piece of information `sign_error` still
  enforces.

**Implementation approach:** narrow the model Literal
(`app/models/transaction.py:TransactionTypeStr`) first — the column splats
`*get_args(TransactionTypeStr)`, so this one edit drives the CHECK constraint DDL. Migration
`0029_refund_as_signed_spend` updates data before narrowing the CHECK (so no row violates it
mid-migration), and rebuilds `ix_transactions_user_confirmed_date` (`WHERE confirmed_at IS
NOT NULL`) explicitly in both `upgrade()` and `downgrade()` — SQLite's `batch_alter_table`
drops a partial index's predicate on reflection ([0008](0008-f3-upi-merchant-normalisation-deferral.md)
hit the same hazard), and `test_migration_parity.py` does not check WHERE clauses, so nothing
else would catch a silent loss. The downgrade is reconstructive, not byte-exact: it widens the
CHECK back and flips every positive `spend` row to `refund`, which would also relabel a
legitimately positive non-refund spend, had one existed — none do at the time of this
migration, but a future downgrade against a database that has since gained one loses that
distinction. Then the wide backend sweep (services, API, parsers, demo data), then the
frontend (`EntryDirection`, the review-queue dropdown, comment sweep), then docs.

**Verification** (`PRD.md` §Verification step 1, rewritten by this ADR):

1. A `CHARGEBACK`-classified credit stores `transaction_type = "spend"` with a **positive**
   `amount_paise`, for both the Axis and ICICI parsers (their refund keyword vocabularies
   diverged historically) — composing `_classify` (still yields `TxnType == "refund"`, T3)
   with `_map_type` (now folds that into `spend`), since the stored type only appears after
   the second step.
2. A positive `spend` nets a category to **exactly zero** against a matching negative `spend`
   in the same category — the structural form of the §F4a netting convention.
3. The F4a mis-pair guard: a negative `income` CC row (the state `POST /backup/import` can
   persist without `sign_error`) plus a positive same-magnitude `spend` in the parent bank
   account does **not** auto-link.
4. Migration `0029` up and down against the test SQLite DB, with an explicit assertion that
   `ix_transactions_user_confirmed_date` keeps its `WHERE confirmed_at IS NOT NULL` predicate
   after each direction.
5. A legacy backup CSV containing `transaction_type=refund` imports as `spend` (read-only
   alias, T4).
6. `GET /transactions?transaction_type=spend&amount_sign=positive` returns refunds only, and
   stays tenant-isolated (ADR-0003).

---

## Trade-offs

**Benefits:**

- Removes a distinction without a difference: nothing downstream ever needed the type
  `refund` for anything a sign check on `spend` didn't already do, and five competitor apps
  independently reach the same schema-level answer.
- `PRD.md` §F4a's netting convention stops being advisory prose and becomes the only
  representation there is — a refund literally cannot drift into its own bucket, because
  there is no bucket left to drift into.
- One fewer type to keep in lockstep across the model Literal, the schema sign rule, every
  dashboard filter, the parser mapping, and the frontend picker — each of which was a second
  place the same "is this a refund" decision had to agree with the first.
- Restores review-queue precision (which spend category a refund nets against) that an
  earlier pass had traded away specifically because the flat type made a precise choice
  clunky to offer.

**Drawbacks:** `sign_error` is now asymmetric between `spend` and `income`, which is one more
rule to remember; the F4a reconciliation guard depends on an invariant (`sign_error` runs) that
one code path (`POST /backup/import`) doesn't actually provide, so the guard has to be stated
rather than derived; and a fat-fingered positive spend is indistinguishable from a genuine
refund at the API level — there is no server-side way to catch the former.

**Mitigation:** the sign asymmetry is documented at the single place it's enforced
(`sign_error()`'s docstring) rather than left implicit; the reconciliation guard's comment
names the exact backup-import path that makes it necessary, so a future reader doesn't "clean
up" the supposedly-redundant clause; and the fat-finger ambiguity is inherent to a two-value
sign, not something a third type could actually resolve either — Firefly III's issue #769
rejection makes the same point about a `refund` type not preventing the analogous mistake in
the other direction.

## Alternatives Considered

**Alternative 1 — keep `refund` as a fourth `transaction_type`** (the status quo before this
ADR): rejected. Every downstream consumer already treated `spend` and `refund` as one bucket
(11 dashboard filter sites alone), so the type carried no information a sign check didn't
already have, at the cost of keeping four vocabularies in sync instead of three.

**Alternative 2 — the path not taken twice: promote refund onto the category axis instead**
(a seeded "Refund" category, tried and reverted in this same working-tree pass): rejected,
and rejected specifically because it makes the netting problem *worse*, not neutral. A
refund's whole job is to reduce the total in the category it reverses; parking it in a
category of its own guarantees the original category stays inflated by exactly the refunded
amount, forever.

**Alternative 3 — link a refund to the spend it reverses**, the way Firefly III actually
models it (a `LinkType` between journals): rejected for v1. `transfer_pair_id` exists and has
the right shape (two rows, one relationship), but [ADR-0002](0002-transfer-pair-id-semantics.md)
pins it specifically to F4a auto-reconciliation and F2 user transfers — repurposing it for
refund-linking would be re-litigating a settled decision, not a freebie, and the sign
convention already gets the netting math right without needing an explicit link. Worth
revisiting only if a future feature needs to answer "which specific spend did this refund
reverse" rather than "how much did this category net to."

**Alternative 4 — widen `income`'s sign rule to match `spend`'s (accept negative income)**:
rejected. It would make an income reversal representable, but that is a different, unscoped
problem (salary clawback, not merchant refund), and accepting it here would erase the one
piece of information `sign_error` still enforces for income rows.

**Alternative 5 — derive the reconciliation guard's sign invariant instead of stating it**:
rejected once `POST /backup/import`'s bypass of `sign_error` was traced through. Deriving
"the target must be negative" from "the CC-side row is `income`" is correct only where
`sign_error` ran, and there is at least one write path where it didn't — so the guard has to
name the sign explicitly rather than assume it follows from the type.
