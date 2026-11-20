# ADR-0019: Investment accounts and per-account FIFO lot matching (CBDT Circular No. 768)

**Status**: Accepted
**Date**: 2026-11-20 (decided)
**Scope**: PRD §F7 (Investment tracking, accounts, portfolio segmentation), §F11 (Tax reporting) ·
`app/models/investment_account.py`, `app/models/investment_transaction.py`, `app/models/realised_lot.py`,
`app/services/holdings_service.py` (`rebuild_realised_lots`, `compute_holdings`, `_replay`),
`app/api/v1/investment_accounts.py`, `app/api/v1/investment_transactions.py`, `app/api/v1/holdings.py`
**Supersedes / Related**: ADR-0001 (portability & batch alter), ADR-0003 (tenant isolation & composite FKs),
ADR-0015 (FIFO lot matching)

---

## Context & Problem

Previously, `investment_transactions` and `realised_lots` were keyed solely on `(user_id, instrument_id)`.
All trades in an instrument across different brokers, depositories, or mutual fund folios were pooled together.

This created two critical flaws:
1. **Legal & Tax Divergence (CBDT Circular No. 768)**: Under Indian income tax law and CBDT Circular No. 768 (dated 20 December 1999), FIFO lot matching for demat securities is legally computed **per Demat account** (depository participant account), not globally across an investor's multiple brokers. For mutual funds, FIFO applies **per folio**. Pooling transactions globally across Zerodha, Groww, and INDmoney causes material cost-basis and holding-period divergence from official broker tax statements, Form 26AS, AIS, and TIS.
2. **Account Attribution**: Users could not filter holdings or performance by broker/account (e.g. "Show my Zerodha Demat holdings" vs. "Show my INDmoney US Brokerage portfolio").

---

## Decision

1. Introduce `investment_accounts` as an owned tenant table representing physical or demat investment buckets (e.g., Zerodha Demat, Groww Demat, INDmoney US Brokerage, AMC MF Folio, Manual FD, NPS).
2. Require every `investment_transaction` and `realised_lot` to be attributed to an `investment_account` owned by the same user, enforced via composite foreign key `(account_id, user_id) -> (investment_accounts.id, investment_accounts.user_id)`.
3. Scope FIFO lot matching queues strictly to `(account_id, instrument_id)`. Sells in Account A consume only acquisition lots opened in Account A.

---

## Rules

1. **Every investment transaction belongs to exactly one `investment_account`.**
   The column `account_id` on `investment_transactions` is `NOT NULL` with composite FK `(account_id, user_id) REFERENCES investment_accounts(id, user_id)` (ADR-0003 rule 10).
2. **FIFO lot matching is strictly per `(account_id, instrument_id)`.**
   `holdings_service._replay` and `rebuild_realised_lots` maintain independent lot queues per `(account_id, instrument_id)`. A `sell` or `switch_out` in one account cannot consume lots from another account, even for the identical `instrument_id`.
3. **Realised lots inherit account attribution.**
   `realised_lots` carries `account_id` (`NOT NULL`) with composite FK `(account_id, user_id) REFERENCES investment_accounts(id, user_id)`, guaranteeing that both `buy_txn_id` and `sell_txn_id` belong to the same account.
4. **Default account backfill for historical data.**
   Migration 0050 creates a default `InvestmentAccount(name="Default Portfolio", account_type="manual", provider="manual", external_account_ref="default")` for every existing user with transactions and assigns historical rows before enforcing `NOT NULL` and composite FK constraints.
5. **Tenant isolation.**
   `investment_accounts` enforces `UniqueConstraint("id", "user_id")` and `UniqueConstraint("user_id", "provider", "external_account_ref")`. Single-row reads filter `WHERE id = :id AND user_id = :uid` (miss = 404). Assigning another user's `account_id` returns 422. Deleting a user teardown cascades `investment_accounts`.

---

## Consequences

- **Tax Accuracy**: Conforms exactly to CBDT Circular No. 768 and broker contract note accounting.
- **Multi-Broker Transparency**: The UI can cleanly filter holdings by broker (`All` | `Zerodha` | `INDmoney` | `Manual`) and calculate account-level totals.
- **Migration**: Requires a 3-step Alembic batch migration on SQLite (`batch_alter_table`) to backfill the default account before enforcing the non-nullable composite FK.
