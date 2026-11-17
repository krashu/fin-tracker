---
name: senior-review
description: Senior-engineer review of a diff — code findings, then a test audit — against this repo's AGENTS.md and ADRs.
argument-hint: "[git ref | path] [PRD §section]"
disable-model-invocation: true
---

Review a change the way a senior engineer who has to sign off on it would. The bar is correctness and
clarity, not maximal architecture. This is review only: report, don't edit, unless the user asks you to
apply the fixes afterwards.

## 1. Pin the diff

- No argument → the working tree: `git diff HEAD` plus untracked files from `git status --porcelain`.
- A ref → `git diff <ref>...HEAD` and `git log --oneline <ref>..HEAD`. Check it with `git rev-parse` first.
- A path → limit either of the above to it.

An empty diff or a bad ref stops here with a one-line message.

## 2. Orient before judging

Read, and cite what you read:

- `backend/AGENTS.md` and/or `frontend/AGENTS.md` for whichever side the diff touches (root `AGENTS.md`
  is already in context).
- The ADR for every area the diff touches — the table in root `AGENTS.md` maps subjects to ADRs.
- The code around each hunk, enough to judge it against existing patterns rather than your own.

If the user named a PRD section, start the **spec check** now, in parallel: dispatch a subagent with a
fresh context, giving it the diff command, the commit list and the section name (it reads only that
section of `PRD.md`, never the whole file). Its brief: "Report (a) requirements the section asks for that
are missing or partial, (b) behaviour in the diff it didn't ask for, (c) requirements that look
implemented but wrong. Quote the PRD line for each. Under 400 words."

## 3. Pass 1 — code findings

Go through every hunk against the checklist below, then hunt in this order: correctness bugs, security and
data integrity, over-engineering (hunted as hard as bugs — a second concrete use or it isn't an
abstraction), then smells.

Tag each finding with a severity and a kind:

- **Severity**: `blocking` (would not merge), `would-slow` (merges, but costs us later), `worth-discussing`.
- **Kind**: Bug · Security · Smell · Tech-debt · Over-engineering · Taste. Label taste as taste; never
  dress it as a bug. Ask "would I actually block on this?"

Each finding: `file:line` — what — why it matters — the smallest fix. Evidence, not vibes. No drive-by
rewrites, no invented requirements.

## 4. Pass 2 — test audit

For each new or changed test, a verdict with a one-line reason:

- **keep** — exercises real behaviour at a sensible seam.
- **theater** — asserts nothing that could fail (status 200 only, mocks asserting on mocks).
- **redundant** — same contract as an existing test.
- **wrong-assertion** — tests the wrong thing, or is tautological (expected value recomputed like the code).

Then list **missing tests by contract**, each as one sentence a test could be named after ("another user's
account id on PATCH → 422").

## 5. Report

Skip the diff summary.

```markdown
## Pass 1 — Code findings
### Blocking
- **[Bug]** `backend/app/api/v1/x.py:42` — what. Why it matters. Smallest fix.
### Would slow us down
### Worth discussing

## Pass 2 — Test audit
| Test | Verdict | Why |
|---|---|---|

Missing, by contract:
- …

## Spec — PRD §… (only if requested; the subagent's report, lightly cleaned)

## Leave alone — already good
- …

## Do now / Defer
Do now (most important first, at most five): …
Defer: …
```

Keep the spec findings separate from Pass 1, without merging or re-ranking them: a change can follow every
standard and still build the wrong thing.

## This repo's checklist

Each item names where the rule lives; cite that file when you flag a breach.

**Tenant isolation** (root AGENTS.md, ADR-0003)
- Every read filters `user_id`, even when the ids came from the user.
- Single-row fetch is `id` AND `user_id`; a miss is 404 (not 403, not 200).
- A body FK to another user's row (`account_id`, `category_id`, `instrument_id`, transfer legs) is 422.
- A new owned table has a row in `tests/api/test_auth_isolation.py`.

**Money** (root AGENTS.md §Money, ADR-0009)
- Integer paise, `BigInteger`, names ending `_paise` / `_native_paise`; no `float` anywhere in the path.
- Refund sign: a `spend` row with the opposite sign, same category.
- `fx_rate_to_inr` is stamped at the transaction date, never re-derived at read time.

**Backend** (`backend/AGENTS.md`, ADR-0001, ADR-0005)
- A new endpoint has its `include_router` line in `app/api/v1/router.py`; nothing under `app/routers/`.
- Time comes from `clock`; stored values use `naive_utcnow()`; `DateTime` columns carry
  `default=utcnow_default` plus the `server_default`.
- Models and migrations follow ADR-0001's five rules, and every `downgrade()` reverses its `upgrade()`.
  A migration change means the full suite ran (`test_migrations_stairway`).
- Suppressions are `# ty: ignore[rule]` (never `# type: ignore`) and the diagnostic was checked as a
  possible real bug first.
- Logging uses `get_logger(__name__)` with structured events: no f-strings, no PAN, account number or
  card last-4.

**Frontend** (`frontend/AGENTS.md`)
- Every mutation invalidates per the `lib/queries/invalidate.ts` docstring; rule mutations call
  `invalidateRules`.
- A response schema that gained a field has it in the TS mirror in `lib/api/client.ts` (diffed by hand —
  tsc cannot see it).
- Rupee amounts go through `Sensitive` or the chart path (`useBalanceHidden()` +
  `pointer-events-none select-none blur-sm`).
- Calls go through `request()`; no 401 handling at the call site.
- Chart money formatting goes through `lib/format.ts`; `"use client"` only where interactivity needs it.

**Repo hygiene** (root AGENTS.md §Never, §Surgical changes, §Dependencies)
- No `.env*` other than `.env.example`, no unredacted statement data in fixtures.
- Lockfile changes were asked for.
- Corrected prose was fixed in all of its copies; a changed command also changed its workflow `run:` step.
- The change is surgical: no reformatting of untouched lines, no renames of things already correct.

## Smell baseline

Always judgement calls, and a documented repo standard overrides them. Skip anything a linter enforces.
Mysterious Name · Duplicated Code · Feature Envy · Data Clumps · Primitive Obsession (beyond the paise
`int`, which is deliberate) · Repeated Switches · Shotgun Surgery · Divergent Change · Speculative
Generality · Message Chains · Middle Man · Refused Bequest.
