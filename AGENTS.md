# AGENTS.md — fin-tracker

Instructions for coding agents (GitHub Copilot, Codex, Cursor, …) working in this repo.
This is the tracked, shareable statement of the project's conventions.

Personal finance tracker — consolidates spending across Indian credit cards / bank accounts /
cash and tracks investments (Indian MFs, US stocks, manual entries) with XIRR and INR-rolled-up
dashboards. Self-hostable, multi-user (households of 2–5).

- `backend/` — Python 3.13 / FastAPI / SQLAlchemy 2.0 / Alembic on SQLite (Postgres in v2), managed by `uv`.
- `frontend/` — Next.js 16 App Router / React 19 / TypeScript / Tailwind 4 / TanStack Query v5, managed by `pnpm` (Node pinned in `.nvmrc`).

Exact versions live in `backend/uv.lock` and `frontend/package.json` — **read them rather than
trusting prose.**

## Commands

Run from the repo root. `Makefile` targets exist for each, but `make` is not always installed —
the underlying commands are authoritative.

| Task | Command |
|---|---|
| Backend tests | `cd backend && uv run pytest` |
| Backend tests, fast loop | `cd backend && uv run pytest --no-cov` |
| Backend lint | `cd backend && uv run ruff check .` |
| Backend typecheck | `cd backend && uv run python -m ty check app` |
| Frontend lint | `cd frontend && pnpm lint` |
| Frontend typecheck | `cd frontend && pnpm typecheck` |
| Migrations | `cd backend && uv run alembic upgrade head` |

**Never start a long-running server yourself** (`pnpm dev`, `uvicorn --reload`, `docker compose up`).
The human runs those in their own terminal; a backgrounded server will serve stale code and
mislead you. Verify backend behaviour with pytest, not by curling a live instance.

`pre-commit` is the real merge gate — 12 hooks, including **both** type-checkers (`ty check app`,
`tsc --noEmit`), ruff, ruff-format, eslint, and a fixture-redaction test. A type error blocks the
commit rather than surfacing in CI. Install steps: `docs/LOCAL_SETUP.md`.

Behind a TLS-inspecting corporate proxy the investment price feeds fail with
`CERTIFICATE_VERIFY_FAILED`; the git-ignored CA-bundle hook is documented in
`frontend/certs/README.md`. No application code is involved — don't "fix" it in `app/`.

## Where the specs live

- `PRD.md` is ~1,100 lines / ~20 K tokens. **Do not read it wholesale** — jump to the section you need.
  - Features: `F1 Statement import`, `F2 Manual transaction entry`, `F3 Auto-tagging`,
    `F3a Transaction labels`, `F4 Duplicate detection`, `F4a Reconciliation rules`, `F5 Categories`,
    `F6 Accounts`, `F7 Investment tracking`, `F8 Dashboards`, `F9 Live / reactive updates`,
    `F10 Export & Google Drive sync`, `F11 Tax statements & reporting`.
  - Architecture: `Data model`, `Tech stack`, `Production-grade essentials`, `Critical files`,
    `Build sequencing`, `Maintenance posture`, `Verification`.
- `docs/adr/` — architecture decisions. **Read the relevant ADR before touching its area; don't
  re-litigate a settled decision.**

