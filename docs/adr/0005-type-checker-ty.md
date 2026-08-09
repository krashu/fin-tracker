# ADR-0005: `ty` replaces `mypy` as the backend type-check gate

**Status**: Accepted
**Date**: 2026-07-29

## Problem

The backend has been gated by `mypy --strict` on `app/` since v0.1, wired identically into three
places (`Makefile` `typecheck`, the `pre-commit` local hook, and `.github/workflows/ci.yml`). That
choice was never recorded in an ADR — it was inherited from PRD §Production-grade essentials, which
names the tool directly: *"`ruff` … + `mypy --strict` on `app/`. CI fails on either."*

Two things make the choice worth revisiting rather than re-litigating ad hoc:

1. **Vendor consolidation.** `ruff` and `uv` are already Astral tools. `ty` is Astral's type
   checker, so adopting it collapses the backend toolchain to one vendor and one release cadence.
2. **Speed.** `ty` is Rust and multi-threaded; it reports roughly 9× single-threaded mypy on typical
   code. On a 97-file / 15.3k-line `app/` the absolute saving is small, but the gate runs on every
   commit via pre-commit, where latency is felt.

The counterweight is maturity: `ty` is pre-1.0 (0.0.63 at time of writing, 1.0 targeted for 2026),
has **no plugin system**, and has **no `--strict` bundle**. The received wisdom is "stay on mypy if
you depend on `sqlalchemy[mypy]` or `django-stubs`". That advice does not bind here, and the reason
is worth recording so it isn't re-derived: this codebase configures **no mypy plugins at all**. The
entire `[tool.mypy]` block was `python_version`, `strict = true`, and one `ignore_missing_imports`
override for `pyxirr`. SQLAlchemy 2.0's `Mapped[...]` typing works through PEP 681 + the descriptor
protocol in SQLAlchemy's own `py.typed` distribution, not through the (deprecated) mypy plugin, and
pydantic has had native `ty` support since 0.0.57.

A dry run confirmed the theory before any config changed: **`ty check app` reported 7 diagnostics
across 97 files.** All 117 `Mapped[...]` annotations passed, as did `TypeDecorator[Decimal]`
(`models/types.py`), `Mapped[Literal[...]]` columns, the `StatementParser` Protocol
(`parsers/base.py`), and the PEP 695 bounded generic `def confirmed_only[S: Select[Any]](...)`
(`services/transaction_queries.py`). `pyxirr` resolved with no override at all.

Of the 7, **three were real type errors that `mypy --strict` passed**:

- `services/benchmark_service.py` / `services/fx_service.py` — the `new_rows` list literals infer as
  `list[dict[str, int | date | Decimal]]`, which is not assignable to the `list[dict[str, object]]`
  their `_insert_*_skip_existing` helpers accept (dict is invariant in its value type).
- `services/investment_import_service.py` — `existing_fps` was annotated `set[str]`, but
  `InvestmentTransaction.fingerprint` is nullable; the `IS NOT NULL` predicate narrows the SQL, not
  the Python type.

A fourth was an annotation-scoping bug (`date: Mapped[date]` in `models/fx_rate_quote.py` shadows
its own `from datetime import date` import). All four are fixed in `b2570e8`, ahead of and
independent of this swap.

## Decision

**`ty` is the sole backend type-check gate; `mypy` is removed as a dependency, and ruff's `ANN`
rules carry the annotation-completeness half of `strict` that `ty` cannot express.**

**Configuration** (`backend/pyproject.toml`):

- `[tool.ty.environment] python-version = "3.13"` — that is the whole `ty` config.
- No strictness block. `ty`'s defaults are stricter than mypy's here: it type-checks unannotated
  function bodies and infers list/dict literals precisely, and neither is configurable. Warnings
  fail the gate because `terminal.error-on-warning` defaults to `true`.
- No `[tool.ty.src] include` — the three entry points pass `app` on the command line exactly as
  they passed it to mypy, so the gated surface is unchanged and lives in one place per caller.
- `select = [..., "ANN"]` with `ignore = ["ANN401"]` and `per-file-ignores` for `"tests/**"`.

**Invocation** — always `uv run python -m ty check app`, never the bare `ty` shim. The `-m` form is
what commit `5c4c479` standardised on after the mypy console shim went missing from the local
Windows venv; `python -m ty` is a supported entry point and dodges the same failure.

