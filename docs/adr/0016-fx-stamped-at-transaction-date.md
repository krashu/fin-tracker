# ADR-0016: An investment row's FX rate is stamped at its transaction date and never re-derived

**Status**: Accepted
**Date**: 2026-11-15 (decided; codifies the convention in force since F7 FX) · **Revised**: 2026-11-16
**Scope**: PRD §F7 (FX layer) · `app/models/investment_transaction.py` (`fx_rate_to_inr`),
`app/services/fx_service.py` (`resolve_fx_rate_to_inr`, `rate_on`, `latest_rate`),
`app/services/investment_import_service.py`, `app/api/v1/investment_transactions.py`,
`app/services/holdings_service.py`, `app/services/portfolio_service.py`
**Supersedes / Related**: ADR-0014 (the tax rate, deliberately different), ADR-0015 (lots carry the
rate), ADR-0001 rules 1b and 5d

## Decision

Every investment transaction stores the INR conversion rate for its own date, stamped by the server when
the row is written. Historical figures — invested amount, XIRR cash flows, realised cost — convert each
row at its stamped rate. The only rates looked up at read time are the latest rate for current value,
and the tax rate of ADR-0014.

## Rules

1. **The server stamps `fx_rate_to_inr` on create and import** through `resolve_fx_rate_to_inr(currency,
   on=txn.date)`: exactly 1 for INR; otherwise the cached rate on or before that date (carried forward
   over weekends and holidays). The client never sends a rate. — *Enforced by*: schema validation
   (`extra="forbid"`) and service · *Locked by*:
   `tests/api/test_investment_transactions.py::test_usd_create_stamps_cached_rate`,
   `::test_fx_rate_field_rejected_by_extra_forbid`
2. **No cached rate on or before the date means the row is refused**: a 422 for manual entry, a
   rejected row for CSV import. Never stamp a later rate, today's rate or a default of 1 on a non-INR
   row. An import keeps no instrument it created only for rejected rows (as ADR-0015 rule 9 does for a
   refused instrument). — *Enforced by*: service · *Locked by*:
   `tests/api/test_investment_transactions.py::test_usd_create_without_cached_rate_422`,
   `::test_reinvestment_usd_without_cached_rate_422`,
   `tests/services/test_investment_import_service.py::test_usd_row_without_rate_rejected_and_batch_not_completed`,
   `::test_instruments_new_counts_only_instruments_that_kept_a_row`
3. **Historical figures use each row's own stamp.** Invested INR sums each surviving lot at its
   acquisition rate, and XIRR converts each cash flow at its own rate before summing. — *Enforced by*:
   service · *Locked by*:
   `tests/services/test_holdings_service.py::test_mixed_rate_fifo_invested_is_per_lot_historical`,
   `::test_usd_holding_converts_at_historical_and_as_of_rates`
4. **A stamp is never rewritten.** A later `fx_rates` backfill or correction leaves existing stamps
   alone. Investment PATCH edits the note only. Changing a date or amount means deleting and
   re-creating the row, which stamps afresh. — *Enforced by*: service and schema validation ·
   *Locked by*: `tests/api/test_investment_transactions.py::test_patch_note_only`
5. **Current value converts at `latest_rate`**, an undated read (ADR-0001 rule 5d). It is the only
   read-time rate on the portfolio side. — *Enforced by*: service · *Locked by*:
   `tests/services/test_holdings_service.py::test_usd_holding_converts_at_historical_and_as_of_rates`

## Out of bounds

- **Tax figures.** They convert at SBI's TTBR under ADR-0014, the one place a rate is looked up by the
  statute's date instead of the stamp.
- **Spending.** Spending is INR-only; there is no FX on `transactions`.
- **Currencies beyond USD and INR.** v1 supports only these two.

## Why

An investment's INR cost is a fact about the day it happened. Re-deriving it at read time would make
history depend on the state of the cache:

- a backfill that fills a missing weekday would silently move last year's invested figure and XIRR;
- a cache rebuilt on another machine, or from another source, would give a different portfolio for
  the same transactions.

A stamp freezes the conversion the user saw when the row was entered. That makes "same numbers on both
deployments" (PRD §Verification 13) hold for history. A third-party description of INDmoney matches
this: its XIRR converts each cash flow to INR at the prevailing rate on its transaction date. That
source is secondary; INDmoney publishes no documentation of its own.

Refusing the row when no rate is cached (rule 2) is better than a guess. A wrong stamp is permanent by
rule 4, while a refused row is fixed by refreshing rates and retrying.

## Alternatives rejected

- **Derive the rate at read time from `fx_rates`** — history moves whenever the cache changes; every
  read needs a join, and a missing date turns into a missing money figure.
- **Store INR amounts only** — loses the native figures that USD holdings, current value and ADR-0014's
  tax computation need.
- **Let the client send the rate** — an unchecked number becomes a permanent money input.
- **Default a missing rate to 1** — a silently wrong USD cost by a factor of about 85.

## Consequences

- Investment history is reproducible and portable; an FX cache can be dropped and rebuilt safely.
- A rate that was wrong when stamped stays wrong until the row is deleted and re-created.
- The canonical investment CSV does not carry the rate, so re-importing a file re-stamps from the
  local cache. Reopen if an investment export lands; it should carry the stamp.
- Reopen if v2 adds currencies, or if the cache source changes to one with different rates for the
  same dates.

## History

- 2026-11-15 — Written as an Accepted ADR in the gap sweep of the ADR rewrite. The convention was already
  stated in root `AGENTS.md` §Money and in the F7 code; this records its reasons and rules.
- 2026-11-16 — Rule 2: an import keeps no instrument it created only for rejected rows (A56).
