# AGENTS.md — fin-tracker

Instructions for coding agents (GitHub Copilot, Codex, Cursor, …) working in this repo.
This is the tracked, shareable statement of the project's conventions.

Personal finance tracker — consolidates spending across Indian credit cards / bank accounts /
cash and tracks investments (Indian MFs, US stocks, manual entries) with XIRR and INR-rolled-up
dashboards. Self-hostable, multi-user (households of 2–5).

- `backend/` — Python 3.14 / FastAPI / SQLAlchemy 2.0 / Alembic on SQLite (Postgres in v2), managed by `uv`.
- `frontend/` — Next.js 16 App Router / React 19 / TypeScript / Tailwind 4 / TanStack Query v5, managed by `pnpm` (Node pinned in `.nvmrc`).

Exact versions live in `backend/uv.lock` and `frontend/package.json` — **read them rather than
trusting prose.**

## Commands

Run from the repo root. `Makefile` targets exist for each, but `make` is not always installed —
the underlying commands are authoritative.

| Task | Command |
|---|---|
| Backend tests | `cd backend && uv run pytest -n auto` |
| Backend tests, fast loop | `cd backend && uv run pytest --no-cov -m "not real_pdf and not slow" -n auto` |
| Backend lint | `cd backend && uv run ruff check .` |
| Backend typecheck | `cd backend && uv run python -m ty check app` |
| Frontend lint | `cd frontend && pnpm lint` |
| Frontend typecheck | `cd frontend && pnpm typecheck` |
| Frontend tests | `cd frontend && pnpm test` |
| Migrations | `cd backend && uv run alembic upgrade head` |

**Never start a long-running server yourself** (`pnpm dev`, `uvicorn --reload`, `docker compose up`).
The human runs those in their own terminal; a backgrounded server will serve stale code and
mislead you. Verify backend behaviour with pytest, not by curling a live instance.

The fast loop is not the definition of done: it drops coverage instrumentation (~25% of the wall
clock), the real-PDF tests, and `slow`-marked tests. **Touching a migration means running the full
suite** — the fast loop skips every migration test that runs upgrade/downgrade steps (only the
model-drift and head-state checks stay in), including `test_migrations_stairway`, the only thing
proving every `downgrade()` in the chain reversible and where 0033's batch rebuild of `categories`
gets exercised under real FK enforcement.

`-n auto` (pytest-xdist) is on both suite commands but deliberately **not** in `addopts`: worker
startup costs ~17s, which pays for itself across the full suite and never on one file. So run a single
test or file **without** it, and use `-n0` when you need `--pdb`, which xdist does not support.
The dev container caps `-n auto` at 8 workers (`PYTEST_XDIST_AUTO_NUM_WORKERS` in
`devcontainer.json`): on the 15W laptop it was tuned on, throughput drops past ~7 processes.
Nothing needed making parallel-safe — every DB test builds its own `:memory:` engine — and coverage
combines across workers to the same total. `addopts` does carry `--durations=15`: it is the regression
radar that would have caught the 104ms fixture call this suite once hid.

`pre-commit` is the local gate — its hooks include **both** type-checkers (`ty check app`,
`tsc --noEmit`), ruff, ruff-format, eslint, a `# type: ignore` ban over `backend/app/`, and a
fixture-redaction test. A type error blocks the
commit. It does **not** run either suite (only the redaction test), so locally the 75% coverage
floor is enforced solely by a bare `uv run pytest` / `make test`, and vitest only by `pnpm test`.
Install steps: `docs/LOCAL_SETUP.md`.

The GitHub Actions in `.github/workflows/` re-run lint, both type-checkers, both suites (with the
coverage floor) and pre-commit — but only after the source is published, so they catch nothing
before it leaves this machine. They mirror the table above: when a command there changes, change
the matching `run:` step in `test-backend.yml` / `test-frontend.yml` too.

Behind a TLS-inspecting corporate proxy the investment price feeds fail with
`CERTIFICATE_VERIFY_FAILED`; the git-ignored CA-bundle hook is documented in
`frontend/certs/README.md`. No application code is involved — don't "fix" it in `app/`.

## Where the specs live

