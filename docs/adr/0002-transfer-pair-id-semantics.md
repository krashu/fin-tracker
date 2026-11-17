# ADR-0002: Paired rows are exactly two same-user legs, linked both ways and written together

**Status**: Accepted
**Date**: 2026-05-21 (decided) · **Revised**: 2026-11-17
**Scope**: PRD §F2, §F4a rule 1, §F7 (IDCW reinvestment, switch), §Data model · `app/models/transaction.py`
(`transfer_pair_id`), `app/models/investment_transaction.py` (`pair_id`),
`app/services/reconciliation_service.py` (`auto_link_cc_bill`, `card_bill_transfer_gate`,
`statement_window`), `app/api/v1/imports.py` (`commit_import_batch`, `list_candidates`),
`app/api/v1/transactions.py`
(`create_transaction`, `update_transaction`, `create_transfer`, `unlink_transaction`, `delete_transaction`, `bulk_delete_transactions`),
`app/services/transaction_delete.py` (`delete_owned_transactions`), `app/services/demo_seed.py`,
`app/api/v1/investment_transactions.py`
(`create_reinvestment`, `delete_investment_transaction`), `app/services/backup_import_service.py`
**Supersedes / Related**: [ADR-0001](0001-sqlite-postgres-portability.md) (constraint names) ·
[ADR-0003](0003-multi-user-auth.md) (composite same-user FK) · [ADR-0009](0009-refund-as-signed-spend.md)

## Decision

