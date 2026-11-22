# AGENTS.md — fin-tracker

Tracked conventions for coding agents. Precedence:
`Explicit user prompt > nearest AGENTS.md (backend/ or frontend/) > root AGENTS.md`
Universal safety and secrets rules in **Never** cannot be overridden.

Personal finance tracker: Indian cards, bank accounts, cash, investments (MFs, US stocks).
- `backend/` — Python 3.14 / FastAPI / SQLAlchemy 2.0 / Alembic on SQLite (`uv`).
- `frontend/` — Next.js 16 App Router / React 19 / TypeScript / Tailwind 4 / TanStack Query v5 (`pnpm`).

## Orientation & Specs

| Resource | Scope |
|---|---|
| [PRD.md](PRD.md) | Features F1–F11, data model, architecture. Read target section only; never wholesale. |
| [GLOSSARY.md](GLOSSARY.md) | Canonical domain definitions (money units, transaction identity, accounts, tenancy). |
| [docs/adr/](docs/adr/README.md) | Architecture Decision Records (0001–0021+). Read before altering covered areas. |
| [.claude/skills/](.claude/skills/README.md) | Situational workflows (TDD, bug diagnosis, reviews, domain modeling, FastAPI, Playwright). |
| [frontend/certs/README.md](frontend/certs/README.md) | Corporate TLS proxy setup if external feeds fail with `CERTIFICATE_VERIFY_FAILED`. |

## Commands

Run from repo root. Fast loop drops coverage, real-PDF, and slow tests; migrations require the full suite.

| Task | Command | Scope / Notes |
|---|---|---|
| Full suite | `cd backend && uv run pytest -n auto` | Enforces 75% coverage floor (`services/`, `parsers/`) |
| Fast loop | `cd backend && uv run pytest --no-cov -m "not real_pdf and not slow" -n auto` | Inner dev loop; skips slow migrations |
| Single test | `cd backend && uv run pytest tests/<path> -k <test_name>` | Run without `-n auto` (avoids 17s xdist worker boot); use `-n0` for `--pdb` |
| Backend lint | `cd backend && uv run ruff check .` | Append `--fix` to autofix |
| Backend typecheck | `cd backend && uv run python -m ty check app` | `python -m ty` required for Windows venv; gates `app/` only |
| Frontend lint | `cd frontend && pnpm lint` | ESLint |
| Frontend typecheck | `cd frontend && pnpm typecheck` | Runs `tsc --noEmit` |
| Frontend tests | `cd frontend && pnpm test` | Runs `vitest run` |
| Frontend single test | `cd frontend && pnpm vitest run <path>` | Targeted component/helper spec |
| Frontend build | `cd frontend && pnpm build` | Next.js production build |

## Universal Invariants

- **Money & Signs:** Amounts are integer paise/cents (int64), never `float`. Field names end `_paise` (INR) or `_native_paise` (investments). Refund is a positive `spend`; income reversal is a negative `income` in the reversed row's category (ADR-0009). Purchase cashback is positive `spend` (ADR-0013). Zero amount is rejected on all write paths. Investment FX rate to INR is server-stamped at transaction date and never rewritten (ADR-0016).
- **Tenant Isolation (ADR-0003):** Every owned table query must filter `user_id`. Single-row fetch is `WHERE id = :id AND user_id = :uid`; a miss returns **404** (never 403, which leaks existence). A foreign user's entity referenced in a request body returns **422**. Global tables without `user_id`: `benchmarks`, `benchmark_nav`, `fx_rates`.
- **Dates & Time (ADR-0001):** Stored instants are portable naive UTC. Business date is IST (`Asia/Kolkata`). Never store or filter by system-local time.

## Boundaries

### Always
- Map non-trivial changes to a verification test; maintain >=75% coverage on backend `services/` and `parsers/`.
- Isolate bug fixes: reproduce with minimal test -> fix -> verify regression test. No drive-by refactoring.
- Check callers before removing code or modifying cross-boundary interfaces.

### Ask First
- Adding, updating, or removing dependencies (`uv add/remove`, `pnpm add/remove`).
- Running database migrations (`alembic upgrade/downgrade`) on non-test databases.
- Modifying database schemas, constraint structures, or table tenancy boundaries.

### Never
- Never commit secrets, `.env*` files, or unredacted statement fixtures (PAN, account/card numbers, Aadhaar).
- Never log PII in logs, error payloads, or traces.
- Never bypass git hooks (`--no-verify`), amend published commits, or force-push (`push -f`).
- Never start background/long-running servers (`pnpm dev`, `uvicorn`, `docker compose up`) — verify via pytest/vitest.
