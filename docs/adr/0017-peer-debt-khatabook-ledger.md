# ADR-0017: Peer debts and Khatabook balances are tracked in an auxiliary peer ledger linked to transactions

**Status**: Proposed
**Date**: 2026-11-18 (decided) · **Revised**: 2026-11-18
**Scope**: PRD §F3b (new), §Data model · `app/models/peer_contact.py`, `app/models/peer_entry.py`,
`app/services/peer_service.py`, `app/api/v1/peer_ledger.py`, `frontend/app/friends/`
**Supersedes / Related**: [ADR-0001](0001-sqlite-postgres-portability.md) (portability & BigInteger) ·
[ADR-0002](0002-transfer-pair-id-semantics.md) (unpaired transfers & inter-account isolation) ·
[ADR-0003](0003-multi-user-auth.md) (composite same-user FKs) · [ADR-0006](0006-f4-dedup-key.md)
(transaction fingerprint immutability) · [ADR-0009](0009-refund-as-signed-spend.md) (reimbursements as signed spend) ·
[ADR-0013](0013-cashback-is-a-rebate.md) (cashback rebate)

## Decision

Peer receivables and payables (money fronted for friends on credit cards, split restaurant bills, and
personal loans/borrowings like Khatabook or Splitwise) are tracked in an auxiliary, tenant-isolated
ledger (`peer_contacts` and `peer_entries`), optionally linked to `transactions`. A contact's net
balance is calculated dynamically via `SUM(you_gave) − SUM(you_got)`. Peer debt tracking isolates
personal consumption from friends' card usage, supports cashback offsets, never mutates core transaction
amounts or fingerprints, and does not hijack inter-account transfer pairs.

## Rules

1. **Model & Tenant Isolation**: Two tables:
   - `peer_contacts (id, user_id, name, phone, upi_id, notes, created_at, archived_at)`
   - `peer_entries (id, user_id, contact_id, transaction_id, date, amount_paise, direction, is_settlement, notes, created_at)`
   Both tables carry `user_id`. `peer_entries.contact_id` is a composite same-user FK
   `(contact_id, user_id) → (id, user_id)` (ADR-0003 rule 9) with `ON DELETE RESTRICT`. `name` is unique
   per user among active contacts via a partial unique index `uq_peer_contacts_active_user_name` on
   `(user_id, LOWER(TRIM(name)))` (`WHERE archived_at IS NULL`). No user-facing path hard-deletes a
   contact. User teardown (`services/user_teardown.py`) empties `peer_entries` in Phase 2 and
   `peer_contacts` in Phase 4. — *Enforced by*: DB FK + partial index + service teardown ·
   *Locked by*: `tests/models/test_models.py::test_peer_contact_cross_user_fk_rejected` (planned),
   `tests/api/test_auth_isolation.py` (planned)
2. **Strictly positive magnitude**: `peer_entries.amount_paise` is a `BigInteger` strictly `> 0`. Zero
   is rejected at all write paths. — *Enforced by*: DB CHECK `ck_peer_entries_amount_positive` + schema
   validation · *Locked by*: `tests/api/test_peer_ledger.py::test_peer_entry_zero_amount_rejected` (planned)
3. **Direction semantics**: `peer_entries.direction` is an `Enum('you_gave', 'you_got', name='peer_direction', native_enum=False, create_constraint=True)`:
   - `'you_gave'`: User paid or lent money (Receivable: Contact owes user). Positive contribution to balance.
   - `'you_got'`: Contact paid or lent to user (Payable: User owes contact). Negative contribution to balance.
   The contact net balance is `Σ(you_gave) − Σ(you_got)`. Positive means they owe the user; negative means
   the user owes them. Balance flips naturally if a settlement overshoots. — *Enforced by*: DB CHECK +
   service · *Locked by*: `tests/services/test_peer_service.py::test_contact_balance_sum` (planned)
