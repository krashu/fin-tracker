# Architecture Decision Records (ADR)

This directory holds the architectural decisions worth writing down for fin-tracker. Each ADR captures one decision with its problem, the choice, the trade-offs, and the alternatives that were considered and rejected.

## Purpose

ADRs help us:

- Document *why* a decision was made (not just *what* the code does).
- Provide context for a future contributor (or future-us) so the same argument doesn't get re-litigated.
- Track the evolution of the architecture as v1 ships and v2 lands.

## Format

Each ADR follows the structure in [template.md](template.md):

- **Title**: short, descriptive (`ADR-NNNN: <kebab-case-summary>`).
- **Status**: one of `Proposed | Accepted | Superseded | Deprecated | Rejected`.
- **Problem**: what's wrong / unclear / re-litigation-prone today, with code references where possible.
- **Decision**: the rule, plus when to apply it and when not to.
- **Trade-offs**: benefits, drawbacks, mitigation.
- **Alternatives Considered**: each rejected option with a one-line reason.

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
- Resolves a recurring ambiguity in [CLAUDE.md](../../CLAUDE.md) or [PRD.md](../../PRD.md).
- Documents a non-obvious *why* that the code alone can't carry.

Do *not* write an ADR for:

- Style preferences (use `ruff` config / `CLAUDE.md`).
- Single-function placement decisions (use a docstring).
- Reversible refactors (use a commit message).

## Index

| ADR | Title | Status | Date |
| --- | --- | --- | --- |
| [0001](0001-sqlite-postgres-portability.md) | SQLite v1 → Postgres v2 portability discipline | Accepted | 2026-05-21 |
| [0002](0002-transfer-pair-id-semantics.md) | `transfer_pair_id` semantics + invariants | Proposed | 2026-05-21 |
| [0003](0003-multi-user-auth.md) | Multi-user auth — cookie JWT + rotating refresh, per-row tenant scoping | Accepted | 2026-07-18 |
| [0004](0004-f3-learning-lifecycle.md) | F3 auto-tag learning lifecycle — one teach per decision, current-health metric | Accepted | 2026-07-19 |
| [0005](0005-type-checker-ty.md) | `ty` replaces `mypy` as the backend type-check gate | Accepted | 2026-07-29 |
| [0006](0006-f4-dedup-key.md) | The F4 dedup key — separator, occurrence ordinal, and the shared `normalize_merchant` contract | Accepted | 2026-07-30 |
| [0007](0007-transaction-field-editability.md) | Transaction field editability on PATCH — the mutable set, identity recompute, and `origin_fingerprint` | Accepted | 2026-08-03 |
| [0008](0008-f3-upi-merchant-normalisation-deferral.md) | F3 UPI merchant normalisation — measured non-convergence, deferred with a trigger | Proposed | 2026-08-03 |
