---
name: handoff
description: Compact the current conversation into a handoff document for another agent to pick up.
argument-hint: "What will the next session be used for?"
disable-model-invocation: true
---

Write a handoff document summarising the current conversation so a fresh agent can continue the work.
Save it as `.scratch/handoff-<YYYY-MM-DD>-<topic>.md` at the repo root. `.scratch/` is git-ignored, so the
file never reaches a commit or the `package_source.py` zip.

Include a "suggested skills" section naming which skills the next agent should call.

Don't duplicate what other artifacts already hold (the PRD section, ADRs, commits, diffs, the plan file).
Reference them by path instead.

Redact secrets and statement PII — PAN, card last-4, account numbers, names, addresses — per AGENTS.md
§Never.

If the user passed arguments, treat them as what the next session will focus on and tailor the document
to that.
