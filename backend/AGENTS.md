# AGENTS.md — fin-tracker backend

Conventions for `backend/`. Universal invariants and root boundaries apply.

## Local Commands

| Task | Command | Notes |
|---|---|---|
| Single test / debug | `uv run pytest tests/<path> -k <test_name>` | Omit `-n auto`; use `-n0` for `--pdb` |
| Fast test loop | `uv run pytest --no-cov -m "not real_pdf and not slow" -n auto` | Fast dev feedback; skips slow migrations |
| Full suite | `uv run pytest -n auto` | Runs full suite with 75% coverage check |
| Lint & format | `uv run ruff check --fix . && uv run ruff format .` | Ruff lint & format |
| Type check | `uv run python -m ty check app` | Gates `app/` only; warnings fail gate |
| Migrations | `uv run alembic upgrade head` | Test SQLite DB only without human approval |

## Architecture & Routing

- `app/api/v1/<domain>.py` — flat domain modules exporting `router`.
- Every module must be registered with an `include_router` line in `app/api/v1/router.py`. Omitting this **404s silently** with no error.
- There is no `app/routers/` directory. Shared dependencies live in `app/api/deps.py`. Pydantic schemas live in `app/schemas/`.

## Models & Migrations (ADR-0001)

- **Column types:** Stored money uses `BigInteger` (`_paise` / `_native_paise`). Units, prices, NAV, and FX rates use `ScaledDecimal` (never `Numeric` or `Float`).
- **Enums:** String enums use `Enum(..., native_enum=False, create_constraint=True)` for portable VARCHAR + CHECK.
- **Constraints:** Constraint names derive from `MetaData(naming_convention=...)` on declarative `Base`. Hand-named CHECKs take only the suffix: `name="no_self_pair"`.
- **Dialect Parity:** SQLite runs with `PRAGMA foreign_keys=ON` via connect listener in `core/db.py`; migrations run with it off (`alembic/env.py`). Partial indexes must define both `sqlite_where=` and `postgresql_where=`. Upserts/increments use `core/db_errors.py` helpers.
- **Base model:** Models inherit `TimestampMixin` with Python-side `default=utcnow_default` and `server_default=func.now()`. User PK is a UUID.

## Timestamps & Clock (ADR-0001)

- Sole time source is `app/core/clock.py`: import as `from app.core import clock`.
  - `clock.naive_utcnow()`: only value written to `DateTime` columns (aware values shift under Postgres).
  - `clock.utcnow()`: aware instant when required by external interfaces.
  - `clock.today()`: current IST business date.
- Never call `datetime.now()` or `date.today()`. Never compare stored datetimes finer than day granularity.

## Logging & PII

- Import via `from app.core.log_config import get_logger`, then `logger = get_logger(__name__)`. Never `import structlog` directly.
- Emit structured events: `logger.info("event_name", key=value)`. Never f-strings.
- Sensitive identifiers (PAN, card numbers, accounts, Aadhaar) are masked automatically at the logger level.

## Type Safety (ADR-0005)

- Type checker is Astral's `ty`: invoke via `uv run python -m ty check app`.
- Suppress only with `# ty: ignore[rule-name]`.
- `# type: ignore` is strictly banned in `app/` (enforced by ruff `PGH003` and pre-commit hook).
