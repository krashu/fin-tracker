---
name: tdd
description: Test-driven development. Use when the user wants to build features or fix bugs test-first, mentions "red-green-refactor", or wants integration tests.
---

# Test-Driven Development

TDD is the red → green loop. This skill is the reference that makes that loop produce tests worth keeping:
what a good test is, where tests go, the anti-patterns, and the rules of the loop. Every section applies on
every cycle — consult them before and during the loop, not after.

Read `GLOSSARY.md` (if it exists) so test names match the domain language, and respect the ADRs in the
area you're touching. Turn the ask into a testable criterion first (AGENTS.md §Definition of done).

## What a good test is

Tests verify behaviour through public interfaces, not implementation details. A good test reads like a
specification — `test_refund_reduces_category_spend` says exactly what capability exists — and survives
refactors because it doesn't care about internal structure.

See [tests.md](tests.md) for examples and [mocking.md](mocking.md) for what to fake.

## Seams: where tests go

A **seam** is the public boundary you test at. In this repo the usual ones are:

- **HTTP API** — `tests/api/`, through the `client` (authenticated as `seeded_user`) or `unauth_client`
  fixtures. Where tenant isolation, status codes (404 vs 422) and router wiring get proven.
- **Service functions** — `tests/services/`, on the `session` fixture. Where business rules live, and where
  the 75% coverage floor effectively lands.
- **Parsers** — `tests/parsers/`, on redacted fixture PDFs.
- **Frontend helpers and components** — vitest, next to the file (`lib/format.test.ts`).

**Test only at pre-agreed seams.** Before writing any test, write down the seams under test and confirm them
with the user, each with a one-line note on what it catches and what it misses.

## Anti-patterns

- **Implementation-coupled**: mocks your own modules, tests private helpers, or verifies through a side
  channel (querying the table instead of calling the API/service). The tell: it breaks on a refactor that
  didn't change behaviour.
- **Tautological**: the expected value is recomputed the way the code computes it, so it passes by
  construction. Expected values come from an independent source: a literal, a worked example, the PRD.
- **Horizontal slicing**: all tests first, then all code. Work in **vertical slices** instead — one test →
  one implementation → repeat, each test a **tracer bullet** that responds to what the last cycle taught you.

## Rules of the loop

- **Red before green.** Write the failing test, run it, see it fail for the right reason, then write only
  enough code to pass. No speculative features (AGENTS.md §Simplicity first).
- **One slice at a time.** One seam, one test, one minimal implementation per cycle.
- **Run one file while looping** — `cd backend && uv run pytest tests/services/test_x.py --no-cov`, without
  `-n auto`. Run the full suite (`-n auto`, with coverage) before calling it done.
- **Refactoring is not part of the loop.** It belongs to review: suggest the user run `/senior-review` once
  the slices are green.