A pair is two rows of one user that record a single event, pointing at each other. On `transactions`,
`transfer_pair_id` links a [transfer pair](../../GLOSSARY.md#paired-rows): the F4a CC-bill auto-link,
an F2 manual transfer, and backup restore all use it. On `investment_transactions`, `pair_id` links an
[investment pair](../../GLOSSARY.md#paired-rows): an IDCW payout plus its reinvestment, or the two legs
of a switch. Both columns follow one invariant set.

## Rules

**Every pair column** (`transactions.transfer_pair_id`, `investment_transactions.pair_id`):

1. **Same user**: a composite FK `(pair_col, user_id) → (id, user_id)` over a unique `(id, user_id)`
   index. — *Enforced by*: DB FK · *Locked by*: `tests/models/test_models.py::test_transfer_pair_id_cross_user_rejected`
2. **No self-pair**: CHECK `no_self_pair` (`pair_col IS NULL OR pair_col != id`). — *Enforced by*:
   DB CHECK · *Locked by*: `tests/models/test_models.py::test_transaction_transfer_pair_self_reference_rejected`
3. **Symmetric, exactly two**: `A → B ⇔ B → A`; no chains; a candidate search ignores rows already
   paired. — *Enforced by*: service ·
   *Locked by*: `tests/services/test_reconciliation_service.py::test_links_single_matching_bank_row`,
   `tests/api/test_investment_transactions.py::test_reinvestment_writes_both_pair_directions`
4. **Atomic population**: both legs' links land in one DB transaction; a failure leaves neither. —
   *Enforced by*: service ·
   *Locked by*: `tests/services/test_reconciliation_service.py::test_a_constraint_failure_on_the_pair_write_is_not_swallowed`,
   `tests/api/test_transactions.py::test_transfer_dup_dest_leg_409_and_source_rolled_back`
5. **Deleting a leg nulls its partner's link** in the same transaction. On `transactions.transfer_pair_id`,
   every delete that can leave a partner behind goes through one shared delete,
   `services/transaction_delete.py::delete_owned_transactions`: the single delete (`DELETE /transactions/{id}`),
   the bulk delete (`POST /transactions/bulk-delete`), the review-queue discard (`discard_ids` on
   `POST /imports/{batch_id}/commit`) and the demo reseed. It releases each partner outside
   the deleted set (rule 9); when both legs are deleted together, neither is released and both go in one
   statement. Two paths delete `transactions` without it, and both are safe: import cancel deletes only
   pending rows, and nothing can point at a pending row (only confirmed rows are paired or named as a
   refund's original); user teardown deletes all of a user's rows in one statement. — *Enforced by*: service ·
   *Locked by*: `tests/api/test_transactions.py::test_delete_transaction_paired_unlinks_partner`,
   `::test_bulk_delete_releases_partners_outside_the_request`, `::test_bulk_delete_takes_both_legs_of_a_pair`,
   `tests/api/test_imports_review.py::test_commit_discards_listed_pending_rows_in_the_same_request`,
   `tests/services/test_demo_seed.py::test_reseed_releases_a_partner_outside_the_wiped_rows`;
   `tests/api/test_investment_transactions.py::test_delete_one_leg_unlinks_partner`
6. **Only named writers mint a pair**, and no request body can set a pair column. — *Enforced by*:
   schema validation (`extra="forbid"`) + service ·
   *Locked by*: `tests/api/test_transactions.py::test_transfer_extra_field_422`,
   `::test_patch_server_managed_key_422`

**`transactions.transfer_pair_id`:**

7. **Writers**: F4a `auto_link_cc_bill` (run at the commit of either the card or the bank batch), the
   F4a manual link (rule 11b), F2 `POST /transactions/transfer`, and backup restore. Restore pairs two
   legs only when exactly two rows sharing a `transfer_group` were inserted by that restore, both are
   confirmed, and they satisfy rule 8; both links land in the restore's single transaction. A row
   restored with a blank `confirmed_at` is pending, and import cancel deletes pending rows (rule 5),
   so it is never paired. Restore never mutates a pre-existing row, so a leg whose partner deduped
   against an existing row, or whose group has a pending leg or fails rule 8, is kept unpaired, given
   back its pre-link type (rule 9), and reported as a restore warning
   counted in `links_not_restored`. The auto-link candidate search skips a row in a refund link: a
   refund's original or a linked refund (ADR-0009 rule 13). — *Enforced by*: service ·
   *Locked by*: `tests/services/test_backup.py::test_round_trip_rebuilds_on_a_fresh_db`,
   `::test_restore_lone_leg_warns`, `::test_restore_never_pairs_a_non_transfer_leg`,
   `::test_restore_never_pairs_a_pending_leg`,
   `tests/services/test_reconciliation_service.py::test_auto_link_skips_a_refund_original`
7b. **An unlinked card-bill debit is a transfer when its card's statement for that date is in.** At a
    bank batch's commit, a debit flagged as a card-bill payment (`cc_payment_candidate`, ADR-0010 rule
    12) and committed with no category becomes an unpaired `transfer` when a live card of the user names
    that bank as "Paid from" (`parent_account_id`) and a committed statement of that card covers the
    debit's date. The card's purchases for that period are then on the books, and counting the bill as
    spend would count them twice. It stays out of spend and income, in no category, until a link pairs
    it: the auto-link at a later card commit (rule 7) or the manual link (rule 11b). It keeps `transfer`
    as its pre-link type (rule 9), so an unlink leaves it an unpaired transfer. Otherwise (no such card,
    or no committed statement of it covering that date: history from before the card was tracked, a
    month not yet imported) it commits as spend in Other (ADR-0012 rule 8). It is then the only record
    of that money, or it links when the card's statement arrives. A flagged debit given a category in
    review commits as spend in that category either way. The default taxonomy has no card-bill spend
    category, and import never prefills a category onto a flagged debit (ADR-0004 rule 8), so a rule
    learned from one bill never decides the next. The commit reads "Paid from" and the card's
    statements as they stand at that commit; later changes never retype a committed row.
    `GET /imports/{batch_id}/candidates` sends `uncategorised_commits_as_transfer` on each row, so the
    review queue never derives it. The retype mints no pair (rule 6 is unchanged). — *Enforced by*:
    service · *Locked by*:
    `tests/api/test_imports_review.py::test_commit_keeps_a_covered_card_bill_debit_out_of_spend`,
    `::test_commit_card_bill_debit_counts_as_spend_when_no_committed_card_statement_covers_it`,
    `::test_commit_card_bill_debit_covered_by_a_statement_with_no_printed_period`,
    `::test_commit_card_bill_debit_given_a_category_stays_spend`,
    `::test_commit_links_a_covered_card_bill_debit_to_a_card_payment_on_the_board`,
    `::test_commit_links_a_later_card_payment_to_a_covered_card_bill_debit`,
    `tests/services/test_reconciliation_service.py::test_card_bill_gate_reads_the_card_statements_in_one_query`,
    `tests/test_migration_parity.py::test_card_bill_category_migration_archives_the_seed_and_keeps_rows`,
    `frontend/app/imports/review/[batchId]/review-queue.test.tsx` ("offers 'Card bill payment' on a
    flagged bank debit, with the card-side copy")
7c. **An unmatched card payment is a transfer.** At a card batch's commit, a credit flagged as a bill
    payment (`cc_payment_candidate`: a positive `income` row naming one, PRD §F4a-1) and committed
    with no category becomes an unpaired `transfer`, whatever "Paid from" says and whether or not the
    paying bank is tracked. Paying a card moves the user's own money onto it: it is never income, and
    the purchases it settles are spend on the card's own statements, so unlike rule 7b nothing is
    left uncounted. It stays out of spend and income, in no category, until a link pairs it: the
    auto-link at this commit or a later bank commit (rule 7), or the manual link (rule 11b). It keeps
    `transfer` as its pre-link type (rule 9), so an unlink leaves it an unpaired transfer. A flagged
    credit given a category in review commits in that category's kind. `card_bill_transfer_gate`
    decides this and rule 7b for both sides, and `uncategorised_commits_as_transfer` carries it to the
    review queue on both. The auto-link pairs a positive card leg that is `income` or `transfer` with
    a negative bank leg that is `spend` or `transfer`. The retype mints no pair (rule 6 is
    unchanged). — *Enforced by*: service · *Locked by*:
    `tests/api/test_imports_review.py::test_commit_keeps_an_unmatched_card_payment_out_of_income`,
    `::test_commit_card_payment_given_a_category_keeps_its_kind`,
    `::test_commit_links_a_covered_card_bill_debit_to_a_card_payment_on_the_board`,
    `::test_commit_links_a_later_card_payment_to_a_covered_card_bill_debit`,
    `tests/services/test_reconciliation_service.py::test_auto_link_pairs_a_card_payment_committed_as_a_transfer`,
    `frontend/app/imports/review/[batchId]/review-queue.test.tsx` ("says a flagged card credit stays
    out of income when no bank debit matches")
7d. **A user may write an unpaired `transfer`.** `POST /transactions` and `PATCH /transactions/{id}`
    accept `transfer` like `spend` and `income`, on an unpaired row, so a row the commit retyped
    (rules 7b, 7c) and the user moved to spend or income can be put back, and money moved to an
    untracked account can be kept out of spend and income. Neither writes a pair column (rule 6), so
    the row is an unpaired transfer until a link pairs it. A paired row's type stays locked (rule 10).
    The category kind still follows the type (ADR-0006 rule 13), a row in a refund link keeps the
    type its link needs (ADR-0009 rule 13), and the row never teaches (ADR-0004 rule 6). —
    *Enforced by*: route · *Locked by*: `tests/api/test_transactions.py::test_post_transfer_either_sign`,
    `::test_patch_an_unpaired_row_to_transfer`,
    `frontend/app/expenses/transaction-dialog.test.tsx` ("retypes an unpaired row to Transfer, keeping its sign")
8. **A paired row is a `transfer`, and a pair nets to zero across two accounts**: CHECK
   `transfer_pair_id IS NULL OR transaction_type = 'transfer'`, and the two legs have exactly equal and
   opposite `amount_paise` on different `account_id`s. Every writer guarantees the second half: F4a's
   zero-tolerance candidate query, the manual link (rule 11b), F2 (server-derived signs; source and
   destination must differ), and restore (rule 7). The type, not the link, is what excludes a row from
   spend and income. — *Enforced by*: DB CHECK (type) + service (amounts, accounts) ·
   *Locked by*: `tests/models/test_models.py::test_paired_non_transfer_rejected`,
   `tests/services/test_backup.py::test_restore_never_pairs_a_non_transfer_leg`
9. **Linking an existing row stores its pre-link type and category** in two nullable columns,
   `pre_link_type` and `pre_link_category_id` (a composite same-user FK), and clears the row's
   category: a pair belongs to no category. F4a and the manual link do this. Unlink and
   partner-delete restore both and clear the columns, except that a category set on a linked leg
   wins: every category write on a linked leg clears `pre_link_category_id` (CHECK
   `pre_link_category_while_linked`). Archive-with-reassign rewrites a stored pre-link category like
   a live one (ADR-0012 rule 10); a plain archive leaves it. A leg born as a transfer (F2) has
   neither and stays `transfer`. Both columns double as provenance; backup export and restore carry
   them. — *Enforced by*: DB CHECK + FK + service · *Locked by*:
   `tests/api/test_transactions.py::test_unlink_restores_pre_link_type`, `::test_unlink_restores_pre_link_category`,
   `::test_a_category_set_after_linking_survives_unlink`, `tests/models/test_models.py::test_pre_link_category_needs_a_linked_uncategorised_row`
10. **Unlink** (`POST /transactions/{id}/unlink`) clears both legs in one transaction; it is
    idempotent and returns 404 cross-user. A paired row refuses type and identity edits. — *Enforced
    by*: service · *Locked by*: `tests/api/test_transactions.py::test_unlink_clears_both_legs`,
    `::test_patch_a_live_transfer_leg_422s`
11. **An F2 transfer's legs** are both INR, non-investment, owned and live. — *Enforced by*: service ·
    *Locked by*: `tests/api/test_transactions.py::test_transfer_cross_currency_422`
11b. **A manual link** (`POST /transactions/{id}/link` with `partner_id`) pairs two existing confirmed
    rows when auto-link missed ("Paid your card bill elsewhere?", offered from an unpaired `income` or
    `transfer` credit on a card, which `TransactionRead.card_bill_link_offered` marks). Both rows
    are owned, live, INR and non-investment, on different accounts, unpaired, and **exactly equal and
    opposite** in amount; dates may differ; and neither is in a refund link (a refund's original or a
    linked refund, ADR-0009 rule 13). Each stores its pre-link type and category (rule 9) and becomes
    a `transfer`. Anything else is 422; a foreign `partner_id` is 422 (ADR-0003 rule 9). A link is not
    a categorisation and never teaches (ADR-0004). `GET /transactions/{id}/link-candidates` lists the
    closest 20 rows the same rule accepts, nearest date first. — *Enforced by*: service ·
    *Locked by*: `tests/api/test_transactions.py::test_manual_link_pairs_equal_opposite_rows`,
    `::test_manual_link_amount_mismatch_422`, `::test_manual_link_refused_422`,
    `::test_link_candidates_lists_only_linkable_rows`,
    `frontend/app/expenses/card-bill-link-field.test.tsx` ("lists the server's link candidates in the order given"),
    `tests/api/test_transactions.py::test_list_marks_only_unpaired_card_credits_card_bill_link_offered`,
    `frontend/app/expenses/transaction-dialog.test.tsx` ("renders the card-bill link only when the server offers it")

**`investment_transactions.pair_id`:**

12. **Writers**: `POST /investment-transactions/reinvestment` (an IDCW `dividend` leg plus a `buy` leg).
    A future switch path pairs `switch_out` with `switch_in`. The pair's kind is read from its member
    types; there is no discriminator column. — *Enforced by*: service ·
    *Locked by*: `tests/api/test_investment_transactions.py::test_reinvestment_records_both_legs`

## Out of bounds

- CSV investment import never writes a pair: the format cannot express the link.
- A refund's optional link to its original spend is a separate column (ADR-0009), not a pair.
- Provenance beyond rule 9 (which writer created a pair) is not stored.
- Linking rows of unequal amount (a partial bill payment, a payment plus a bank fee): each pair nets to
  zero, so the remainder stays an ordinary row.

## Why

Two rows can record one movement of money: a card bill paid from a bank account, or a bank-to-bank
move. Counting both double-counts. One column with one meaning gives one exclusion rule for dashboards
and one set of tests. Investment pairs have the same shape: an IDCW reinvestment is income and an
acquisition on one date, and conflating them breaks FIFO holding periods. A CHECK cannot reference
another row on either engine, so same-user ownership is a composite FK. Symmetry needs triggers, which
are not portable, so the service enforces it. Without the type CHECK and the stored pre-link type,
a hand-edited backup could pair two spends, and unlinking a wrong F4a match would silently drop rent
out of spend. Fold, an Indian spending app, works the same way: auto-link plus a manual "paid
elsewhere" link, and it admits auto-link misses.

## Alternatives rejected

- **F4a-only column** — F2 transfers would need a second mechanism for the same concept.
- **One column per use** (`f4a_pair_id`, `f2_pair_id`) — two indexes; provenance is metadata.
- **CHECK joining on `user_id`** — impossible on both engines.
- **Triggers for symmetry** — not portable across the cutover.
- **Legs stay `transfer` after unlink** — a mis-linked spend silently leaves spend totals.
- **Manual link with an amount tolerance** — breaks "a pair nets to zero", the rule every aggregate
  relies on.
- **An unlinked card-bill debit stays spend until linked** — a tracked card's bill counts twice
  until its card statement arrives, and for good when the link misses.
- **A needs-link type or column** — a fourth type breaks ADR-0009 rule 1 and every aggregate; a
  flag duplicates what an unpaired `transfer` already says.
- **Gate on "Paid from" alone** — bank history imported further back than the card's would drop
  every earlier bill out of spend with no card purchases to replace it, so those months
  under-report.
- **An unmatched card payment stays income until linked** — a bill paid from an untracked bank, or
  one whose bank statement is not in yet, counts as income it never was.
- **Gate the card payment on "Paid from" like rule 7b** — rule 7b's gate protects the only record of
  money spent; a card payment is never that record, since the card's purchases are.
- **Refuse a lone `transfer` on both writers** — a row the commit retyped and the user moved to spend
  could come back only by a manual link, which needs a partner row.
- **A card-bill role on a spend category** — a category that means "transfer when covered" is the
  needs-link flag rejected above.

## Consequences

- One exclusion rule (`transaction_type`) for every aggregate.
- Symmetry and exactly-two stay service-enforced; every new writer must take the same path.
- The unique `(id, user_id)` target is an `Index(unique=True)`, not a `UniqueConstraint`: SQLite's batch
  mode could not create it and the composite FK in one pass.
- The investment FK keeps its historical name `fk_investment_transactions_switch_pair_same_user`.
- An unpaired `transfer` is an ordinary state, not an error: a deleted or unrestored partner leaves
  one (rules 5, 7), an unlinked card-bill debit is one (rule 7b), so is an unmatched card payment
  (rule 7c), and a user may write one (rule 7d). Its account balance still moves; spend and income
  exclude it.
- Which way an unlinked card-bill debit counts depends on "Paid from" and the card's committed
  statements at its own commit. Importing the card's statement later links the debit when the
  amounts match; otherwise the earlier debit stays spend in Other.
- A row the vocabulary flags by mistake (a bank debit committed uncategorised while covered, or a
  card credit committed uncategorised) stays a `transfer` after an unlink, the one case where "legs
  stay transfer after unlink" (rejected above) still happens; the user retypes it (ADR-0006 rule 15).

## History

- 2026-05-21 — Proposed: `transfer_pair_id` shared by F4a and F2.
- 2026-05-28 — Accepted; constraints landed in migration `0005_adr0002_transfer_pair_constraints`.
- 2026-11-15 — Rewritten to the rules-first template and widened to all paired rows (investment
  `pair_id`, renamed from `switch_pair_id` in 0026). Added: paired ⇒ `transfer` CHECK, pre-link type
  restored on unlink, backup restore as the third writer, IDCW wording.
- 2026-11-15 — Added the manual link (rule 11b) as a fourth writer; auto-link runs from either batch side.
- 2026-11-16 — Rule 9: a link also moves the row's category to `pre_link_category_id`; unlink restores it
  unless the user re-categorised the leg (migration `0044_pre_link_category`).
- 2026-11-16 — Rule 7: the auto-link candidate search skips a row in a refund link (ADR-0009 rule 13).
- 2026-11-16 — Rule 11b: the manual link refuses a row in a refund link; Locked-by tests landed
  (`POST /transactions/{id}/link`) and `GET /transactions/{id}/link-candidates` added.
- 2026-11-16 — Rule 5: the transaction delete paths (single, bulk, demo reseed) share one delete that
  releases partners outside the deleted set; import cancel and user teardown are the two named exemptions.
- 2026-11-17 — Rule 7b: an unlinked card-bill debit commits as an unpaired `transfer` when a
  committed statement of a card paid from that bank covers its date, else as spend in Other (A61).
- 2026-11-17 — Rule 7: restore pairs only confirmed legs; a pending leg is kept unpaired (A73).
- 2026-11-17 — Rule 7c: an unmatched card payment commits as an unpaired `transfer`, and the auto-link
  pairs a card leg that is `income` or `transfer`; rule 11b is offered from such a credit, marked by the
  server (A75).
- 2026-11-17 — Rule 7d: `POST` and `PATCH /transactions` accept `transfer` on an unpaired row (A77).
- 2026-11-17 — Rule 7b: the seeded "Credit Card Payments" spend category is retired, and import never
  prefills a category onto a flagged debit (A76).
- 2026-11-17 — Rule 5: the review-queue discard goes through the shared delete, in the same request as the
  batch commit (A70).