| ADR | Subject |
|---|---|
| [0001](docs/adr/0001-sqlite-postgres-portability.md) | SQLite→Postgres portability — 5 rules for every model and migration |
| [0002](docs/adr/0002-transfer-pair-id-semantics.md) | `transfer_pair_id` is shared by F4a auto-reconciliation *and* F2 user transfers |
| [0003](docs/adr/0003-multi-user-auth.md) | Cookie JWT + rotating refresh; the per-row tenant-isolation contract |
| [0004](docs/adr/0004-f3-learning-lifecycle.md) | F3 auto-tag learning — one teach per decision |
| [0005](docs/adr/0005-type-checker-ty.md) | `ty` replaces `mypy` |
| [0006](docs/adr/0006-f4-dedup-key.md) | The F4 dedup key — the fingerprint formula is **frozen**; identity lives in the hash, multiplicity in `transactions.occurrence` |
| [0007](docs/adr/0007-transaction-field-editability.md) | All six user-visible transaction columns are editable on PATCH; identity edits recompute the fingerprint at `occurrence = 0`, and `origin_fingerprint` keeps an edit from re-staging on re-import |
| [0008](docs/adr/0008-f3-upi-merchant-normalisation-deferral.md) | *Superseded by 0011* — F3 UPI normalisation deferred past v1; don't touch `normalize_merchant` |
| [0009](docs/adr/0009-refund-as-signed-spend.md) | A refund is a `spend` row with positive `amount_paise`, not its own `transaction_type` — `transaction_type` is `spend \| income \| transfer` |
| [0010](docs/adr/0010-parsed-statement-return.md) | `StatementParser.parse()` returns `ParsedStatement` (rows + a `StatementSummary` of optional opening/closing balance + period), not a bare row list |
| [0011](docs/adr/0011-merchant-alias-layer.md) | Merchant alias layer — token-boundary canonicalisation downstream of frozen `normalize_merchant`; per-user seed dictionary at `hit_count = 0` |

## Working rules

### Money

Amounts are **`paise` / `cents` as int64, never `float`**. Field names end `_paise` (INR) or
`_native_paise` (investments). In the DB they are `BigInteger` — plain `Integer` overflows at
₹21,474,836.

Two conventions that recur and are easy to get backwards:

- **Refund sign** — a refund is not its own `transaction_type` (ADR-0009); it's a `spend` row
  whose `amount_paise` is opposite the original spend's, same category, so signed sums reduce
  category spend naturally. Don't invert without checking.
- **FIFO tie-breaking** on a partial sell — the PRD pins ordering by purchase date but not the
  tie-break. Ask rather than guess.

Investment transactions are stored in native currency with `fx_rate_to_inr` stamped at the
transaction date — **never re-derived at read time**. Spending is INR-only.

### Think before coding

Before anything non-trivial, state in 2–3 lines: which PRD section you're touching, which existing
files you'll edit, and any assumption not already nailed down. If a sign convention, column type,
test expectation, or ownership question is ambiguous — **ask, don't guess.**

### Simplicity first

PRD §Non-goals and the `Out of scope v1` notes are policy, not preference.

- No speculative error handling. Internal code trusts internal code. Validate at boundaries only:
  HTTP body, file upload, external API response.
- No abstraction ahead of the second concrete use. `StatementParser` exists because there is more
  than one parser.
- No config knobs for hypothetical flexibility. If it's not in the PRD, it's a fork.

### Surgical changes

A bug fix is a bug fix. Don't refactor on the way to it, don't rename a variable that's already
correct, don't reformat untouched lines. Adjacent cleanups go in a separate commit.

- When removing code, full-text search for callers first — the type-checkers catch only some uses.
- Prose duplicated across N files is a real defect class here. When you correct a comment or
  docstring, search for its copies.

### Definition of done

Every non-trivial change maps to a PRD §Verification step; if none exists, write one in the commit
message before writing code. "Done" means that step passes — not "it compiles". **Coverage floor is
75%** — one global `fail_under` with `source = ["app"]` and an omit list (`api/`, `models/`,
`schemas/`, `core/config.py`, `core/db.py`, `main.py`), so the floor lands effectively on
`services/` and `parsers/`.

Convert vague asks into testable criteria up front. *"Implement the ICICI CC parser"* becomes
*"uploads a redacted ICICI CC PDF, produces N rows matching the snapshot JSON, fingerprint stable
across runs, ruff/ty clean, coverage ≥ 75%."* Then loop independently — write, run, fix, re-run.

### Bug fixes

Reproduce → minimal repro → root cause → fix → regression test, in that order. Don't fix from
inspection alone.

