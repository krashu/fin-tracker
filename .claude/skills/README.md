# Project skills

Claude Code loads each `<name>/SKILL.md` here as a skill. Model-invoked skills can fire on their own;
user-invoked ones (`disable-model-invocation: true`) run only when typed as `/<name>`.

| Skill | Invocation | Source | Local changes |
|---|---|---|---|
| `diagnosing-bugs` | model | mattpocock/skills `engineering/diagnosing-bugs` | Feedback loops are pytest/vitest (no dev servers); statement-PII redaction; repo's usual bug suspects; HITL script dropped |
| `tdd` (+ `tests.md`, `mocking.md`) | model | `engineering/tdd` | Seams are this repo's API/service/parser/vitest layers; examples rewritten as pytest on the real fixtures; time via `clock`, external HTTP via `httpx.MockTransport` |
| `senior-review` | user | `engineering/code-review` + the prompts in `docs/PROMPTS.md` | Two passes (code findings, test audit) instead of two axes; this repo's checklist; optional PRD-section spec check |
| `grilling` | model | `productivity/grilling` | none |
| `domain-modeling` (+ `GLOSSARY-FORMAT.md`) | model | `engineering/domain-modeling` | ADRs use `docs/adr/template.md` and the AGENTS.md table; seeded with this repo's vocabulary; upstream `ADR-FORMAT.md` dropped |
| `grill-with-docs` | user | `engineering/grill-with-docs` | none |
| `handoff` | user | `productivity/handoff` | Writes to the git-ignored `.scratch/` |
| `retro` | user | `engineering/retro` | Names this repo's guardrails and the root/area AGENTS.md split |
| `writing-for-agents` (+ `SKILL-MECHANICS.md`) | model | `productivity/writing-for-agents` | none |
| `fastapi` (+ `references/`) | model | FastAPI 0.162.5's own `fastapi/.agents/skills/fastapi` | A "fin-tracker overrides" block at the top of `SKILL.md` |

Every skill also dropped upstream's `agents/openai.yaml` (Codex-only) and its issue-tracker setup.

## Updating

- **mattpocock/skills** — vendored from https://github.com/mattpocock/skills at
  `5863806dd4e15c63174bee4e7515a4256792ecfa` (2026-11-12). Re-sync by hand: diff upstream against the
  table above and keep the local changes.
- **fastapi** — after a FastAPI upgrade in `backend/uv.lock`, re-copy
  `backend/.venv/lib/python3.*/site-packages/fastapi/.agents/skills/fastapi/` over `fastapi/`, put the
  overrides block back, and update the version above.

## License

The mattpocock/skills content is MIT-licensed:

> MIT License — Copyright (c) 2026 Matt Pocock
>
> Permission is hereby granted, free of charge, to any person obtaining a copy of this software and
> associated documentation files (the "Software"), to deal in the Software without restriction, including
> without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
> copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the
> following conditions:
>
> The above copyright notice and this permission notice shall be included in all copies or substantial
> portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT
> LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO
> EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER
> IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR
> THE USE OR OTHER DEALINGS IN THE SOFTWARE.

The `fastapi` skill ships inside FastAPI, which is MIT-licensed (Copyright (c) 2018 Sebastián Ramírez).
