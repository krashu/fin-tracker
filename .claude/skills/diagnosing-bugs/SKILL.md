---
name: diagnosing-bugs
description: Diagnosis loop for hard bugs and performance regressions. Use when the user says "diagnose"/"debug this", or reports something broken/throwing/failing/slow.
---

# Diagnosing Bugs

A discipline for hard bugs, and the procedure behind AGENTS.md §Bug fixes (reproduce → minimal repro →
root cause → fix → regression test). Skip phases only when explicitly justified.

When exploring, read `GLOSSARY.md` (if it exists) for the domain vocabulary, and the ADR for the area
you're touching (the table is in AGENTS.md). Code under `backend/` or `frontend/` has its own
`AGENTS.md` — read it before theorising about that side.

## Redact

This skill has you show commands, outputs and captured artifacts. Statement data carries PAN, card
last-4, account numbers, names and addresses, so **redact every one of them, and every secret**: write
`<REDACTED>` in its place. Build loops on the redacted fixtures in `backend/tests/fixtures/`; never on the
real PDFs under `fixtures/_local/`. Keep credentials in the environment, not in what you show.

If the redacted output is not enough to diagnose the bug, say so and ask the user.

## Phase 1: Build a feedback loop

**This is the skill.** If you have a **tight** pass/fail signal that goes **red** on _this_ bug, you will
find the cause; bisection, hypotheses and instrumentation only consume it. Without one, no amount of
reading code will save you. Spend disproportionate effort here. Be relentless.

### Ways to construct one, in roughly this order

1. **Failing pytest** at the seam that reaches the bug: an API test through the `client` /
   `unauth_client` fixtures (`tests/api/conftest.py`), a service test on the `session` fixture
   (`tests/services/conftest.py`), or a parser test on a redacted fixture PDF.
2. **Failing vitest** (`cd frontend && pnpm test <file>`) for a `lib/` helper or component.
3. **Replay a captured payload.** Save a redacted request body / CSV / statement extract to a fixture and
   drive it through the code path in a test.
4. **External API through `httpx.MockTransport`** — the pattern in `tests/services/test_fx_service.py` —
   when the bug sits behind a price/FX feed.
5. **Throwaway harness**: a script that calls one service function with a fresh in-memory session.
6. **Property / fuzz loop**: for "sometimes wrong output", run many generated inputs and look for the failure.
7. **Bisection harness**: if it appeared between two known states, automate the check and `git bisect run` it.
8. **Differential loop**: same input through old vs new code (or two configs), diff the outputs.

The app is not a feedback loop here: you don't start dev servers (AGENTS.md §Commands). If only the
running UI shows the bug, ask the user to reproduce it and give you the exact request, response and
console output, then turn that into a test.

### Tighten the loop

- Faster: run one file or test, **without** `-n auto` (worker startup costs more than it saves on one
  file), and with `--no-cov`.
- Sharper: assert on the user's exact symptom, not "didn't crash".
- Deterministic: build time-relative data from `clock.naive_utcnow()` (see `tdd/mocking.md`); never real
  wall-clock time or network.

### Non-deterministic bugs

Aim for a **higher reproduction rate**, not a clean repro: loop the trigger, add stress, narrow the timing
window. A 50%-flake bug is debuggable; 1% is not, so keep raising the rate.

### When you genuinely cannot build a loop

Stop and say so. List what you tried. Ask the user for a redacted captured artifact (request/response,
log lines, a redacted statement) or for them to reproduce while you watch the output. Do **not** proceed
to hypothesise without a loop.

### Completion criterion: a tight loop that goes red

Phase 1 is done when you can name **one command** you have **already run** (show the invocation and its
redacted output) that is:

- [ ] **Red-capable**: drives the real bug code path and asserts the user's exact symptom.
- [ ] **Deterministic**: same verdict every run (flaky bugs: a pinned, high reproduction rate).
- [ ] **Fast**: seconds, not minutes.
- [ ] **Agent-runnable**: you can run it unattended.

If you catch yourself reading code to build a theory before this command exists, **stop**: jumping
straight to a hypothesis is the exact failure this skill prevents.

## Phase 2: Reproduce + minimise

Run the loop and watch it go red. Confirm:

- [ ] It shows the failure the **user** described, not a nearby one. Wrong bug = wrong fix.
- [ ] It reproduces across runs (or at a high enough rate).
- [ ] You captured the exact symptom (error, wrong paise amount, timing) for later verification.

Then shrink to the **smallest scenario that still goes red**: cut rows, fields, fixtures, calls **one at
a time**, re-running after each cut. Done when every remaining element is load-bearing. The minimal
repro becomes the regression test in Phase 5.

## Phase 3: Hypothesise

Generate **3–5 ranked, falsifiable hypotheses** before testing any:

> "If <X> is the cause, then <changing Y> will make the bug disappear / <changing Z> will make it worse."

Usual suspects in this codebase, worth checking against each hypothesis: a sign flipped on a refund
(ADR-0009), float creeping into paise arithmetic, a naive/aware datetime mix (`clock`), a missing
`user_id` predicate (ADR-0003), a fingerprint/occurrence mismatch (ADR-0006), or an endpoint never wired
into `app/api/v1/router.py`.

**Show the ranked list to the user before testing.** They often re-rank instantly. Don't block on it if
they're away.

## Phase 4: Instrument

Each probe maps to one prediction from Phase 3. **Change one variable at a time.**

1. **Debugger**: `uv run pytest <test> -n0 --pdb` (xdist doesn't support `--pdb`).
2. **Targeted logs** at the boundaries that separate hypotheses, through `get_logger(__name__)` with
   structured fields — never PII, never f-strings.

**Tag every debug probe** with a unique prefix, e.g. `[DEBUG-a4f2]`, so cleanup is one grep.

**Perf branch**: measure first. `--durations=15` is already in `addopts`; add a timing harness or check
the SQL (`echo=True` on the test engine) before changing anything.

## Phase 5: Fix + regression test

Write the regression test **before the fix**, at a **correct seam**: one that exercises the real bug
pattern as it happens at the call site. If the only seam available is too shallow to replicate it, that
is itself the finding — note it.

1. Turn the minimal repro into a failing test at that seam.
2. Watch it fail.
3. Apply the smallest fix that addresses the root cause — no refactors on the way (AGENTS.md §Surgical changes).
4. Watch it pass.
5. Re-run the Phase 1 loop against the original, un-minimised scenario.

## Phase 6: Cleanup

Required before declaring done:

- [ ] The original repro no longer reproduces.
- [ ] The regression test passes (or the missing seam is documented).
- [ ] All `[DEBUG-...]` probes are gone (`grep` the prefix).
- [ ] Throwaway harnesses are deleted.
- [ ] The full suite passes if you touched a migration (AGENTS.md §Commands); otherwise the fast loop.
- [ ] The hypothesis that turned out correct is in the commit message, so the next debugger learns.
