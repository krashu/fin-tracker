# ADR-NNNN: <the decision, stated as a rule>

**Status**: Proposed | Accepted | Superseded by ADR-XXXX | Deprecated | Rejected
**Date**: YYYY-MM-DD (decided) · **Revised**: YYYY-MM-DD
**Scope**: PRD §F… · `app/services/…`, `app/models/…`
**Supersedes / Related**: ADR-XXXX

<!--
Scope names files and symbols, never line numbers. Terms are defined in GLOSSARY.md, not here.
The ADR states the target state: where code disagrees, the ADR wins and the code is the bug.
Aim for 60–120 lines.
-->

## Decision

One or two sentences an agent can act on without reading further.

## Rules

1. <rule, testable> — *Enforced by*: DB constraint | service | schema validation | tooling ·
   *Locked by*: `tests/…::test_name`

<!--
Enforced by: what actually stops a violation. Locked by: the test that fails if the rule breaks;
"(planned)" after a test name marks one the code rewrite adds; "none (by choice)" says no gate is
wanted, and why sits in Why.
-->

## Out of bounds

What this ADR deliberately does not allow or cover, stated as positive boundaries.

## Why

Problem and forces, short. Link terms to [GLOSSARY.md](../../GLOSSARY.md) rather than redefining
them.

## Alternatives rejected

- **<name>** — one line.

## Consequences

What gets easier or harder, and the triggers that would reopen this decision.

## History

- YYYY-MM-DD — one line per material change.
