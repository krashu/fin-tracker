# ADR-0013: Purchase cashback is a rebate on spending, recorded as a positive spend

**Status**: Accepted
**Date**: 2026-11-15 (decided) · **Revised**: 2026-11-15
**Scope**: PRD §F4a rule 3, §F5 (seed taxonomy), §F8 (CC YTD block) · `app/services/import_service.py`
(`_map_type`, `is_cashback_credit`), `app/services/provisioning.py` (default taxonomies),
`app/api/v1/dashboards.py` (YTD `cashback` accumulator)
**Supersedes / Related**: supersedes the cashback-as-income rule of [ADR-0009](0009-refund-as-signed-spend.md)
(0009 otherwise stands) · [ADR-0012](0012-category-hierarchy.md) (taxonomy) ·
[ADR-0004](0004-f3-learning-lifecycle.md) (corrections teach)

## Decision

A [cashback](../../GLOSSARY.md#transaction-types--signs) credit an issuer gives back on purchases is a
positive `spend` row in a spend-kind "Cashback" category. It nets spending down, exactly like a refund
(ADR-0009), and is never income. Referral and sign-up bonuses are not cashback: they are `income`.

## Rules

1. **A cashback credit is `transaction_type = 'spend'` with `amount_paise > 0`.** `_map_type` maps a
   positive credit whose merchant text names cashback to `spend`, never `income`. — *Enforced by*:
   service · *Locked by*: `tests/services/test_import_service.py::test_map_type`
2. **It is pre-filled into the seeded spend-kind "Cashback" category.** The category lives in the
   spend taxonomy in `provisioning.py`; the income taxonomy has no Cashback child. — *Enforced by*:
   service · *Locked by*: `tests/services/test_import_service.py::test_cashback_credit_lands_in_the_cashback_category`
3. **Referral, sign-up and other non-purchase bonuses are `income`.** The statement text cannot always
   tell them apart, so the user corrects one by picking an income category in review; that retypes it
   to `income` (ADR-0009's merged picker). — *Enforced by*: user correction · *Locked by*: none (a
   classification aid, not an invariant)
4. **The CC YTD block splits positive spend by category**: `cashback` is the positive `spend` rows in
   the Cashback category, and `refund` is the other positive `spend` rows. `spend_ytd = gross + refund +
   cashback`. The wire names stay. — *Enforced by*: service ·
   *Locked by*: `tests/api/test_dashboards.py::test_overview_cashback_ytd_is_spend_side`
5. **Existing data moves once**: a migration retypes `income` rows in the old income-kind Cashback
   category to positive `spend` in the new spend-kind one, and archives the old category (ADR-0012). —
   *Enforced by*: migration · *Locked by*: `tests/test_migration_parity.py::test_cashback_rows_move_to_spend`

## Out of bounds

- Reward points, and redemption of points into statement credit, are not modelled in v1; a redemption
  credit is cashback.
- No tax treatment is computed for cashback; F11 never reads it.
- How a refund nets is ADR-0009's; this ADR only says cashback takes the same path.

## Why

In Indian practice, purchase cashback is a reduction in the price paid, a rebate, not earnings: it is
not reported as income, and Indian apps (CRED, INDmoney) net it against spend. Counting it as income
overstates both income and spend by the same amount, and a household's "what did we really spend on
the card" figure is off by every rebate. Referral and sign-up bonuses are different: no purchase is
being reduced, so they are income.

## Alternatives rejected

- **Cashback as income** (ADR-0009's original rule; YNAB's convention) — inflates both sides, and
  diverges from Indian practice.
- **Cashback netted into the original merchant's category** — the credit rarely names the purchase
  it rewards, so the category is unknowable.
- **A fourth `cashback` transaction type** — the same mistake ADR-0009 undid for refunds.

## Consequences

- Card spend totals fall by rebates, matching what the household actually paid.
- The YTD block's `cashback` figure becomes a spend-side number; the frontend label stays "Cashback".
- Reopen if issuers start printing structured reward data that distinguishes rebates from bonuses.

## History

- 2026-11-15 — Accepted from the India-context review. Supersedes ADR-0009's cashback-as-income rule;
  referral and sign-up bonuses stay income.
