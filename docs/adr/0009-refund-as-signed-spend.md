# ADR-0009: A refund is a signed spend, not a `transaction_type`

**Status**: Accepted
**Date**: 2026-08-12 (decided) · **Revised**: 2026-11-15
**Scope**: PRD §F2, §F4a, §F8, §Verification 1 · `app/models/transaction.py` (`TransactionTypeStr`),
`app/schemas/transactions.py` (`sign_error`), `app/services/import_service.py` (`_map_type`),
`app/services/reconciliation_service.py` (`auto_link_cc_bill`), `app/services/backup_import_service.py`,
`app/services/transaction_delete.py` (`delete_owned_transactions`),
`app/parsers/backup_csv.py`, `app/api/v1/dashboards.py`, `frontend/lib/transaction-types.ts`
**Supersedes / Related**: cashback rule superseded by [ADR-0013](0013-cashback-is-a-rebate.md) ·
[ADR-0006](0006-f4-dedup-key.md) (sign rule on PATCH) · [ADR-0002](0002-transfer-pair-id-semantics.md)

## Decision

`transactions.transaction_type` is exactly `spend | income | transfer`. A
[refund](../../GLOSSARY.md#transaction-types--signs) is a `spend` row with a positive amount and an
[income reversal](../../GLOSSARY.md#transaction-types--signs) is an `income` row with a negative
amount, each in the same category as the row it reverses. Both are derived from the sign and never
stored as a type.

## Rules

1. **`transaction_type ∈ {spend, income, transfer}`**, a CHECK driven by the model Literal. —
   *Enforced by*: DB CHECK · *Locked by*: `tests/test_migration_parity.py::test_migration_matches_models`
2. **Refund ≡ `spend AND amount_paise > 0`; income reversal ≡ `income AND amount_paise < 0`.** Both
   are read-time derivations. — *Enforced by*: service ·
   *Locked by*: `tests/api/test_transactions.py::test_list_positive_spend_serialises_as_a_refund`;
   `::test_post_negative_income_is_a_reversal`
3. **`sign_error`: every type accepts any non-zero sign; zero is rejected for all types.** One shared
   helper for POST and PATCH (ADR-0006 governs which PATCH bodies run it). — *Enforced by*: schema
   validation · *Locked by*: `tests/api/test_transactions.py::test_post_zero_amount_422`,
   `::test_post_spend_positive_amount_is_a_refund`,
   `::test_patch_sign_and_type_are_validated_as_a_merged_pair`
4. **The sign rule binds every stored row.** Backup restore runs `sign_error`, and skips a violating
   row with a counted warning (the same treatment as an unknown type). Consumers may rely on "no
   zero amounts". — *Enforced by*: service ·
   *Locked by*: `tests/services/test_backup.py::test_restore_skips_a_zero_amount_row_with_a_warning`
5. **Aggregates discriminate on sign, never on a type.** Spend totals are the signed sum of `spend`
   rows; [gross spend](../../GLOSSARY.md#transaction-types--signs) is the negative `spend` rows only;
   `spend_ytd = gross + refund` (plus cashback, ADR-0013). Income totals are signed sums likewise, so reversals net them down.
   — *Enforced by*: service ·
   *Locked by*: `tests/api/test_dashboards.py::test_spend_plus_equal_refund_same_category_nets_to_exactly_zero`
6. **The F4a CC-bill candidate query states `amount_paise < 0` explicitly**; it never derives a sign
   from a type. — *Enforced by*: service ·
   *Locked by*: `tests/services/test_reconciliation_service.py::test_no_op_on_no_or_bad_candidate`
7. **`_map_type`**: parser `payment` → `income`; parser `deposit` (a bank credit) → `income`
   unless its text names cashback, then `spend` (ADR-0013 rule 1); every other line → `spend`,
   keeping the parser's sign (a purchase cashback is a positive `spend` per ADR-0013). It never
   produces a `refund` type.
   — *Enforced by*: service · *Locked by*: `tests/services/test_import_service.py::test_map_type`,
   `tests/parsers/test_cc_classify.py::test_chargeback_credit_stores_as_a_positive_spend`
8. **The parser vocabulary `TxnType` keeps `refund`** (`purchase | payment | refund | other |
   withdrawal | deposit`), with `RawTransaction` checking its sign (`withdrawal` ≤ 0,
   `deposit` ≥ 0, the bank column types); it is statement-side, upstream of `_map_type`. —
   *Enforced by*: service · *Locked by*: `tests/parsers/test_base.py::test_refund_negative_paise_raises`,
   `::test_withdrawal_positive_paise_raises`, `::test_deposit_negative_paise_raises`
9. **Refunds auto-tag as spend rows**: `AUTO_TAGGABLE_TYPES == {"spend"}`. — *Enforced by*: service ·
   *Locked by*: `tests/services/test_import_service.py::test_import_auto_tags_refund_row`
10. *Retired 2026-11-15.* Backup restore no longer reads a legacy `refund` type: the backup format
    is versioned (PRD §F10) and any other version is refused, so a `refund` cell is an unknown type
    like any other.
11. **`GET /transactions?transaction_type=…&amount_sign=…`** isolates refunds or reversals, scoped by
    user. — *Enforced by*: service ·
    *Locked by*: `tests/api/test_transactions.py::test_list_filter_by_transaction_type_and_amount_sign_isolates_refunds`
12. **Manual entry offers four `EntryDirection`s**, each mapped to `(type, sign)`: spend → (spend, −),
    refund → (spend, +), income → (income, +), income reversal → (income, −). — *Enforced by*:
    frontend · *Locked by*: `frontend/lib/transaction-types.test.ts`
13. **A refund may optionally link to the spend it reverses**: a nullable self-reference with a
    composite same-user FK (ADR-0003). The target must be a negative `spend` of the same user. The link
    is informational; netting never depends on it. Backup restore relinks a restored refund to its
    original — a row inserted by the same restore, or the one pre-existing row its original deduped
    against when that match is unambiguous — under the same checks as
    `PUT /transactions/{id}/refund-link`; restore never mutates a pre-existing row, so a link it cannot
    write is a restore warning counted in `links_not_restored`. `TransactionRead.refund_of_id` carries the
    link, and `GET /transactions/{id}/refund-candidates` lists the originals the PUT would accept.
    Deleting an original nulls the link on every refund pointing at it; the refund survives. A refund and its
    original deleted together go in one statement. — *Enforced by*: DB FK + service ·
    *Locked by*: `tests/api/test_transactions.py::test_refund_link_rejects_a_non_spend_target`,
    `tests/api/test_transactions.py::test_refund_candidates_offer_only_linkable_originals`,
    `frontend/app/expenses/transaction-dialog.test.tsx` ("links a refund to an original picked from its
    candidates"),
    `tests/services/test_backup.py::test_restore_links_a_new_refund_to_a_deduped_original`,
    `tests/api/test_transactions.py::test_deleting_the_original_clears_the_refund_link`,
    `::test_bulk_delete_releases_partners_outside_the_request`, `::test_bulk_delete_takes_both_legs_of_a_pair`,
    `tests/services/test_demo_seed.py::test_reseed_clears_a_refund_link_into_the_wiped_rows`

## Out of bounds

- No seeded or system "Refund" category on any axis.
- `investment_transactions.transaction_type` is a separate vocabulary with no refund concept.
- Cashback classification belongs to ADR-0013.

## Why

A fourth `refund` type was a label for "spend row, positive sign". Every consumer already bucketed
`spend` and `refund` together, and a refund parked in its own category leaves the original category
permanently inflated. Peers agree at schema level: Firefly III, Actual, YNAB, Lunch Money and GnuCash
all model a refund as a same-category inflow (a negative expense), never as a type. Income reversal
(a salary clawback) is the mirror case: double-entry debits the income account, so it is a negative
`income` in the same income category.

Backup restore is a file-upload boundary, so it validates. Without that, a hand-edited zip could store
a zero or a wrong-sign row, which every aggregate and the F4a guard would have to tolerate.

## Alternatives rejected

- **Keep `refund` as a fourth type** — no information a sign check lacks, one more vocabulary to sync.
- **A seeded "Refund" category** — guarantees the reversed category stays inflated.
- **Record an income reversal as a negative-signed spend** — inflates spend and leaves income
  overstated.
- **Reuse `transfer_pair_id` to link a refund** — ADR-0002 pins it to one movement of money.
- **Restore verbatim** — pushes bad-sign tolerance into every consumer.

## Consequences

- One representation for netting: a refund cannot drift into its own bucket.
- Accepted cost: a fat-fingered positive spend reads as a refund, and a negative income as a
  reversal. Only the user knows which was meant, and a type would not tell either.
- Reopen if a feature needs refunds as first-class records (partial refunds against several spends).

## History

- 2026-08-12 — Accepted. `refund` type removed (migration `0029_refund_as_signed_spend`); dashboards
  moved from type to sign; a seeded "Refund" category tried in the same pass was reverted.
- 2026-11-15 — Rewritten to the rules-first template. Income reversal adopted (`sign_error` is
  symmetric). Restore runs `sign_error`. Optional refund → original link added. Cashback moved to
  ADR-0013.
- 2026-11-16 — Rules 7–8: bank column types `withdrawal` / `deposit` added to `TxnType`; a deposit
  imports as income unless it names cashback.
- 2026-11-16 — Rule 13: deleting an original nulls its refunds' links; a refund deleted with its original
  goes in one statement (the shared transaction delete).