### Dependencies

**Never run `uv add` / `remove` / `sync` / `lock` or `pnpm add` / `install` / `remove` unprompted** —
surface the proposed change first. Version policy is PRD §Maintenance posture: bounded ranges
(floor = current, ceiling = next breaking boundary), with exact pins in the committed lockfiles.

## Backend conventions (`backend/`)

### Layout

`app/api/v1/<domain>.py` — one flat module per domain, each exporting a `router`, all wired by a
single `include_router` line in `app/api/v1/router.py` and mounted under `/api/v1`. Shared
dependencies live in `app/api/deps.py`, deliberately outside `v1/`.

A new endpoint means a new module **and** its line in `v1/router.py`. Forget the second and it
**404s silently** — no error anywhere.

**There is no `app/routers/`.** Adding one creates a second router location the wiring doesn't know
about, with the same silent-404 result. Other packages: `core/`, `models/`, `parsers/`, `schemas/`,
`services/`, plus `main.py` and `middleware.py`. Pydantic schemas live in `app/schemas/`.

### Tenant isolation — ADR-0003

Every owned table carries `user_id`. Three rules, and getting them wrong is a **data leak**:

1. **Every read filters `user_id`.** No exceptions for "we derived these ids from the user anyway" —
   restate the predicate.
2. **Single-row fetch is `WHERE id = :id AND user_id = :uid`, and a miss returns 404** — not 403,
   not 200. A 403 is an existence oracle.
3. **A body FK pointing at another user's row is 422**, never silently honoured. Applies to
   `account_id`, `category_id`, `instrument_id`, and transfer legs.

Global-by-design (no `user_id`): `benchmarks`, `benchmark_nav`, `fx_rates`. Instrument NAVs are
per-user, on `instruments.current_nav`.

The contract is locked by `tests/api/test_auth_isolation.py`. A new owned table means a new row in
that matrix.

### Models & migrations — ADR-0001

Five rules, applied to every new column, constraint, index, and Alembic revision. SQLite v1 →
Postgres v2 is the reason.

1. **Money is `BigInteger`**, named `_paise` / `_native_paise`.
2. **String enums use `Enum(..., native_enum=False, create_constraint=True)`** — portable VARCHAR +
   CHECK. Native PG enums can't be altered transactionally.
3. **Constraint naming comes from `MetaData(naming_convention=...)`** on the declarative `Base`.
   Never hand-name.
4. **SQLite gets `PRAGMA foreign_keys=ON`** via the connect listener in `core/db.py`.
5. **Time comes from `app/core/clock.py`; stored timestamps are naive UTC.**

Every model carries `TimestampMixin`. `User` uses a UUID PK. `insert_skip_existing`
(`core/db_errors.py`) holds the only runtime dialect fork in `app/` — a new external-source cache
table calls it rather than cloning the branch.

**Don't run `alembic upgrade` / `downgrade` against anything but the test SQLite DB without explicit
confirmation.**

### Timestamps — ADR-0001 rule 5

**`app/core/clock.py` is the only answer to "what time is it."** Import the module —
`from app.core import clock` — then `naive_utcnow()` (the portable one), `utcnow()` (aware), or
`today()` (UTC date). No `date.today()`, no `datetime.now()`.

- **Write `naive_utcnow()` to any column Python computes on**, and give every `DateTime` column a
  Python-side `default=utcnow_default` (`models/base.py`) alongside its `server_default`. Aware is
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
- Suppress with **`# ty: ignore[rule-name]`**. **`# type: ignore` is not honoured** — `ty` errors
  straight through it.
- Don't widen the gate to `tests/`; that's its own decision. There is no `strict` bundle, so ruff's
  `ANN` rules carry annotation completeness.
- Before suppressing, check whether the diagnostic is a real bug. Four of seven were, on the dry run
  that motivated the swap.

### Logging & PII

