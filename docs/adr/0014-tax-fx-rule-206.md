# ADR-0014: Foreign-asset capital gains convert at SBI's TT buying rate under Rule 206, not the stamped rate

**Status**: Proposed
**Date**: 2026-11-15 (proposed) · **Revised**: —
**Scope**: PRD §F7 (realised-gain classification), §F11 (capital-gains statement) · the planned F11
tax module and SBI TTBR reference table (no code yet) · reads `InvestmentTransaction.amount_native_paise`,
`fees_native_paise` and the persisted lot-match (`realised_lots`)
**Supersedes / Related**: ADR-0015 (FIFO supplies the lots), ADR-0016 (the stamped rate this one
does not use), ADR-0001 (rates are `ScaledDecimal`), ADR-0003 (global tables)

## Decision

The F11 capital-gains statement converts a foreign asset's gain to INR at the **State Bank of India
telegraphic-transfer buying rate (TTBR) on the last day of the month before the month of transfer**
(Rule 206 of the Income-tax Rules 2026; Rule 115 of the 1962 Rules for transfers before 1 April 2026).
This rate is looked up at report time and is never the transaction-date `fx_rate_to_inr` that drives
holdings, XIRR and the portfolio. **Proposed** until a CA rules on how cost of acquisition converts.

## Rules

1. **The specified date is the last day of the month immediately before the month the asset is
   transferred**, and the rate is SBI's TTBR on that date. The regime follows the sale date: Rule 206
   from 1 April 2026, Rule 115 before it. — *Enforced by*: service ·
   *Locked by*: `tests/services/test_tax_fx.py::test_specified_date_is_month_end_before_transfer` (planned)
2. **TTBR lives in its own global reference table**, keyed by date and stored as `ScaledDecimal` at
   10⁶. It is never read from or written to `fx_rates` (the frankfurter cache) or
   `investment_transactions.fx_rate_to_inr`. — *Enforced by*: schema ·
   *Locked by*: `tests/services/test_tax_fx.py::test_tax_rate_never_touches_stamped_rate` (planned)
3. **No TTBR for the specified date means no tax figure.** The statement flags the sale as "rate
   missing" rather than substituting the stamped rate, a frankfurter rate or today's rate. Whether an
   earlier published rate may stand in (a holiday month-end) is part of the open question below. —
   *Enforced by*: service · *Locked by*: `tests/services/test_tax_fx.py::test_missing_ttbr_flags_sale` (planned)
4. **Provisional method, until the trigger fires:** compute the gain per sold lot in native currency
   (proceeds − FIFO cost, fees included) and convert that gain at the rule 1 rate — the literal reading
   of Rule 206, which converts "income". Every foreign-asset row carries a "provisional" mark. —
   *Enforced by*: service · *Locked by*: `tests/services/test_tax_fx.py::test_gain_converted_once_at_sale_ttbr` (planned)
5. **Each lot keeps its native cost**, so that either reading of the cost side can be computed at
   report time without a migration. — *Enforced by*: schema (`amount_native_paise`, `fees_native_paise`) ·
   *Locked by*: `tests/services/test_holdings_service.py::test_mixed_rate_fifo_invested_is_per_lot_historical`

## Out of bounds

- **Non-residents.** Rule 52 (separate cost, expense and consideration rates) applies to non-residents
  selling shares of an Indian company. The app models resident users only.
- **Foreign dividends.** Rule 206 Sl.5 (month before declaration or payment) governs them; the F11
  dividend summary takes that up when it covers USD rows.
- **Schedule FA / FSI preparation, loss set-off and anything that files a return.** F11 is a statement
  to reconcile against the broker's report, not tax advice.
- **INR assets.** They need no conversion.

## Why

Two different questions need two different rates. "What is my portfolio worth, and what did it
return?" uses the rate on each cash flow's own date (ADR-0016). "What gain do I report?" uses the
date the statute specifies. A single rate would make one of the two answers wrong.

Primary source: Rule 206, Income-tax Rules 2026 (G.S.R. 198(E), 20 March 2026). Sub-rule (1) converts
"any income … in foreign currency" at the TT buying rate on the specified date. Table Sl.6 sets the
date for capital gains: *"Last day of the month, immediately preceding the month … in which the capital
asset … is transferred"*. Rule 207(3)(b) defines the TT buying rate as **State Bank of India's**
([Gazette text](https://www.caalley.com/itax26/Income-Tax-Rules-2026.pdf), pp.1687–1688;
[Rule 206, Income Tax Department](https://wm.incometaxindia.gov.in/w/rule-206-1)).

**The open question is the cost side.** The rule converts the income and says nothing about
converting cost separately. Practitioners split three ways:

- **(A)** cost converts at the TTBR for the month before purchase;
- **(B)** one sale-side rate converts both proceeds and cost;
- **(C)** cost converts at the purchase-date rate.

Rule 4 implements (B), the literal reading, and rule 5 keeps (A) and (C) computable.

## Alternatives rejected

- **Reuse the stamped `fx_rate_to_inr`** — wrong rate and wrong date for tax; the figure would not
  reconcile with a CA's computation.
- **Re-stamp `fx_rate_to_inr` with TTBR** — breaks XIRR and holdings, which rest on the stamped rate.
- **Use a frankfurter / RBI reference rate as a TTBR proxy** — not the rate the rule names.
- **Wait for the CA before writing anything down** — F11 design would start without a target.

## Consequences

- F11 needs an SBI TTBR feed. SBI publishes no free API, so loading that table is its own piece of
  work, and an empty table means foreign sales show "rate missing" (rule 3).
- A foreign-asset sale can show a tax gain while the portfolio shows an INR loss, and the reverse.
  The statement must say why.
- **Resolving trigger**: a CA ruling (or CBDT guidance) on whether cost of acquisition converts
  separately, and whether a holiday month-end may use an earlier rate. On that ruling, fix rule 4,
  drop the "provisional" mark and move this ADR to Accepted.
- Reopen if a Budget amends Rule 206's table or the definition of TTBR.

## History

- 2026-11-15 — Proposed, from the India-context review of the ADR rewrite. Sale-side rule verified
  against the Gazette text of Rule 206; the cost side is pending a CA ruling.