4. **Transaction association (1:N & partial splits)**: `peer_entries.transaction_id` is a nullable
   composite same-user FK `(transaction_id, user_id) → (id, user_id)` to `transactions` with `ON DELETE NO ACTION`.
   - A single transaction may link to multiple `peer_entries` (e.g. splitting a ₹3,000 restaurant swipe
     ₹1,500 to contact A and ₹1,500 to contact B).
   - **Transaction Allocation Guard**: The sum of `amount_paise` across all `peer_entries` linked to a
     given `transaction_id` must not exceed `abs(transaction.amount_paise)` (422 otherwise).
   - `peer_entries.date` for a linked entry is seeded from the parent transaction's `date` at creation
     and is never synced if the transaction date edits. Standalone entries take the user-provided date.
   - Standalone entries (`transaction_id IS NULL`) are permitted for off-bank/cash debts or when a friend
     swiped their card on the user's behalf. — *Enforced by*: DB FK + service ·
   *Locked by*: `tests/api/test_peer_ledger.py::test_peer_entry_sum_cannot_exceed_transaction_amount` (planned)
5. **Deleting a transaction preserves debt history**: Deleting an underlying `transaction` sets
   `peer_entries.transaction_id = NULL` via service-level update in `services/transaction_delete.py::delete_owned_transactions`
   inside the same DB transaction boundary (matching `refund_of_id` and `transfer_pair_id`). The debt entry
   survives as an unlinked record. — *Enforced by*: service (`delete_owned_transactions`) ·
   *Locked by*: `tests/api/test_transactions.py::test_delete_transaction_nulls_peer_entry_link` (planned)
6. **Settlement entries**: A repayment is recorded as a `peer_entries` row with `is_settlement = TRUE`
   and `direction = 'you_got'`. To prevent double-counting against personal income/spend, a settlement
   is either recorded as a standalone entry (`transaction_id IS NULL`) or links to an incoming bank
   repayment whose `transaction_type` is set to `transfer` (or a positive `spend` refund in `Shared Expenses & Splits`).
   `POST /friends/contacts/{id}/settle` records an atomic full settlement entry for the exact current
   balance in a serialized transaction. — *Enforced by*: service ·
   *Locked by*: `tests/api/test_peer_ledger.py::test_settle_contact_balance` (planned)
7. **Spend Isolation & Friend Card Usage Reporting**:
   - **Option B Spend Isolation**: When fronting an expense for a friend on a card:
     - 100% fronted: User may flag transaction as an unpaired `transfer` (ADR-0002 rule 7d), excluding it
       from personal spend from day one.
     - Partial split: Transaction keeps its statement amount, but dashboard/overview reports:
       `Net Personal Spend = Gross Spend − Fronted Peer Entries`.
   - **Monthly Card Usage Tile**: A dedicated dashboard metric sums all credit card debits linked to
     `peer_entries` in that month, giving the user exact visibility into *"Card Used by Friends this month"*. —
     *Enforced by*: dashboard service ·
     *Locked by*: `tests/api/test_dashboards.py::test_fronted_card_usage_calculation` (planned)
8. **Cashback & Discount Offsetting**:
   - **Upfront Cashback / Discount Pass-Through**: When creating a peer entry from a card transaction with
     cashback or instant card discount (e.g. 5% Amazon ICICI), the split UI calculates the net amount:
     `amount_paise = original_paise − discount_paise`. The note records the original amount and discount
     applied (e.g. "₹10,000 Amazon order less 5% cashback").
   - **Statement Cashback Linking**: When a delayed cashback rebate arrives (ADR-0013 spend-kind Cashback
     row), the user may link it to a peer contact as a settlement credit (`direction = 'you_got'`). —
     *Enforced by*: service ·
     *Locked by*: `tests/api/test_peer_ledger.py::test_cashback_offset_on_peer_entry` (planned)
