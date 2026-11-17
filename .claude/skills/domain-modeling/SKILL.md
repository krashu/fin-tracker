---
name: domain-modeling
description: Build and sharpen a project's domain model. Use when discussing codebase terminology, writing or editing a GLOSSARY.md, or recording or editing an ADR.
---

# Domain Modeling

Actively build and sharpen the project's domain model as you design: challenging terms, inventing edge-case
scenarios, and writing the glossary and decisions down the moment they crystallise. (Merely *reading*
`GLOSSARY.md` for vocabulary is not this skill; this skill is for changing the model.)

## Files

- `GLOSSARY.md` at the repo root — one context. Create it lazily, when the first term is resolved.
- `docs/adr/` — the existing ADRs, indexed in the table in root `AGENTS.md`, with the format in
  `docs/adr/template.md` and the status meanings in `docs/adr/README.md`.
- `PRD.md` holds the feature definitions (F1–F11). It is long: read only the section in play.

The vocabulary already in use is the place to start: `transaction_type` (`spend | income | transfer`),
refund-as-signed-spend (ADR-0009), fingerprint and `occurrence` (ADR-0006), `transfer_pair_id` (ADR-0002),
merchant alias (ADR-0011), category and subcategory (ADR-0012), owned vs global-by-design tables (ADR-0003).

## During the session

### Challenge against the glossary and the ADRs

When the user uses a term that conflicts with `GLOSSARY.md` or a settled ADR, call it out immediately:
"ADR-0009 makes a refund a `spend` row with a positive amount, but you're describing it as its own type.
Which is it?" Settled ADRs are not re-litigated casually: if the user really wants to reverse one, that is
a new ADR that supersedes it.

### Sharpen fuzzy language

When a term is vague or overloaded, propose a precise canonical one. "You're saying 'account': do you mean
the `Account` row (a card, bank or cash ledger), or the `User` who owns it?"

### Discuss concrete scenarios

Stress-test relationships with specific, awkward scenarios: a partial refund across a statement boundary,
two identical rows on one statement, a transfer whose other leg is in an account the user hasn't imported.
Force precision about where one concept ends and the next begins.

### Cross-reference with code

When the user states how something works, check the code agrees, and surface contradictions: "The import
service dedups on fingerprint alone, but you just said two identical rows on one statement are both kept.
`occurrence` is what does that — is that what you mean?"

### Update GLOSSARY.md inline

When a term is resolved, add it to `GLOSSARY.md` right then, in the format in
[GLOSSARY-FORMAT.md](./GLOSSARY-FORMAT.md). The glossary holds definitions only — no implementation
details, specs or decisions.

### Offer ADRs sparingly

Offer an ADR only when all three hold:

1. **Hard to reverse** — changing your mind later costs something real.
2. **Surprising without context** — a future reader will ask "why this way?"
3. **The result of a real trade-off** — there were genuine alternatives.

When it does qualify: copy `docs/adr/template.md` to `docs/adr/NNNN-kebab-title.md`, with NNNN one above
the highest existing number; start it as `Proposed` unless the user has decided; and add its row to the
ADR table in root `AGENTS.md`. Supersession updates the old ADR's status and links both ways.
