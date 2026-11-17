# ADR-0005: `ty` is the only backend type-check gate, over `app/`

**Status**: Accepted
**Date**: 2026-07-29 (decided) · **Revised**: 2026-11-15
**Scope**: PRD §Production-grade essentials, §Maintenance posture · `backend/pyproject.toml`
(`[tool.ty.environment]`, `[tool.ruff.lint]`, `requires-python`), `.pre-commit-config.yaml`, `Makefile`
(`typecheck`), `.github/workflows/test-backend.yml`
**Supersedes / Related**: —

## Decision

Astral's `ty`, invoked as `uv run python -m ty check app`, is the only backend type-check gate; warnings
fail it. `mypy` is not a dependency. Ruff's `ANN` rules carry annotation completeness, which `ty` has no
setting for.

## Rules

1. **Every entry point runs exactly `uv run python -m ty check app`**: pre-commit, `Makefile` and CI.
   Never the bare `ty` shim. — *Enforced by*: tooling · *Locked by*: the pre-commit hook and
   `.github/workflows/test-backend.yml`
2. **The gated surface is `app/` only**, passed on the command line; there is no `[tool.ty.src]`. —
   *Enforced by*: tooling · *Locked by*: none (by choice — see Consequences)
3. **Warnings fail the gate** (`ty`'s default `error-on-warning`; no `--exit-zero`). — *Enforced by*:
   tooling · *Locked by*: the pre-commit hook
4. **The project runs Python 3.14 only**: `requires-python = ">=3.14,<3.15"` and
   `[tool.ty.environment] python-version = "3.14"`, which is the whole `ty` config. — *Enforced by*:
   tooling · *Locked by*: none (one config line)
5. **The only suppression in `app/` is `# ty: ignore[<rule>]`.** `# type: ignore`, bare or coded, is
   banned: ruff `PGH003` catches the bare form, and the `no-type-ignore-in-app` pre-commit hook (a
   pygrep over `backend/app/`) the coded form, which `PGH003` lets through. — *Enforced by*: tooling
   (ruff, pre-commit) · *Locked by*: `PGH003` in `[tool.ruff.lint] select` and the pygrep hook, both
   in pre-commit
6. **Annotation completeness is ruff `ANN`**, with `ANN401` ignored and `tests/**` exempt. —
   *Enforced by*: tooling (ruff) · *Locked by*: ruff in pre-commit and CI
7. **`ty` is pinned `>=<locked version>,<0.1`.** Every upgrade raises the floor to the newly locked
   version in the same commit, and upgrades are deliberate (`uv lock --upgrade`) behind the 3-day
   `exclude-newer` cooldown. — *Enforced by*: tooling (lockfile) · *Locked by*: none (review)
8. **Treat a diagnostic as a possible real bug before suppressing it.** — *Enforced by*: review ·
   *Locked by*: none (process rule)

## Out of bounds

- `tests/` is not type-checked. Widening the gate is its own decision (see Consequences).
- Editor checkers (pyright, Pylance) are conveniences; only `ty` gates.
- No mypy plugins, and no second checker kept "just in case": `uvx mypy app` serves on demand.

## Why

`ruff` and `uv` are already Astral tools, so `ty` puts lint, format, type-check and packaging on one
vendor and one release cadence. It is Rust and multi-threaded, which matters for a gate that runs on
every commit.

The usual reason to stay on mypy is a plugin (`sqlalchemy[mypy]`, `django-stubs`). This codebase uses
none: SQLAlchemy 2.0's `Mapped[...]` types through its own `py.typed`, and pydantic is supported
natively. A dry run before the swap found three real type errors that `mypy --strict` had passed.

The `-m` invocation dodges console shims that go missing from a Windows venv. Bare `# type: ignore` is
honoured by `ty` and silently hides a real error, while the coded form is ignored — two behaviours, so
one dialect is banned outright. A floor below the lock lets a fresh resolve pick an older 0.0.x with
different diagnostics. Python 3.14 only, because a 3.14-only API passes `ty` and then crashes at import
on 3.13.

## Alternatives rejected

- **Keep mypy, run `ty` as advisory** — two checkers and two suppression dialects for the benefit of
  neither.
- **Swap to `ty` but keep mypy installed** — an ungated checker rots and fails the first time it is needed.
- **Widen ruff (`ANN401`, `PLR`, `TCH`) to recover strictness** — a new gate, not parity; `ANN401`
  alone flags deliberate `Any` sites.
- **Override the cooldown for a newer `ty`** — the cooldown exists for exactly this kind of dependency.
- **Target `python-version` at the 3.13 floor** — the runtime, Docker image and CI are 3.14.

## Consequences

- **Strictness regresses** against `mypy --strict`: `warn_return_any`, `disallow_untyped_calls`,
  `disallow_untyped_decorators`, `disallow_any_generics`, `strict_equality` and `extra_checks` have no
  `ty` equivalent. `ANN` recovers annotation presence only. In return `ty` checks unannotated bodies
  and infers literals precisely.
- **Pre-1.0 churn**: a 0.0.x bump can add or drop diagnostics, so upgrades are their own commits.
- **No plugin escape hatch**: a dependency that needs a mypy plugin would force a re-decision.
- Rollback is cheap: nothing in `app/` depends on `ty` except its suppression comments.
- **Reopen** when `ty` reaches 0.1 or beta: reconsider gating `tests/`.

## History

- 2026-07-29 — Accepted: `ty` replaces `mypy --strict`; the dry run found three real bugs mypy missed.
- 2026-11-15 — Rewritten to the rules-first template. Python 3.14 only; bare and coded
  `# type: ignore` banned via ruff `PGH003`; the `ty` floor tracks the locked version; `tests/` stays
  ungated until `ty` reaches 0.1.
- 2026-11-15 — Rule 5's enforcement corrected on landing: `PGH003` flags only the bare form, so a
  pre-commit pygrep hook bans the coded form in `app/`.
