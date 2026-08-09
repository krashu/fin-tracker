# ADR-0002: `transfer_pair_id` semantics + invariants

**Status**: Accepted (2026-05-28)
**Date**: 2026-05-21

## Problem

`Transaction.transfer_pair_id` ([backend/app/models/transaction.py:80](../../backend/app/models/transaction.py#L80)) is a nullable self-FK intended to link two rows that represent the same movement of money. The PRD does not pin two things:

1. **Semantic scope** — is the column used only for F4a auto-reconciliation (CC bill payment ↔ bank-side debit), or also for F2 user-initiated transfers (e.g. bank ↔ bank)?
2. **Integrity invariants** — what does the schema enforce vs leave to the service layer?

The current docstring documents the ambiguity but doesn't resolve it. The decision is forced by the first Alembic migration: at that point the column's role is documented at schema-freeze time, and changing its meaning later requires a data migration.

## Decision

**`transfer_pair_id` is reused for both F4a auto-reconciliation and F2 user-initiated transfers.** Same column, same shape: two `Transaction` rows pointing at each other, both excluded from spend totals on dashboards. The semantic is "this pair represents one movement of money, not two events."

### Invariants and where they're enforced

**1. Same-user ownership** — if `A.transfer_pair_id = B`, then `A.user_id = B.user_id`. Enforced via a **composite foreign key + composite unique constraint**, NOT a CHECK constraint (CHECK on either SQLite or Postgres cannot reference other rows). The schema pattern lands when the first Alembic migration is written:

```python
# On Transaction:
__table_args__ = (
    UniqueConstraint("id", "user_id", name="uq_transactions_id_user"),
    ForeignKeyConstraint(
        ["transfer_pair_id", "user_id"],
        ["transactions.id", "transactions.user_id"],
        name="fk_transactions_transfer_pair_same_user",
    ),
    ...
)
```

The `(id, user_id)` unique constraint makes the pair a referenceable target; the composite FK guarantees any non-null `transfer_pair_id` points at a row with matching `user_id`. Portable across SQLite and Postgres. DB-enforced.

**2. No self-reference** — `A.transfer_pair_id ≠ A.id`. Enforceable via a portable CHECK constraint:

```sql
CHECK (transfer_pair_id IS NULL OR transfer_pair_id != id)
```

**3. Exactly-two pairing** — if `A.transfer_pair_id = B`, then `B.transfer_pair_id = A`. No chains (A→B→C), no trees. **Not DB-enforceable** without triggers (SQLite has limited trigger support; Postgres triggers are non-portable). Service-layer responsibility. The composite FK above prevents cross-user chains but does not enforce symmetry within a user.

**4. Atomic population** — both rows' `transfer_pair_id` set in the same DB transaction. Service-layer concern (`import_service` for F4a, manual-transfer flow for F2).

### Population

- F4a rule-1 sets both rows automatically when reconciliation detects a CC-bill pair.
- F2 manual-transfer flow sets both rows on user action.

The "both at once, same txn" rule lives in the service layer (orchestration), not the schema.

## Trade-offs

**Benefits:** single mechanism, single index, single set of dashboard exclusion rules. The composite FK pattern (`(id, user_id)` unique → composite FK) is a portable primitive that can be reused for other future cross-table tenant-ownership constraints when v2 hosted mode lands.

**Drawbacks:** composite FK requires the `(id, user_id)` unique constraint — slight schema overhead. Symmetry and exactly-two pairing remain service-layer concerns and need integration tests to catch regressions. Provenance is lost — a paired row doesn't carry whether the pairing was auto (F4a) or manual (F2).

**Mitigation:** add `transfer_link_source ENUM('f4a_auto', 'f2_manual')` later if provenance becomes important — additive, no semantic change to existing data. Integration tests asserting symmetry + exactly-two land with `import_service` when it gets real content.

## Alternatives Considered

**Alternative 1 — F4a-only.** Locks the docstring's original commitment. F2 manual transfers would need a separate column or table. Rejected: schema already supports the broader interpretation at zero cost, and the user's stated usage pattern (bank↔bank manual transfers are more likely than F4a CC-bill reconciliation today) argues for F2 first.

**Alternative 2 — Separate column per use case** (`f4a_pair_id` + `f2_pair_id`). Explicit provenance, but two columns and two indexes to maintain. Rejected: the abstraction (single concept: "this pair is one movement") is the load-bearing idea; provenance is metadata.

**Alternative 3 — CHECK constraint joining on user_id for same-user invariant.** CHECK constraints on both SQLite and Postgres cannot reference other rows or tables. Rejected as a misconception (this ADR corrects an earlier draft of the plan).

**Alternative 4 — All four invariants enforced via triggers.** Triggers are not portably available — SQLite has limited support, Postgres triggers don't move with the schema across the v1 → v2 cutover. Rejected: composite-FK pattern + service-layer tests is the cleanest portable mix.

## Status note

Stays **Proposed** until either (a) the first F2 manual-transfer flow lands and the column gets a second use case, or (b) the first Alembic migration is written and the schema is frozen. Upgrade to Accepted at that point. Supersede or Reject if actual usage reveals a fault in the recommendation.

**Accepted 2026-05-28**: the column's *shape* was frozen at migration 0001 (Status §b satisfied retroactively); the *constraints* specified by this ADR landed in migration 0005 ([backend/alembic/versions/0005_adr0002_transfer_pair_constraints.py](../../backend/alembic/versions/0005_adr0002_transfer_pair_constraints.py)). One implementation note: the composite-unique target is declared as a unique `Index` rather than a `UniqueConstraint` so the migration can create it standalone before the batch FK swap — SQLite's `batch_alter_table` cannot create the composite unique target and the composite FK in one batch without `PRAGMA foreign_keys=OFF`, and PRAGMA is a no-op inside Alembic's open transaction. Functionally equivalent to a `UniqueConstraint` as an FK target on both SQLite and Postgres.