**Suppressions** — `ty` does **not** honour mypy-style `# type: ignore[code]` comments (verified: it
errored straight through the two in `api/v1/auth.py`). Use `# ty: ignore[rule-name]`. There are
exactly two in the codebase, both for `samesite=settings.cookie_samesite`, which
`Settings._validate_cookie_policy` already guarantees at startup.

**Version policy** — `"ty>=0.0.63,<0.1"`. The floor is 0.0.63 rather than the newer 0.0.64 because
the project's 3-day `exclude-newer` cooldown (§Maintenance posture) filters anything younger, and
that guard is the point — it should not be overridden with `exclude-newer-package` for a type
checker. Both versions were verified clean on `app/`.

**Do not:**

- Widen the gate to `tests/` as part of a `ty` upgrade. `tests/` has never been type-checked; it
  carries ~20 inert mypy-style ignores and 21 `ANN` violations. Widening it is its own decision.
- Reach for `# ty: ignore` before checking whether the diagnostic is a real bug. Four of the seven
  in the dry run were.
- Treat the `pyright-lsp` editor plugin (`.claude/settings.local.json`) as authoritative. It is an
  editor convenience and may disagree with `ty`; only `ty` gates.

---

## Trade-offs

**Benefits:** One vendor for lint + format + type-check + packaging. Materially faster gate. Caught
three latent type errors mypy passed. Checks unannotated code by default. `pyxirr` no longer needs a
missing-imports override, so the config shrinks from 4 settings to 1.

**Drawbacks:**

- **Strictness genuinely regresses.** These `mypy --strict` components have no `ty` equivalent and
  are simply gone: `warn_return_any`, `disallow_untyped_calls`, `disallow_untyped_decorators`,
  `disallow_any_generics`, `strict_equality`, `extra_checks`. `ANN` recovers annotation *presence*
  (`disallow_untyped_defs` / `disallow_incomplete_defs`) but nothing about what the annotations mean.
  `warn_unused_ignores` is partly recovered by `ty`'s own `unused-ignore-comment` rule.
- **Pre-1.0 churn.** Behaviour can change between 0.0.x releases, so a version bump can surface new
  diagnostics or drop existing ones. The tool is beta and self-describes as having no stable API.
- **No plugin escape hatch.** If a future dependency needs a mypy plugin to type correctly, there is
  no `ty` answer; that dependency would force a re-decision.

**Mitigation:** `uv.lock` pins exact and the 3-day cooldown applies, so upgrades are deliberate
(`uv lock --upgrade`) rather than ambient. The strictness delta is written down here rather than left
as a surprise. Rollback is cheap and total: revert the tooling commit and `uv lock` — nothing in
`app/` depends on `ty`, the only checker-specific artefacts in the source tree are the two
`# ty: ignore` comments, and the `b2570e8` fixes are clean under both checkers.

## Alternatives Considered

**Alternative 1 - Keep `mypy`, add `ty` as an advisory non-blocking check**: the safest option and
the one the migration guides recommend, but it means maintaining two checkers and two suppression
dialects indefinitely while getting the benefit of neither. Rejected as the stated goal was to *use*
`ty`, not evaluate it.

**Alternative 2 - Swap the gate to `ty` but keep `mypy` installed as a manual escape hatch**:
cheap insurance, and genuinely tempting given the beta status. Rejected because an ungated second
checker rots — nothing keeps `app/` mypy-clean once CI stops asking, so the escape hatch would be
broken the first time it was needed. `uvx mypy app` serves the same purpose on demand without a
dependency.

**Alternative 3 - Recover the lost strictness with `ANN` plus a wider ruff selection
(`ANN401`, `PLR`, `TCH`)**: `ANN401` bans explicit `Any`, which is *stricter* than the old gate
(`disallow_any_explicit` was never enabled) and would flag five deliberate sites. Rejected as scope
creep — the goal is parity with the previous gate, not a new one.

**Alternative 4 - Override the dependency cooldown to take `ty` 0.0.64**: rejected on principle. The
cooldown exists to keep freshly-published releases out of the lockfile, and a beta type checker is
precisely the kind of dependency it is protecting against. 0.0.63 is clean and two releases behind
nothing that matters.
