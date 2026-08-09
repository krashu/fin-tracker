# ADR-0001: SQLite v1 → Postgres v2 portability discipline

**Status**: Accepted
**Date**: 2026-05-21

## Problem

fin-tracker ships v1 on SQLite (single-user, local-first) and is planned to move to Postgres in v2 (hosted mode). The two engines diverge on enum semantics, integer width, default constraint naming, and foreign-key enforcement defaults. Without a consistent portability discipline, the v2 migration produces noisy Alembic diffs, runtime data corruption (silent integer overflow), or downtime (`ALTER TYPE` on native PG enums isn't transactional).

The codebase already encodes the right decisions in load-bearing ways, but none of them are obvious from reading the code in isolation. A contributor adding a new model in v0.4 will reach for `Integer` and `native_enum=True` (Postgres default) and silently break the migration.

### Where these rules live in code

- `MetaData(naming_convention=...)` — [backend/app/models/base.py:14-24](../../backend/app/models/base.py#L14)
- `Enum(..., native_enum=False, create_constraint=True)` — [backend/app/models/transaction.py:52-63](../../backend/app/models/transaction.py#L52), [backend/app/models/account.py:34-58](../../backend/app/models/account.py#L34)
- `BigInteger` for `_paise` columns — [backend/app/models/transaction.py:51](../../backend/app/models/transaction.py#L51), [backend/app/models/account.py:48](../../backend/app/models/account.py#L48)
- `PRAGMA foreign_keys=ON` connect listener — [backend/app/core/db.py:42-46](../../backend/app/core/db.py#L42)
- `insert_skip_existing` — the dialect-aware `ON CONFLICT DO NOTHING` bulk insert used by
  the external-source caches (`fx_rates`, `benchmark_nav`) —
  [backend/app/core/db_errors.py](../../backend/app/core/db_errors.py). This holds the
  **only runtime `if dialect == "sqlite" / elif "postgresql"` fork in `app/`**, so it is
  the first thing a Postgres cutover must revisit. A new external-source cache table
  should call it rather than clone the branch — cloning is how the two pre-existing
  copies came about.
- `clock.utcnow` / `naive_utcnow` / `today` —
  [backend/app/core/clock.py](../../backend/app/core/clock.py), the only answer to "what time
  is it", plus `utcnow_default` in
  [backend/app/models/base.py](../../backend/app/models/base.py) which is the column-default
  seam. Rule 5 below. Like `insert_skip_existing` this centralises a place where the two
  engines genuinely differ; the module docstring lists what it deliberately does **not** own,
  so the "one answer" claim stays checkable rather than aspirational.

## Decision

**Every model and migration follows five rules.**

**1. Money is `BigInteger` (int64) paise/cents.** Field names end `_paise` (INR) or `_native_paise` (investments). `Integer` (int32) overflows at ~₹21,474,836 — short by orders of magnitude for any non-trivial portfolio.

**2. String enums use `Enum(..., native_enum=False, create_constraint=True)`.** This generates a portable VARCHAR + CHECK constraint rather than a Postgres-native enum type. Native PG enums can't be altered transactionally (`ALTER TYPE … ADD VALUE` must run outside a txn), so adding a new `transaction_type` post-launch would force downtime. CHECK constraints are alterable via standard migrations.

**3. Constraint naming is explicit via `MetaData(naming_convention=...)` at the declarative `Base`.** Alembic autogenerate without a naming convention produces SQLite-default constraint names that get renamed to Postgres defaults on first PG migration — diff noise that's hard to review and easy to break. With the convention, names are stable across DBs.

**4. SQLite gets `PRAGMA foreign_keys=ON` at every connection.** libsqlite3 leaves FKs off by default (back-compat with very old SQLite versions); Postgres always enforces them. A SQLAlchemy `connect` event listener gated on `url.startswith("sqlite")` equalizes behavior so `ON DELETE` cascades and FK violations fire identically in dev/test and prod.

**5. Time comes from `app/core/clock.py`, and stored timestamps are naive UTC.** Three parts, and the reason they are one rule is that all three break the same way — silently on SQLite, wrongly on Postgres:

- **No `func.now()` as the *sole* writer of a column Python reads back.** Every `DateTime` column may keep a `server_default` (it is the backstop for raw `INSERT`s that bypass the ORM, and `tests/test_migration_parity.py` compares DB-side defaults, so removing one is a schema change) — but it must *also* carry a Python-side default, `default=utcnow_default` from [base.py](../../backend/app/models/base.py). `func.now()` is the *database server's* clock: on Postgres with `TimeZone=Asia/Kolkata` it returns IST wall-clock into a `TIMESTAMP WITHOUT TIME ZONE`, and any Python arithmetic against it is then off by the server's offset. That shipped as a real security-control break — the OWASP absolute session cap in `rotate_session` fired at 17h30m instead of 12h.
- **Write naive UTC to any column Python computes on.** Use `clock.naive_utcnow()`. An *aware* value is not portable here: `DateTime` has no bind processor, so it reaches the driver as `timestamptz` and Postgres assignment-casts it through the server's `TimeZone` — the same offset bug by another route. SQLite silently strips the offset instead, which is why this is invisible in the test suite.
- **No `date.today()`.** It reads the *host's* timezone, so the native (IST) and Docker (UTC) deployments answered differently for identical data. Use `clock.today()` for a genuine as-of anchor — and for a "now" view with no as-of date, prefer a read with no date predicate at all (`fx_service.latest_rate`, not `rate_on(on=today)`), because carry-forward turns a one-day shift into a missing rate and then into a wrong money number.

**Scope, honestly:** what lands *on disk* is naive UTC. `clock.utcnow()` (aware) is still the app-level shape at **7 call sites**, covering exactly two columns — `confirmed_at` and `archived_at` — which SQLite normalizes for us today and which are **Postgres-cutover items**, not settled. `nav_updated_at` was the third until every one of its writers went naive; and those writers are also why this sentence used to say "~11", conflating `clock.utcnow()` calls with aware column writes in general — `nav_snapshot_service` wrote an aware `datetime` literal and an aware quote timestamp, neither of them a `clock` call. The two readings now coincide: 7 either way. `clock.naive_utcnow()` is the dialect-independent one; prefer it for anything new. `clock.utcnow()` is correct where the value never reaches a column (a JWT claim, a filename, a JSON string).

`ty` cannot help: aware and naive are both `datetime`. The gate is `tests/services/test_datetime_boundary.py`, which pins the app clock to a fixed instant and asserts the stored value equals it — the only assertion that distinguishes the two clocks on SQLite, where they are identical in value.

**When to apply:**
- Every new model column / constraint / index.
- Every new Alembic migration.

**When NOT to apply:**
- One-off scripts that target SQLite explicitly and aren't part of the migration path.

## Trade-offs

**Benefits:** Alembic migrations stay portable across the v1 → v2 cutover; no silent integer overflow; enum evolution doesn't require downtime; FK constraints fire identically across dev (SQLite) and prod (Postgres).

**Drawbacks:** `BigInteger` is 8 bytes vs 4 for `Integer` (negligible for a personal-finance dataset). `Enum(native_enum=False)` foregoes Postgres-side strict-type enforcement at the type level (the CHECK constraint provides equivalent enforcement). The pragma listener is SQLite-specific code in a layer that aspires to dialect-neutrality.

**Mitigation:** The listener is gated on `url.startswith("sqlite")` so Postgres connections skip it cleanly. `Enum(name=..., create_constraint=True)` keeps the constraint visible to Alembic autogenerate, so renames flow through migrations the normal way. Money fields are always `_paise` / `_native_paise` so the column name hints at the unit — `Integer paise` would be unambiguous in code review.

## Alternatives Considered

**Alternative 1 — SQLite-only until v2, fix on cutover.** Would generate dozens of constraint renames and column-type changes in the first Postgres migration, hard to review and easy to break. Rejected because cutover risk compounds vs the marginal cost of getting it right now.

**Alternative 2 — Native Postgres enums (`native_enum=True`) for prod, accept the rewrite at v2.** Would require dropping/recreating enum types during migration, with downtime for adds and a manual rename dance for renames. Rejected because zero-downtime enum evolution is a recurring need (PRD's `transaction_type` set may grow with v0.5 investment flows).

**Alternative 3 — `Integer` for money + multiply-by-100 in app.** Overflows at ₹21,474,836.47 (USD ~$258K). Rejected for HNI portfolio support and because the storage-cost win is trivial.

**Alternative 4 — Skip the naming convention, accept Alembic diff noise.** Every contributor's first migration would rename half the constraints. Compounds across team members. Rejected because the convention is a one-time write that pays forever.
