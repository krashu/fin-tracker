# ADR-0001: Every model and migration runs unchanged on SQLite and Postgres

**Status**: Accepted
**Date**: 2026-05-21 (decided) · **Revised**: 2026-11-15
**Scope**: PRD §Data model, §Tech stack, §Verification 12–13 · `app/models/base.py` (`NAMING_CONVENTION`,
`Base`, `TimestampMixin`, `utcnow_default`), `app/models/types.py` (`ScaledDecimal`), `app/core/db.py`
(`make_engine`), `app/core/db_errors.py` (`insert_skip_existing`), `app/core/clock.py`, `alembic/env.py`
**Supersedes / Related**: —

## Decision

v1 runs on SQLite and v2 on Postgres from the same Alembic chain and the same app code. Every model,
migration and runtime DB access follows the rules below, and dialect-specific code lives only in the
named seams: the connect listener in `core/db.py`, `core/db_errors.py`, `core/clock.py`, and the paired
`*_where` predicates of partial indexes.

## Rules

1. **Money is `BigInteger`**, named `*_paise` (INR) or `*_native_paise` (the instrument's own currency);
   never `Integer`, `Numeric` or float. — *Enforced by*: schema ·
   *Locked by*: `tests/models/test_money_columns.py::test_every_paise_column_is_biginteger`
1b. **Non-money decimals** (units, per-unit price, NAV, FX rate) are stored as scaled int64 through
   `ScaledDecimal` (units and price at 10⁸, FX at 10⁶), never `Numeric` or float. — *Enforced by*:
   schema · *Locked by*: `tests/test_migration_parity.py::test_migration_matches_models`
2. **String enums are `Enum(..., name=..., native_enum=False, create_constraint=True)`**: VARCHAR plus
   a named CHECK, never a native Postgres enum. — *Enforced by*: DB CHECK ·
   *Locked by*: `tests/test_migration_parity.py::test_migration_matches_models`,
   `tests/models/test_models.py::test_account_type_enum_rejects_unknown`
3. **Constraint and index names come from `NAMING_CONVENTION`** and are identical between
   `create_all` and `alembic upgrade head`. — *Enforced by*: tooling ·
   *Locked by*: `tests/test_migration_parity.py::test_migration_matches_models`, which compares FK, UQ,
   index and CHECK names
3b. **Where the convention cannot name a constraint** (a composite FK or UQ, or any CHECK), pass an
   explicit name, and for a CHECK pass only the suffix: `name="no_self_pair"` becomes
   `ck_transactions_no_self_pair`. — *Enforced by*: schema ·
   *Locked by*: `tests/test_migration_parity.py::test_migration_matches_models`
4. **Every app SQLite connection runs `PRAGMA foreign_keys=ON`** through the `connect` listener in
   `make_engine`, gated on `url.startswith("sqlite")`; tests build engines through `make_engine` too.
   — *Enforced by*: tooling · *Locked by*: `tests/models/test_models.py::test_transaction_fk_to_account_enforced`
4b. **Alembic migrations on SQLite run with `PRAGMA foreign_keys=OFF`** so batch rebuilds of referenced
   tables succeed; the chain's FK correctness is proven separately. — *Enforced by*: tooling
   (`alembic/env.py`) · *Locked by*: `tests/test_migration_parity.py::test_migrations_stairway`,
   `::test_cli_upgrade_with_referencing_data_succeeds`
5a. **Every `DateTime` column has a Python-side `default=utcnow_default`.** A `server_default=func.now()`
   may stay as the backstop for raw `INSERT`s, never as the sole writer. — *Enforced by*: schema ·
   *Locked by*: `tests/services/test_datetime_boundary.py::test_no_datetime_column_relies_on_the_db_clock_alone`
5b. **Only naive UTC reaches a `DateTime` column**: `clock.naive_utcnow()`, or an explicit
   `.replace(tzinfo=None)` of a UTC value. `clock.utcnow()` (aware) is for values that never reach a
   column: a JWT claim, a filename, a JSON string. — *Enforced by*: service ·
   *Locked by*: `tests/services/test_datetime_boundary.py::test_created_at_comes_from_the_app_clock`,
   `::test_no_aware_datetime_reaches_a_column`
5c. **`app/core/clock.py` is the only answer to "what time is it".** No `date.today()`,
   `datetime.now()` or `datetime.utcnow()` elsewhere in `app/`. `clock.today()` returns the
   [business date](../../GLOSSARY.md#dates--periods) (IST, `Asia/Kolkata`); stored instants stay UTC.
   — *Enforced by*: convention · *Locked by*: none (by choice — see Why)
5d. **A "now" read with no as-of date uses an undated query** (`fx_service.latest_rate`), not
   `rate_on(on=clock.today())`. — *Enforced by*: service · *Locked by*: PRD §Verification 13
6. **Runtime dialect branching lives only in `core/db_errors.py`** (the private `_dialect_insert`,
   behind `insert_skip_existing` and `insert_or_increment`). An external-source cache table
   (`fx_rates`, `benchmark_nav`) or a counter upsert calls those rather than cloning the branch.
   — *Enforced by*: service · *Locked by*: `tests/test_db_errors.py::test_postgres_index_name_matches`
7. **A partial index declares its predicate for both dialects**, `sqlite_where=` and
   `postgresql_where=`, with identical text. — *Enforced by*: schema ·
   *Locked by*: `tests/test_migration_parity.py::test_partial_index_where_clause_preserved`

## Out of bounds

- One-off scripts that target SQLite explicitly and are not on the migration path.
- Choosing `timestamptz` for v2: that is a cutover decision (see Consequences), not made here.
- Which tables carry `user_id`: that is ADR-0003.

## Why

The two engines differ on enum semantics, integer width, default constraint names and FK enforcement.
Each difference is silent on SQLite and wrong on Postgres:

- `Integer` is 64-bit on SQLite whatever you declare, and 32-bit on Postgres, where it overflows at
  ₹21,474,836.
- `ALTER TYPE … ADD VALUE` on a native enum cannot run inside a transaction, so adding a
  `transaction_type` would need downtime. A CHECK alters through an ordinary migration.
- Unnamed constraints get engine-default names, so the first Postgres migration renames half of them.
- libsqlite3 leaves FKs off by default, while Postgres always enforces them.
- An aware `datetime` reaches Postgres as `timestamptz` and is cast through the server's `TimeZone`.
  `func.now()` is the server's clock. Both already shipped a real bug: the 12-hour absolute session
  cap fired at 17h30m. SQLite silently strips the offset, so no test caught it.
- `date.today()` reads the host's timezone, so the native (IST) and Docker (UTC) deployments gave
  different answers for the same data. A UTC "today" is also yesterday between 00:00 and 05:30 IST,
  which misdates NAV stamps and holdings as-of dates. Hence an app clock with an IST business date.

Rule 5c has no lint gate by choice: ruff's `DTZ` rules flag the clock module itself and every
documented exclusion (`time.time()` in rate limiting, `perf_counter()` in middleware). One module and a
grep are cheaper to keep honest than a suppression list.

## Alternatives rejected

- **SQLite-only until v2, fix at cutover** — dozens of renames and type changes in one unreviewable
  migration; the risk compounds.
- **Native Postgres enums** — downtime for every added value and a manual dance for renames.
- **`Integer` money** — overflows at ₹21,474,836; the storage saving is trivial.
- **`Numeric` for units and FX** — dialect-dependent precision and Decimal/float leaks through drivers.
- **No naming convention** — every contributor's first migration renames constraints.
- **Doubled CHECK names kept as frozen** — any later `drop_constraint` that targets the documented
  name silently misses `ck_transactions_ck_transactions_no_self_pair`.

## Consequences

- Every new column, constraint, index and migration has a rule to check against; the parity test
  catches most drift.
- `ty` cannot tell aware from naive (both are `datetime`), so rule 5b rests on the boundary tests.
- **Cutover items**: revisit `insert_skip_existing` first, and consider `DateTime(timezone=True)`
  (`timestamptz`), which the Postgres project recommends. Naive UTC is the v1 portability choice, not a
  verdict against it.
- Reopen if v2 drops SQLite entirely: then rules 4, 4b, 6 and 7's SQLite halves can go.

## History

- 2026-05-21 — Accepted with five rules (money, enums, naming, FK pragma, clock).
- 2026-08 — Rule 5 grew after the session-cap bug; aware column writes reduced to `confirmed_at` and
  `archived_at`, then tolerated as cutover items.
- 2026-11-15 — Rewritten to the rules-first template. Promoted ScaledDecimal (1b), FK-off migrations
  (4b) and dual partial-index predicates (7) to rules. CHECKs take the suffix only; the two doubled
  names are renamed. Naive UTC became a hard rule with no tolerated exceptions. The business date
  became IST. Added a metadata lock on money columns.
