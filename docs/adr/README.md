# Architecture Decision Records (ADR)

This directory holds the architectural decisions worth writing down for fin-tracker. Each ADR captures one decision with its problem, the choice, the trade-offs, and the alternatives that were considered and rejected.

## Purpose

ADRs help us:

- Document *why* a decision was made (not just *what* the code does).
- Provide context for a future contributor (or future-us) so the same argument doesn't get re-litigated.
- Track the evolution of the architecture as v1 ships and v2 lands.

## Format

Each ADR follows [template.md](template.md), rules first:

- **Title**: the decision stated as a rule (`ADR-NNNN: <rule>`).
- **Header**: Status, Date (decided) and Revised, Scope (PRD sections plus files and symbols — never
  line numbers, which drift), and Supersedes / Related.
- **Decision**: one or two sentences an agent can act on without reading further.
- **Rules**: numbered and testable, each naming what enforces it and the test that locks it.
- **Out of bounds**, **Why**, **Alternatives rejected**, **Consequences**, **History**.

An ADR states the **target state**. Where the code disagrees, the ADR wins and the code is the bug.
A *Locked by* test marked `(planned)` does not exist yet; the code rewrite adds it. Domain terms are
defined once, in [GLOSSARY.md](../../GLOSSARY.md).

## Status semantics

- **Proposed**: the recommendation is on the table but not yet committed. A Proposed ADR documents the current best-thinking *plus* the trigger that will resolve it. When the trigger fires, the ADR is updated to Accepted (or Rejected / Superseded if the answer changes).
- **Accepted**: the decision is in force. New code should follow it.
- **Superseded**: replaced by a later ADR (link the replacement).
- **Deprecated**: still documents what was decided historically, but the rule is no longer applied (link the reason).
- **Rejected**: considered and explicitly not adopted.

## Naming convention

`NNNN-kebab-title.md`, sequential. The number reflects creation order, not importance.

## When to write an ADR

Write one for a decision that:

- Is hard to reverse (schema choices, dependency choices, top-level layout).
- Affects multiple files or future PRs (cross-cutting policy).
- Resolves a recurring ambiguity in [AGENTS.md](../../AGENTS.md) or [PRD.md](../../PRD.md).
- Documents a non-obvious *why* that the code alone can't carry.

Do *not* write an ADR for:

- Style preferences (use `ruff` config / `AGENTS.md`).
- Single-function placement decisions (use a docstring).
- Reversible refactors (use a commit message).

## Index

| ADR | Title | Status | Date |
| --- | --- | --- | --- |
| [0001](0001-sqlite-postgres-portability.md) | Every model and migration runs unchanged on SQLite and Postgres | Accepted | 2026-05-21 |
| [0002](0002-transfer-pair-id-semantics.md) | Paired rows are exactly two same-user legs, linked both ways and written together | Accepted | 2026-05-21 |
| [0003](0003-multi-user-auth.md) | Cookie-JWT auth with rotating refresh; every owned row is scoped to one user | Accepted | 2026-07-18 |
| [0004](0004-f3-learning-lifecycle.md) | A tag rule is taught once per user decision, and only by a user decision | Accepted | 2026-07-19 |
| [0005](0005-type-checker-ty.md) | `ty` is the only backend type-check gate, over `app/` | Accepted | 2026-07-29 |
| [0006](0006-f4-dedup-key.md) | A transaction's identity is a frozen four-field hash; multiplicity and provenance live beside it | Accepted | 2026-07-30 |
| [0007](0007-transaction-field-editability.md) | Transaction field editability on PATCH (merged into 0006) | Superseded by 0006 | 2026-08-03 |
| [0008](0008-f3-upi-merchant-normalisation-deferral.md) | F3 UPI merchant normalisation deferred (replaced by 0011) | Superseded by 0011 | 2026-08-03 |
| [0009](0009-refund-as-signed-spend.md) | A refund is a signed spend, not a `transaction_type` | Accepted | 2026-08-12 |
| [0010](0010-parsed-statement-return.md) | A statement parser returns one `ParsedStatement` — rows plus an always-present summary | Accepted | 2026-08-12 |
| [0011](0011-merchant-alias-layer.md) | Auto-tagging keys on a canonical merchant resolved through per-user aliases | Accepted | 2026-08-12 |
| [0012](0012-category-hierarchy.md) | Categories are two levels, capped; the hierarchy only groups, and archive is soft | Accepted | 2026-08-14 |
| [0013](0013-cashback-is-a-rebate.md) | Purchase cashback is a rebate on spending, recorded as a positive spend | Accepted | 2026-11-15 |
| [0014](0014-tax-fx-rule-206.md) | Foreign-asset capital gains convert at SBI's TT buying rate under Rule 206, not the stamped rate | Proposed | 2026-11-15 |
| [0015](0015-fifo-lot-matching.md) | Sells consume lots first-in-first-out, after same-day equity trades net out | Accepted | 2026-11-15 |
| [0016](0016-fx-stamped-at-transaction-date.md) | An investment row's FX rate is stamped at its transaction date and never re-derived | Accepted | 2026-11-15 |
| [0017](0017-peer-debt-khatabook-ledger.md) | Peer debts and Khatabook balances are tracked in an auxiliary peer ledger linked to transactions | Proposed | 2026-11-18 |
