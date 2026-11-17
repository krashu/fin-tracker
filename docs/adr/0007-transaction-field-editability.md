# ADR-0007: Transaction field editability on PATCH — the mutable set, identity recompute, and `origin_fingerprint`

**Status**: Superseded by [ADR-0006](0006-f4-dedup-key.md)
**Date**: 2026-08-03 (decided) · **Revised**: 2026-11-15

This decision now lives in [ADR-0006](0006-f4-dedup-key.md), which is the single record of transaction
identity. The PATCH mutable set (seven user-visible fields — six columns plus labels), identity
recompute, the `occurrence` reset, sign and kind checks, the transfer guard and `origin_fingerprint` are
its rule groups C and D. Code and docs that cite ADR-0007 should be read as citing those groups; no rule
was dropped in the merge.

## History

- 2026-08-03 — Accepted.
- 2026-08-12 — Sign rule amended by ADR-0009.
- 2026-11-15 — Merged into ADR-0006; this file is kept so existing `ADR-0007` citations still resolve.
