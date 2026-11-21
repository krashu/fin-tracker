# ADR-0021: Shared reference pricing (Market NAV History) & Time-Weighted Return (TWR) Cache Foundation

**Status**: Accepted
**Date**: 2026-11-21 (decided)
**Scope**: PRD §F7 (Investment tracking), §F8 (Dashboards views 4–6), §F9 (Live updates) ·
`backend/app/models/market_nav_history.py`, `backend/app/services/market_data_service.py`,
`backend/app/jobs/backfill_nav.py`, `backend/alembic/versions/0052_market_nav_history.py`
**Supersedes / Related**: ADR-0001 (SQLite/Postgres portability), ADR-0003 (Tenant isolation & global tables),
ADR-0016 (FX stamped at transaction date), ADR-0018 (Foreign return waterfall)

---

## Context & Problem

Generating historical portfolio valuation curves, time-weighted returns (TWR), and moving alpha charts
against benchmarks requires daily pricing history across all held instruments over multi-year horizons
(typically 3–10 years).

If historical NAV/prices were stored per-user on individual `instruments`:
1. **Massive Storage Duplication**: Common mutual funds (e.g., Parag Parikh Flexi Cap `INF879O01027`,
   Nifty 50 Index funds) and equities (`NSE:TCS`, `NASDAQ:AAPL`) held across multiple household users
   would each store identical multi-year time-series of thousands of price points, causing 10x–100x redundant DB bloat.
2. **Floating-point Imprecision**: Storing NAV prices as floating-point numbers causes rounding drift in
   compound returns and valuation sweeps. Indian mutual funds publish official NAVs with 4 decimal places
   (e.g., ₹245.6789).
3. **Storage & Fetch Waste**: Storing synthetic rows for non-trading days (weekends, exchange holidays) in the
   database wastes disk I/O and creates artificial database bulk.

---

## Decision

1. **Global Reference Pricing Table (`market_nav_history`)**:
   Establish a global, system-wide table `market_nav_history` with no `user_id`. Queries for the same
   price key (`ISIN` for mutual funds, `EXCHANGE:SYMBOL` for stocks/ETFs) share the identical cached rows
   across all users in the system.
2. **Integer Micro-Units ($10^{-4}$ Precision)**:
   Store prices as 64-bit `BigInteger` (`nav_micros`) in $10^{-4}$ currency minor units.
   - 100 micro-units = 1 paise (or 1 cent).
   - 10,000 micro-units = 1 whole currency unit (₹1.0000 or $1.0000).
   - Zero floating-point columns.
3. **Sparse Storage Policy**:
   Only actual trading days with published market quotes are persisted in `market_nav_history`.
   Weekend and holiday valuations are never populated as duplicate DB rows.
4. **In-Memory LOCF (Last Observation Carried Forward)**:
   Downstream valuation services (e.g., forward-sweep TWR engine) apply LOCF in-memory across calendar date
   windows in $O(N)$ linear time.

---

## Rules

1. **`market_nav_history` is global-by-design (ADR-0003 exception).**
   Like `benchmarks`, `benchmark_nav`, and `fx_rates`, `market_nav_history` does not carry `user_id`.
   It is registered in `GLOBAL_TABLES` in `tests/owned_tables.py`. Teardown of a user never deletes market
   history rows.
2. **Natural key is `(price_key, nav_date)`.**
   Enforced via `UniqueConstraint("price_key", "nav_date", name="uq_market_nav_key_date")`.
   `price_key` is standardized as:
   - 12-character ISIN for Indian mutual funds (e.g., `INF879O01027`).
   - `EXCHANGE:SYMBOL` for exchange-traded securities (e.g., `NSE:TCS`, `BSE:500180`, `NASDAQ:AAPL`).
3. **NAV precision is strictly integer micro-units (`nav_micros`).**
   $\text{nav\_micros} = \text{round}(\text{Decimal}(\text{nav}) \times 10{,}000)$.
   Example: NAV ₹245.6789 $\to$ `2456789` micro-units $\to$ `24568` paise.
4. **Sparse storage; no synthetic calendar days.**
   Rows exist only for dates with published closing prices from upstream feeds (`mfapi`, `yahoo`, `amfi`, `nps`).
5. **LOCF applied in-memory at read/evaluation time.**
   When pricing an instrument for a date without a direct quote (e.g., Sunday), the latest quote on or before
   that date is carried forward in memory. If no quote exists on or prior to the date, the price is resolved
   as missing.
6. **Concurrent ingestion uses `insert_skip_existing`.**
   Backfill jobs ingest historical quotes idempotently using `insert_skip_existing` with conflict columns
   `["price_key", "nav_date"]`. Retrying or overlapping backfills never raise unique constraint errors.

---

## Consequences

- **Deduplicated Cache**: All users holding the same fund or stock benefit immediately from backfills executed by any other user or system job.
- **High Computational Precision**: Integer micro-units eliminate float inaccuracies across long-running compounding and valuation sweeps.
- **Storage Efficiency**: 10 years of trading days for an instrument requires only ~2,500 integer rows.
- **Portability**: Schema adheres strictly to ADR-0001 (SQLite/Postgres cross-compatibility with standard `BigInteger`, `Date`, and `DateTime`).
