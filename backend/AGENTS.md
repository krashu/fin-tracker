# AGENTS.md — fin-tracker backend

Backend conventions for `backend/`. The repo-wide rules — commands, money, tenant isolation,
logging & PII, and the never-list — are in the root [`AGENTS.md`](../AGENTS.md).

### Layout

`app/api/v1/<domain>.py` — one flat module per domain, each exporting a `router`, all wired by a
single `include_router` line in `app/api/v1/router.py` and mounted under `/api/v1`. Shared
dependencies live in `app/api/deps.py`, deliberately outside `v1/`.

A new endpoint means a new module **and** its line in `v1/router.py`. Forget the second and it
**404s silently** — no error anywhere.

**There is no `app/routers/`.** Adding one creates a second router location the wiring doesn't know
about, with the same silent-404 result. Other packages: `core/`, `models/`, `parsers/`, `schemas/`,
`services/`, plus `main.py` and `middleware.py`. Pydantic schemas live in `app/schemas/`.

### Models & migrations — ADR-0001

The rules apply to every new column, constraint, index, and Alembic revision; the same chain must run
on SQLite (v1) and Postgres (v2). The ADR has the full numbered list and what locks each one.

1. **Money is `BigInteger`**, named `_paise` / `_native_paise`. Units, prices, NAV and FX rates are
   `ScaledDecimal`, never `Numeric` or float.
2. **String enums use `Enum(..., native_enum=False, create_constraint=True)`** — portable VARCHAR +
   CHECK. Native PG enums can't be altered transactionally.
3. **Constraint names come from `MetaData(naming_convention=...)`** on the declarative `Base`. Name
   one by hand only where the convention can't (composite FK/UQ, every CHECK), and give a CHECK just
   the suffix: `name="no_self_pair"`, never `name="ck_transactions_no_self_pair"`.
4. **SQLite gets `PRAGMA foreign_keys=ON`** via the connect listener in `core/db.py`; migrations run
   with it off (`alembic/env.py`).
5. **Time comes from `app/core/clock.py`; stored timestamps are naive UTC.**
6. **A partial index carries both `sqlite_where=` and `postgresql_where=`**, same text.

Every model carries `TimestampMixin`. `User` uses a UUID PK. `core/db_errors.py`
holds the only runtime dialect fork in `app/` (`insert_skip_existing`, `insert_or_increment`) — a new
external-source cache table or counter upsert calls those rather than cloning the branch.

**Don't run `alembic upgrade` / `downgrade` against anything but the test SQLite DB without explicit
confirmation.**

### Timestamps — ADR-0001 rules 5a–5d

**`app/core/clock.py` is the only answer to "what time is it."** Import the module —
`from app.core import clock` — then `naive_utcnow()` (the portable one), `utcnow()` (aware), or
`today()` (the IST business date). No `date.today()`, no `datetime.now()`.

- **Only naive UTC reaches a `DateTime` column** — write `naive_utcnow()`, and give every
  `DateTime` column a Python-side `default=utcnow_default` (`models/base.py`) alongside its
  `server_default`. Aware is
  not portable: Postgres assignment-casts it through the *server's* `TimeZone`, so the value comes
  back shifted. That shipped as a real security break once (a 12h session cap firing at 17h30m).
- **`server_default=func.now()` stays** on those columns as the raw-`INSERT` backstop, and
  `tests/test_migration_parity.py` compares DB-side defaults — removing one is a schema change, not
  a cleanup. Never `func.now()` as a column's *sole* writer.
- **Don't compare stored datetimes at finer than day granularity.**
- Some `.date()` calls (in `holdings_service.max_staleness_days` and `nav_snapshot_service`) are
  mandatory `date`↔`datetime` conversions plus deliberate day-truncation, **not** naive/aware scars.
  Deleting them is an instant `TypeError`. Don't "clean them up".
- **Don't add a clock.** Call `clock`. If you need a shape it doesn't have, add it there with a
  reason — not at the call site.
- The type-checker cannot catch any of this — aware and naive are both `datetime`. The gate is
  `tests/services/test_datetime_boundary.py`.

### Types — ADR-0005

- The type-checker is Astral's **`ty`, not mypy**. Invoke as `uv run python -m ty check app`; not
  the bare `ty` shim, which goes missing from the Windows venv. Warnings fail the gate.
- Suppress with **`# ty: ignore[rule-name]`** only. `# type: ignore` is banned in `app/`, bare
  by ruff `PGH003` and coded by the `no-type-ignore-in-app` pre-commit hook: `ty` honours the bare
  form, silently hiding a real error, and ignores the coded one.
- The gate covers `app/` only; widening it to `tests/` reopens when `ty` reaches 0.1. There is no
  `strict` bundle, so ruff's `ANN` rules carry annotation completeness.
- Before suppressing, check whether the diagnostic is a real bug. Four of seven were, on the dry run
  that motivated the swap.
