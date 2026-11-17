# Copilot instructions — fin-tracker

**[`AGENTS.md`](../AGENTS.md) at the repo root, plus [`backend/AGENTS.md`](../backend/AGENTS.md) and
[`frontend/AGENTS.md`](../frontend/AGENTS.md) for their areas, are the full specification of this
project's conventions. Read them before writing code here.** This file is only the always-on kernel: the rules
that are expensive enough to break that they must apply even if nothing else is loaded.

Personal finance tracker. `backend/` = Python 3.14 / FastAPI / SQLAlchemy 2.0 / Alembic on SQLite,
managed by `uv`. `frontend/` = Next.js App Router / React 19 / TypeScript / Tailwind 4 /
TanStack Query v5, managed by `pnpm`.

Also read the relevant [`docs/adr/`](../docs/adr/) entry before changing an area it covers, and the
named section of `PRD.md` — **never** read `PRD.md` wholesale, it is long.

## Non-negotiable

1. **Money is int64 `paise` / `cents`, never `float`.** Field names end `_paise` or `_native_paise`;
   DB columns are `BigInteger` (plain `Integer` overflows at ₹21,474,836).
2. **Every DB read filters `user_id`.** Single-row fetch is `WHERE id = :id AND user_id = :uid` and a
   miss returns **404**, never 403 (a 403 is an existence oracle). A request-body FK pointing at
   another user's row is **422**, never silently honoured. Getting this wrong is a data leak.
3. **Time comes from `app/core/clock.py`** — `naive_utcnow()` for anything stored. No
   `datetime.now()`, no `date.today()`, no `func.now()` as a column's sole writer. Stored timestamps
   are naive UTC; aware values shift under Postgres.
4. **The backend type-checker is `ty`, not mypy.** Run `uv run python -m ty check app`. Suppress with
   `# ty: ignore[rule-name]` — **`# type: ignore` is not honoured.**
5. **Never log PII.** No PAN, account numbers, or card last-4 in logs, tracebacks, or error events.
   Use `get_logger(__name__)` from `app.core.log_config` and emit structured events
   (`logger.info("event_name", key=value)`) — never f-strings, never `import structlog` directly.
6. **Never commit `.env*`** (only `.env.example` is tracked) **or unredacted statement fixtures** —
   real statements carry PAN, card last-4, account numbers, names and addresses.
7. **Never bypass git hooks** (`--no-verify`), amend published commits, or force-push.
8. **Never run `uv add` / `remove` / `sync` / `lock` or `pnpm add` / `install` / `remove` unprompted** —
   propose the change first.
9. **Never start a long-running server** (`pnpm dev`, `uvicorn --reload`, `docker compose up`). The
   human runs those. Verify backend behaviour with pytest, not against a live instance.
10. **Never run `alembic upgrade` / `downgrade`** against anything but the test SQLite DB without
    explicit confirmation.

## Two silent-failure traps

- **A new endpoint needs its module *and* its `include_router` line in `app/api/v1/router.py`.**
  Forget the second and it 404s with no error anywhere. There is no `app/routers/` — don't add one.
- **TanStack Query invalidation is manual.** Nothing refetches on its own
  (`staleTime: 30_000`, `refetchOnWindowFocus: false`). A mutation that doesn't invalidate serves
  stale data. Read the docstring in `frontend/lib/queries/invalidate.ts` before adding one.

Also: TS types in `frontend/lib/api/client.ts` are **hand-mirrored** from Pydantic schemas, and
`tsc` cannot see a backend field the TS type never declared. A response schema gaining a field must
be diffed by hand.

## Commands

`make` may not be installed; these are authoritative.

```
cd backend  && uv run pytest -n auto              # tests (add --no-cov for a fast loop; drop -n auto for one file)
cd backend  && uv run ruff check .                # lint
cd backend  && uv run python -m ty check app      # typecheck
cd frontend && pnpm lint                          # lint (declares zero rules — passes vacuously)
cd frontend && pnpm typecheck                     # tsc --noEmit — the only check on the API-type mirror
cd frontend && pnpm test                          # vitest
```

`pre-commit` is the local gate, including both type-checkers; it runs neither test suite. Coverage floor
is 75%, landing effectively on `services/` and `parsers/`. The `.github/workflows` re-run everything
only after the source is published.

## How to work here

State which PRD section you're touching and which files you'll edit before non-trivial changes. If a
sign convention, column type, or ownership question is ambiguous — **ask, don't guess.** Bug fixes go
reproduce → minimal repro → root cause → fix → regression test, and stay surgical: no drive-by
refactors, no reformatting untouched lines. No abstraction before the second concrete use, no
speculative error handling, no config knobs for hypothetical flexibility.
