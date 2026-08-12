# ADR-0010: `parse()` returns `ParsedStatement`, not `list[RawTransaction]`

**Status**: Accepted
**Date**: 2026-08-12

## Problem

`StatementParser.parse(cls, pdf_bytes, password) -> list[RawTransaction]` has nowhere to put
**statement-level metadata** — the opening/closing balance and billing period a statement prints
once, beside the transaction rows, not per row. PRD §Roadmap "Considered, unscheduled" had parked
**balance reconciliation** (a user-visible check that catches a missed page, a duplicated import,
or an F4 false-positive dedup that silently dropped a row) specifically because the parser
protocol had no seam for that metadata to travel through — widening `parse()`'s return type is a
one-way door across both shipped parsers (`axis_cc.py`, `icici_cc.py`) and their one caller
(`import_service.py`), so the shape has to be right before a second feature builds on it.

## Decision

**`parse()` returns a `ParsedStatement` — `rows: list[RawTransaction]` plus
`summary: StatementSummary` — and a parser gains a second method, `interpret_summary`, to
produce it.**

```python
@dataclass(frozen=True, slots=True)
class StatementSummary:
    """Statement-level metadata. Every field independently optional — a layout
    that prints no summary block yields an all-None instance, never None itself."""
    opening_balance_paise: int | None = None
    closing_balance_paise: int | None = None
    period_start: date | None = None
    period_end: date | None = None


@dataclass(frozen=True, slots=True)
class ParsedStatement:
    """Everything one statement file yields."""
    rows: list[RawTransaction]
    summary: StatementSummary


class StatementParser(Protocol):
    @classmethod
    def parse(cls, pdf_bytes: bytes, password: str | None) -> ParsedStatement: ...
```

Per-parser surface after the change:

| Method | Signature | Changed? |
|---|---|---|
| `parse` | `(pdf_bytes, password) -> ParsedStatement` | **Yes — the one-way door** |
| `interpret_tables` | `(tables) -> list[RawTransaction]` | No |
| `interpret_summary` | `(lines: Sequence[str]) -> StatementSummary` | New |

`parse()` becomes: `_decrypt` → (`_extract_tables` → `interpret_tables`) for rows, and
(`_extract_text` → `interpret_summary`) for metadata, composed into one `ParsedStatement`.

**`StatementSummary` is always present, its fields independently optional** — a layout that
prints no summary block (or one a parser can't read) returns an all-`None` `StatementSummary`,
never `None` itself and never a `ParserError`. That removes a `None`-of-`None` double check at
every read site, and it is the rule for every future statement parser: implement `parse` →
`ParsedStatement` and `interpret_summary` → `StatementSummary`; an all-`None` summary is a
legitimate result, not a failure.

`StatementSummary` nests **inside** `ParsedStatement` rather than flattening its four fields onto
it, for one reason that matters: `interpret_summary(lines) -> StatementSummary` is independently
testable and independently fixtured — exactly what the text-extraction path needs. Flattened, the
summary scanner would have no return type of its own to test against.

**Migration path**: atomic, in one commit, no compatibility shim — no `ParsedStatement.__iter__`
back-compat trick, no transitional union return. fin-tracker is pre-release; correct new
behaviour outright beats a shim, and the call sites are two parsers and **one** consumer
(`import_service.py`, `rows = parser_cls.parse(...)`).

**Relationship to §3.3 (split the PDF→text parser seam) — this lands first, kept apart from it.**
They touch the same protocol at different seams: §3.3 changes the *input* type of the
interpretation step (`pdf_bytes → list[str] → rows`); this ADR changes its *output* type.
Sequencing this first means §3.3's target signature (`interpret_*(lines) -> ParsedStatement`) is
already correct in advance — §3.3 only has to move the input, not also design a return shape —
and `_extract_text()` (introduced here to read the summary block) is a genuine down-payment on
§3.3's text-extraction need, scoped to one job, with no fixture-format rewrite forced on it.

**What not to do:**

- Don't add a `.issuer` or `.type` attribute to a parser class — dispatch stays entirely in the
  `PARSERS` table keyed on the account's `(issuer, type)` columns.
- Don't raise `ParserError` for an all-`None` `StatementSummary` — an unreadable layout and an
  empty statement are already distinguishable via `rows`; a missing summary block is its own,
  separate, non-error signal.
- Don't add a compatibility shim for the old `list[RawTransaction]` return — see Migration path.

## Trade-offs

**Benefits:**

- One parser call yields everything a statement file has to offer — rows and metadata can never
  disagree about which file they came from, unlike a second call or a second dispatch table would
  risk.
- `StatementSummary`'s independent optionality means "this layout has no balance block" is
  representable without a sentinel or a second `None` check downstream (in
  `reconciliation_service.reconcile_batch`, `opening is None or closing is None` is the entire
  gate).
- Sets up §3.3 for free — its target return type already exists.

**Drawbacks:** every statement parser now implements two methods instead of one, and a parser
that genuinely has no summary block to interpret still has to define `interpret_summary` (even if
its body is one line returning the all-`None` default).

**Mitigation:** the two-method split is exactly what makes `interpret_summary` independently
fixturable — the cost buys the thing PRD §Verification's closing-balance check needs (a summary
that can be snapshot-tested on its own, not only as a side effect of testing row parsing).

## Alternatives Considered

**Alternative 1 — a second optional classmethod on the protocol** (e.g. `parse_summary` called
separately from `parse`): rejected on three counts. It needs a second `_decrypt` + pdfplumber
pass, or threaded state between two calls; "optional method on a `Protocol`" means a `hasattr`
check at the call site, which `ty` cannot enforce against a `dict[..., type[StatementParser]]`
dispatch table (ADR-0005's type checker has no way to verify every registered parser implements
the optional method); and it splits one statement's information across two calls that can
disagree about which file they actually read.

**Alternative 2 — a separate `BalanceExtractor` strategy table keyed on `(issuer, type)`**:
rejected. A second dispatch table registered the same way as `PARSERS` reproduces the
silent-404-router class of bug (AGENTS.md §Layout) in a different guise, and it is abstraction
ahead of the second concrete use (AGENTS.md §Simplicity first) — there is exactly one place a
statement's balance metadata needs to reach.

**Alternative 3 — metadata on a mutable class attribute** set by `parse()` and read back
separately: rejected outright. Hidden state on a classmethod-only object has no upside over a
return value and every downside of shared mutable state on a class the dispatch table treats as
stateless.
