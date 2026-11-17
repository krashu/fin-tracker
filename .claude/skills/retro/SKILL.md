---
name: retro
description: Conduct a retrospective on a coding session.
disable-model-invocation: true
---

The user has asked for a **retrospective**: suggest improvements to the coding agent's **environment** so
future runs go better.

## Steps

1. Call the Skill tool with `writing-for-agents` for the writing style guide.

2. Read the primary sources for the session the user names. That may mean the session logs under
   `~/.claude/projects/`. With no session named, use the current one.

3. Look for candidates in these categories:

- **Navigation**: how easily did the agent find the right files? Would a **navigation pointer** in an
  `AGENTS.md` help? _Use when_ finding information took a long time.
- **Automated checks**: could a check have caught a mistake the agent made? This repo's guardrails are
  pre-commit (`.pre-commit-config.yaml`), the Claude Code Bash guard (`.claude/hooks/guard_bash.py`), and
  the GitHub workflows that run only after the source is published — too late to stop anything locally.
  So a new check goes into pre-commit **and** the matching workflow. Read those first: a check that exists
  but is unwired or broken is the finding, not a reinvention. _Use when_ the agent made a mistake a check
  could have caught.
- **Coding standards**: classify the violation. A **mechanical** one (banned API, import shape, file
  location) gets a deterministic check — a ruff rule, a pre-commit hook, a test like
  `test_datetime_boundary.py` — not prose. Reserve `AGENTS.md`, the ADRs and `/senior-review`'s checklist
  for genuine **judgement calls**. _Use when_ review failed to catch a mistake.
- **AGENTS.md size**: root `AGENTS.md` loads into every session, so it holds repo-wide rules and pointers;
  area detail belongs in `backend/AGENTS.md` or `frontend/AGENTS.md`. Could a steering line become a check
  instead? _Use when_ the files are large.
- **Tool economy**: did the agent make expensive calls that could be streamlined (reading `PRD.md`
  wholesale, running the full suite with `-n auto` on one file)? _Use when_ a call was expensive.
- **No-ops**: instructions in steering files that don't change behaviour. _Use when_ the files are large.
- **Information access**: was something crucial unavailable — a log, a redacted fixture, a captured
  response? _Use when_ the agent lacked information.

4. Present the candidates to the user, most severe first. Change nothing until they pick.

## Reference

### Implementation vs review

Work goes through two stages. The implementation agent carries the most **context pressure**: exploring,
writing code, debugging. The review agent (`/senior-review`) receives a diff and needs no exploration, so
standards are cheapest to enforce there, not in the implementer's always-loaded context.

### Files

- `AGENTS.md` (root and per area), loaded through the `CLAUDE.md` shims: use them sparingly, mostly for
  **navigation pointers** and rules whose breach is a leak or corruption.
- `docs/adr/`: decisions and their reasons; pointed at, not restated.
- Skills (`.claude/skills/`): procedures and reference. Follow `writing-for-agents`.
