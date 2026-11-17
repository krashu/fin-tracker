# ADR-0008: F3 UPI merchant normalisation — deferred (superseded)

**Status**: Superseded by [ADR-0011](0011-merchant-alias-layer.md)
**Date**: 2026-08-03 (decided) · **Revised**: 2026-11-15
**Scope**: PRD §F3 · `app/services/merchant.py` (`normalize_merchant`)
**Supersedes / Related**: superseded by ADR-0011

## Decision

Superseded. This ADR measured that `normalize_merchant` (lower-case plus whitespace collapse) keeps every
per-transaction reference in UPI and card descriptors, so learning keys rarely repeat. It deferred any
fix past v1 and mitigated in the import review UI instead. [ADR-0011](0011-merchant-alias-layer.md)
replaced the deferral with a merchant alias layer downstream of the unchanged `normalize_merchant`. The
measurements are summarised in ADR-0011 §Why; the rules that carried forward are ADR-0011's.

## History

- 2026-08-03 — Accepted: measured non-convergence on real statements; `normalize_merchant` unchanged in
  v1; bulk select in the review queue; reopen trigger recorded.
- 2026-08-12 — Superseded by ADR-0011 (merchant alias layer and seed dictionary).
- 2026-11-15 — Reduced to a stub; evidence moved to ADR-0011 §Why.