- `PRD.md` is long. **Do not read it wholesale** — jump to the section you need.
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
| [0001](docs/adr/0001-sqlite-postgres-portability.md) | SQLite→Postgres portability — numbered rules for every model and migration; naive-UTC instants, IST business date |
| [0002](docs/adr/0002-transfer-pair-id-semantics.md) | Paired rows (`transfer_pair_id`, investment `pair_id`) — exactly two same-user legs, linked both ways, written atomically; manual link needs equal and opposite amounts; unlink restores the pre-link type |
| [0003](docs/adr/0003-multi-user-auth.md) | Cookie JWT + rotating refresh (30-min idle / 12h cap); per-row tenant isolation backed by composite same-user FKs; demo login never over HTTPS; self-service account deletion reuses guest teardown |
| [0004](docs/adr/0004-f3-learning-lifecycle.md) | F3 learning — one teach per user decision, keyed on the canonical merchant; a bulk gesture teaches once per merchant |
| [0005](docs/adr/0005-type-checker-ty.md) | `ty` (not mypy) gates `app/` only; suppress with `# ty: ignore[rule]`, `# type: ignore` banned |
| [0006](docs/adr/0006-f4-dedup-key.md) | Transaction identity — the fingerprint formula is **frozen**; multiplicity in `occurrence`, provenance in `origin_fingerprint`; identity edits recompute the hash |
| [0007](docs/adr/0007-transaction-field-editability.md) | *Merged into 0006* — stub |
| [0008](docs/adr/0008-f3-upi-merchant-normalisation-deferral.md) | *Superseded by 0011* — stub |
| [0009](docs/adr/0009-refund-as-signed-spend.md) | A refund is a positive `spend`, an income reversal a negative `income`, same category — `transaction_type` is `spend \| income \| transfer` |
| [0010](docs/adr/0010-parsed-statement-return.md) | PDF statement parsers (card and bank) return `ParsedStatement` (rows + an always-present `StatementSummary`); bank rows checked against the running balance; uploads are never persisted |
| [0011](docs/adr/0011-merchant-alias-layer.md) | Auto-tagging keys on the canonical merchant — single-pass token-boundary aliases after frozen `normalize_merchant`; per-user seed dictionary |
| [0012](docs/adr/0012-category-hierarchy.md) | Categories are **two levels, capped** — one-hop rollup and colour inheritance, total *soft* archive cascade; "Other" is an unarchivable root |
| [0013](docs/adr/0013-cashback-is-a-rebate.md) | Purchase cashback is a positive `spend` in a spend-kind Cashback category; referral and sign-up bonuses are income |
| [0014](docs/adr/0014-tax-fx-rule-206.md) | *Proposed* — F11 foreign-asset gains convert at SBI's TT buying rate (Rule 206), never the stamped `fx_rate_to_inr`; cost side awaits a CA |
| [0015](docs/adr/0015-fifo-lot-matching.md) | FIFO lot matching after same-day `indian_equity` trades net out as intraday; same-date order is date → source position → id |
| [0016](docs/adr/0016-fx-stamped-at-transaction-date.md) | `fx_rate_to_inr` is server-stamped at the transaction date and never rewritten; a missing rate refuses the row |

## Working rules

### Money

Amounts are **`paise` / `cents` as int64, never `float`**. Field names end `_paise` (INR) or
`_native_paise` (investments). In the DB they are `BigInteger` — plain `Integer` overflows at
₹21,474,836.

Two conventions that recur and are easy to get backwards:

- **Refund and reversal sign** — neither is its own `transaction_type` (ADR-0009). A refund is a
  positive `spend` and an income reversal a negative `income`, each in the same category as the row
  it reverses, so signed sums net naturally; zero is rejected on every write path. Purchase cashback
  is a positive `spend` too (ADR-0013).
- **FIFO** (ADR-0015) — sells consume the oldest lot first; same-date lots go by source-statement
  position, then id, carried by insertion order. Insert imported rows in file order.

Investment transactions are stored in native currency with `fx_rate_to_inr` server-stamped at the
transaction date — **never re-derived or rewritten** (ADR-0016); tax uses SBI's TTBR instead
(ADR-0014). Spending is INR-only.

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

Layout, models & migrations (ADR-0001), timestamps and the `ty` type-checker live in
[`backend/AGENTS.md`](backend/AGENTS.md), loaded when you work under `backend/`. Read it before adding
an endpoint: one without its `include_router` line in `app/api/v1/router.py` 404s silently.
The two rules below stay here because breaking them leaks data.

### Tenant isolation — ADR-0003

Every owned table carries `user_id`. Three rules, and getting them wrong is a **data leak**:

1. **Every read filters `user_id`.** No exceptions for "we derived these ids from the user anyway" —
   restate the predicate. The only reads without it are the identity lookups: `sessions` by
   `token_hash`, `users` by id or email.
2. **Single-row fetch is `WHERE id = :id AND user_id = :uid`, and a miss returns 404** — not 403,
   not 200. A 403 is an existence oracle.
3. **A body FK pointing at another user's row is 422**, never silently honoured. Applies to
   `account_id`, `category_id`, `instrument_id`, category `parent_id`, labels, transfer legs and the
   refund → original link. The DB backs it up: every owned→owned FK is a composite same-user FK
   `(fk, user_id) → (id, user_id)`.

Global-by-design (no `user_id`): `benchmarks`, `benchmark_nav`, `fx_rates`. Instrument NAVs are
per-user, on `instruments.current_nav`.

The contract is locked by `tests/api/test_auth_isolation.py`. A new owned table means a new row in
that matrix, and the user teardown (`services/user_teardown.py`, shared by guest expiry and account deletion) must empty it.

### Logging & PII

`from app.core.log_config import get_logger` then `logger = get_logger(__name__)`, once per module.
Never `import structlog` directly.

Emit **structured events** — `logger.info("event_name", key=value)`, a stable event name plus
fields. **Never f-strings.** `configure_logging()` runs in the app lifespan and wires PII masking,
the standard fields, `LOG_FORMAT=json` in prod and `LOG_LEVEL` globally. Call sites add nothing.

**Never put PAN, account numbers, card last-4 or Aadhaar numbers in logs, tracebacks, or error
events.** Masking
happens at the logger, never the call site.

## Frontend conventions (`frontend/`)

In [`frontend/AGENTS.md`](frontend/AGENTS.md), loaded when you work under `frontend/`. Read it before
adding a mutation (TanStack Query invalidation is manual), changing a response type (TS types
hand-mirror the Pydantic schemas), or rendering a rupee amount (masked by default).

## Never

- **Never commit `.env*`** (only `.env.example` is tracked), or **unredacted statement fixtures**.
  Real statements carry PAN, card last-4, account numbers, names and addresses —
  `scripts/redact_fixture.py` and the `fixture-redaction` pre-commit hook exist for this. The
  bundled sample statements (`backend/app/sample_statements/`) are synthetic and sit under the
  same guard.
- **Never bypass git hooks** (`--no-verify`), amend published commits, or force-push.
- **Never log PII** — see §Logging & PII.
- **Never start a long-running server** — see §Commands.