`from app.core.log_config import get_logger` then `logger = get_logger(__name__)`, once per module.
Never `import structlog` directly.

Emit **structured events** — `logger.info("event_name", key=value)`, a stable event name plus
fields. **Never f-strings.** `configure_logging()` runs in the app lifespan and wires PII masking,
the standard fields, `LOG_FORMAT=json` in prod and `LOG_LEVEL` globally. Call sites add nothing.

**Never put PAN, account numbers, or card last-4 in logs, tracebacks, or error events.** Masking
happens at the logger, never the call site.

## Frontend conventions (`frontend/`)

shadcn/ui is **owned source** in `components/ui/`, not a package — include it when auditing
CSS-utility dependents.

### Server state is TanStack Query — and invalidation is manual

**There are no `use*` query hooks.** Components call `useQuery` directly against a shared key
convention. `["dashboards"]`-prefixed invalidation is what makes writes refresh reads.

Defaults live in `components/providers.tsx`: `staleTime: 30_000`, `refetchOnWindowFocus: false`.
Nothing refetches on its own — **if a mutation doesn't invalidate, the UI serves stale data until
the stale window lapses.**

`lib/queries/invalidate.ts` is a written contract, not a helper. Its docstring names every caller
that must invoke it. Any mutation that learns or mutates a rule calls `invalidateRules` — otherwise
`/settings/rules` and the tagging-health tiles serve a stale list and rule actions can 404 on a
deleted id. **Read that docstring before adding a mutation.**

### The tsc blind spot

Pydantic schemas are **hand-mirrored** as TS types in `lib/api/client.ts`. Drift is only half-caught:

- tsc **does** flag a TS field the backend dropped or renamed — a call site reads it.
- tsc **cannot** see a backend field the TS type never declared. No diagnostic, nothing renders it,
  suite stays green.

So a response schema gaining a field must be **diffed by hand**. Two dashboard fields were computed
and silently discarded this way.

The gate is thinner than it looks: `tsc --noEmit` is the whole of it. `eslint.config.mjs` declares
zero rules, so `pnpm lint` passes vacuously, and there are no frontend tests.

### Components

Dashboards and forms are **Client Components** (`"use client"`) — they need interactivity and
TanStack Query. Pages that are pure layout stay Server Components.

### Money display is masked by default

`Sensitive` (from `components/balance-visibility.tsx`) hides every amount behind `••••` for
over-the-shoulder privacy. That file's header docstring is the spec:

- Client Context + localStorage, **not** a cookie + `router.refresh()` like the theme toggle. The
  mask must be instant.
- **Defaults to hidden**, so a reload never flashes real amounts.
- `Sensitive` is a text swap and **can't wrap chart internals**. Charts mask via `useBalanceHidden()`
  plus `pointer-events-none select-none blur-sm` — `pointer-events-none` is load-bearing, since
  `ChartTooltipContent` renders raw values unless passed a `formatter`.

Any new surface that renders a rupee amount goes through one of those two paths.

### API client

All calls go through `request()` in `lib/api/client.ts`. Tokens live in httpOnly cookies, so every
call must send credentials, and **a 401 triggers one silent refresh + replay inside `request()`** —
never at a call site. Bypassing it means an expired-but-refreshable session logs the user out
instead of recovering.

### Charts

`lib/charts.ts` owns period → x-axis labelling and delegates money formatting to `lib/format.ts`
(`compactINR`) and windows to `lib/dates.ts`. **Don't format money inline in a chart component.**

## Never

- **Never commit `.env*`** (only `.env.example` is tracked), or **unredacted statement fixtures**.
  Real statements carry PAN, card last-4, account numbers, names and addresses —
  `scripts/redact_fixture.py` and the `fixture-redaction` pre-commit hook exist for this.
- **Never bypass git hooks** (`--no-verify`), amend published commits, or force-push.
- **Never log PII** — see §Logging & PII.
- **Never start a long-running server** — see §Commands.
