# ADR-0018: Foreign return decomposes into asset price and currency gain; returns under 365 days suppress XIRR for HPR

**Status**: Accepted
**Date**: 2026-11-19 (decided)
**Scope**: PRD §F7 (Investment tracking), §F8 (Portfolio dashboards views 5 & 6) · `app/services/holdings_service.py`,
`app/services/portfolio_service.py`, `app/services/performance_service.py`, `app/services/nav_snapshot_service.py`,
`app/schemas/investments.py`, `app/schemas/performance.py`
**Supersedes / Related**: ADR-0015 (FIFO lot matching), ADR-0016 (frozen historical FX rates), ADR-0001 (portability & naive-UTC stamps)

---

## Decision

For foreign investments, total unrealized INR return is decomposed into **Currency FX Return** (the currency movement
on invested capital across surviving FIFO lots) and **Asset Price Return** (residual native capital gain converted at the
as-of rate), balancing to the total unrealized INR return with zero remainder. For holding periods under 365 days,
annualized XIRR is suppressed to prevent distorted projections, cleanly falling back to simple **Holding Period Return (HPR)**.

---

## Rules

1. **The closed-form waterfall identity is an exact invariant**:
   $$\text{price\_pnl\_inr\_paise} + \text{fx\_pnl\_inr\_paise} \equiv \text{unrealized\_pnl\_inr\_paise}$$
   Currency gain is evaluated per surviving open FIFO lot against its stamped acquisition rate:
   $$\text{fx\_pnl\_inr\_paise} = \sum_{i=1}^k \left( \text{to\_inr\_paise}(l_i.\text{cost\_paise}, R_{\text{current}}) - l_i.\text{invested\_inr\_paise} \right)$$
   and price gain is the exact residual:
   $$\text{price\_pnl\_inr\_paise} = \text{unrealized\_pnl\_inr\_paise} - \text{fx\_pnl\_inr\_paise}$$
   absorbing any $\pm 1$ paisa lot-rounding artifact. — *Enforced by*: service · *Locked by*:
   `tests/services/test_holdings_service.py::test_usd_holding_return_waterfall_balances_exactly (planned)`,
   `::test_mixed_rate_fifo_waterfall_preserves_lot_rates (planned)`

2. **INR holdings carry zero currency gain**: For domestic INR instruments, $R_{\text{hist}} \equiv 1$ and
   $R_{\text{current}} \equiv 1$, so $\text{fx\_pnl\_inr\_paise} \equiv 0$ and $\text{price\_pnl\_inr\_paise} \equiv \text{unrealized\_pnl\_inr\_paise}$.
   The UI completely suppresses currency breakdown elements for INR-only positions. — *Enforced by*: service ·
   *Locked by*: `tests/services/test_holdings_service.py::test_inr_holding_waterfall_is_zero_fx (planned)`

3. **Holding periods under 365 days suppress annualized XIRR**: If cashflow duration
   $\Delta t = (as\_of - \min(d_1, \dots, d_n)).\text{days} < 365$, `xirr` is set to `None` and the return metric
   falls back to Holding Period Return (HPR / Absolute Return):
   $$HPR = \frac{V_{\text{terminal}} + \sum_{CF_i > 0} CF_i - \sum_{CF_i < 0} |CF_i|}{\sum_{CF_i < 0} |CF_i|}$$
   with `return_type = "hpr"`. Positions with $\Delta t \ge 365$ solve for annualized XIRR with `return_type = "xirr"`. —
   *Enforced by*: service · *Locked by*: `tests/services/test_portfolio_service.py::test_xirr_suppressed_under_one_year (planned)`,
   `::test_xirr_computed_at_or_above_one_year (planned)`

4. **Zero-cost basis holdings return no percentage rate**: If total invested outflows equal zero (e.g. 100% bonus
   shares or zero-cost corporate actions), percentage return cannot be computed; `rate`, `xirr`, and `hpr` return `None`.
   The UI renders `—` with an advisory note rather than dividing by zero. — *Enforced by*: service ·
   *Locked by*: `tests/services/test_portfolio_service.py::test_zero_invested_returns_none_rate (planned)`

5. **Benchmark alpha comparison requires at least 365 days**: If the user's portfolio cashflows span $< 365$ days,
   scalar alpha comparison is suppressed: `portfolio_xirr = None`, `benchmark_xirr = None`, `alpha = None`, and
   `benchmark_unavailable_reason = "duration_too_short"`. — *Enforced by*: service · *Locked by*:
   `tests/services/test_performance_service.py::test_benchmark_short_duration_unavailable (planned)`

6. **External feeds are protected by polite pacing, hours gating, and manual cooldown**:
   Background and manual refreshes deduplicate unique symbols, gate requests by market trading hours, skip AMFI
   and FX calls when today's data is already recorded or on weekends, pace equity requests with a 300–500ms delay, and
   enforce a 5-minute cooldown on manual UI triggers. — *Enforced by*: service · *Locked by*:
   `tests/services/test_nav_snapshot_service.py::test_refresh_cooldown_and_deduplication (planned)`

---

## Out of bounds

- **Tax return calculations**: Capital gains for Indian tax filings convert under ADR-0014 (SBI TT buying rate on the
  transaction date and last day of preceding month), not the current quote waterfall.
- **Spending and INR accounts**: Currency decomposition applies strictly to foreign currency investments; domestic
  spending accounts have no currency component.
- **Intraday FX fluctuations**: FX rates remain single daily close quotes; intraday FX volatility is out of scope.

---

## Why

1. **Retail Investor Confusion**: For an Indian investor holding US tech stocks, a 15% INR return frequently conflates
   a 10% stock rise with a 5% currency tailwind. When a stock falls in USD but the rupee depreciates, INDmoney and other
   apps present a misleading green gain without explaining that the underlying company lost value. Decomposing the two
   restores full financial honesty.
2. **SEBI Regulatory Alignment**: Annualizing XIRR over 30 or 60 days produces unhinged figures (e.g. 350% on a 2-month SIP).
   SEBI mutual fund regulations (SEBI/HO/IMD/DF2/CIR/P/2018/16) mandate simple absolute return for periods under 1 year
   and CAGR/XIRR only for periods $\ge 1$ year.
3. **Public API Health**: AMFI, Yahoo Finance, and Frankfurter provide free public feeds. Unthrottled or frequent polling
   leads to IP blocks (HTTP 429/403). Cooldowns, deduping, and calendar gating keep fin-tracker resilient and polite.

---

## Alternatives rejected

- **Percentage-based component sum (Sharesight style)**: Sharesight deliberately allows component percentages not to sum
  to total percentage because compounding is non-linear. Rejected in favor of Portfolio Performance's closed-form
  monetary waterfall ($Price + FX \equiv Total$), which users find far easier to balance and reconcile down to the rupee.
- **Blended historical FX rate**: Computing average historical exchange rate across lots and applying it to total units.
  Rejected because it violates ADR-0016 (frozen historical transaction-date rates) and distorts FIFO cost basis.
- **Unbounded background cron polling**: Polling Yahoo and AMFI every 10–15 minutes around the clock. Rejected due to
  immediate risk of IP blacklisting on free tiers.

---

## Consequences

- Foreign holdings clearly communicate whether returns were driven by asset selection or currency movement.
- Short-term positions show stable, honest absolute returns instead of wild annualized swings.
- Background refresh runs reliably on market days without hammering upstream public services.

---

## History

- 2026-11-19 — Accepted and ratified following Staff Engineer and Senior Product Manager reviews.