9. **Soft archive**: Archiving a contact (`archived_at IS NOT NULL`) hides the contact from active pickers
   while preserving ledger history and past transaction links. A contact with a non-zero balance requires
   explicit user confirmation to archive. — *Enforced by*: service + partial index ·
   *Locked by*: `tests/api/test_peer_ledger.py::test_archive_contact_with_balance_requires_confirmation` (planned)
10. **Query Efficiency & No Stored Balance Counter**:
    - The running balance of a contact is always computed via `SUM(CASE WHEN direction = 'you_gave' THEN amount_paise ELSE -amount_paise END)`
      over `peer_entries`. No materialized balance column exists on `peer_contacts` (preventing cache drift).
    - Contact list queries compute balances in a single `LEFT JOIN ... GROUP BY peer_contacts.id` query.
    - Indexed by:
      - `ix_peer_entries_user_contact` on `(user_id, contact_id)`
      - `ix_peer_entries_user_txn` on `(user_id, transaction_id)`
      - `ix_peer_entries_user_date` on `(user_id, date)`
      - `ix_peer_contacts_user_archived` on `(user_id, archived_at)`. —
      *Enforced by*: DB indexes · *Locked by*: `tests/test_migration_parity.py` (planned)

## Out of bounds

- No automated SMS or WhatsApp payment reminder gateways; manual WhatsApp text breakdown and UPI ID copy only.
- No multi-currency peer ledgers; all peer entries are in INR paise.
- No multi-party debt-simplification graph algorithms (e.g. Splitwise group debt minimization). Debts are
  strictly pairwise between the authenticated user and each contact.
- `peer_entries` are not accounts: they do not appear in `accounts` and do not have statement parsers.

## Why

In Indian daily spending, credit cards are frequently used to pay on behalf of friends to leverage card-specific
discounts (e.g. Amazon ICICI 5% cashback, dining bank offers), and UPI transfers are routinely used to split
bills or lend small amounts. Today, users have no way to answer:
1. *"How much did friends spend using my credit cards this month?"*
2. *"What was my actual personal spend versus what I fronted for friends?"*
3. *"Who currently owes me money, and how do we pass card cashback/discounts to them?"*

Conflating friends with `accounts` fails because friends are not statement-bearing financial accounts.
Conflating debts with `transaction_labels` (tags) fails because tags cannot express split amounts, cashback offsets,
or directionality. A dedicated auxiliary ledger provides running balances per contact with optional links
to bank and card transactions.

## Alternatives rejected

- **Treating each friend as an Account (`account_type = 'peer'`)**: Rejected. Overloads `accounts`, breaks
  statement reconciliation (ADR-0010), pollutes net worth liquid assets, and creates artificial `transfer_pair_id`
  legs for debts.
- **Pure Tag/Label based tracking (F3a Tags)**: Rejected. Tags cannot store amounts or running balances.
  They only filter transactions, requiring manual mental math to calculate who owes what.
- **Direct transaction mutation (splitting one `Transaction` row into two)**: Rejected. Violates ADR-0006
  (transaction fingerprint immutability) and breaks statement reconciliation against PDF running balances.
- **Materialized balance column on `peer_contacts`**: Rejected. Learning from Maybe Finance's snapshot cache
  concurrency failures, balances are aggregated via SQL `SUM` to guarantee 100% mathematical consistency.

## Consequences

- Users gain a dedicated `/friends` ("Friends & Shared Expenses") dashboard showing net receivables ("You will get") and payables ("You will give").
- Card transactions can be attributed in full or in part to one or more friends directly from the transaction detail dialog.
- Card cashbacks and merchant discounts can be seamlessly offset against friends' dues.
- Clear separation between true personal consumption and fronted card spending.

## History

- 2026-11-18 — Proposed. Designed for friend card usage, split bills, cashback offsets, and Khatabook-style peer balance tracking. Revised following Senior Staff Opus review with allocation guards, teardown integration, and service nulling.
