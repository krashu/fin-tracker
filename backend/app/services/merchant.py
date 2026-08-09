"""Merchant-string normalization.

Single source of truth for the normalized merchant string used by:

* :func:`app.services.fingerprint.transaction_fingerprint` (PRD §F4 dedup).
* :class:`app.models.merchant_tag_map.MerchantTagMap` (PRD §F3 auto-tagging,
  arriving in a later increment).

v1 is intentionally minimal: lowercase + whitespace collapse.

CHANGE HAZARD (ADR-gated). This one function's output is the shared key for
THREE stores, so changing it for previously-seen inputs breaks all three,
silently:

* the F4 dedup fingerprint (:mod:`app.services.fingerprint`) — a changed key
  no longer matches stored fingerprints, so re-imports duplicate;
* the F3 ``merchant_tag_map`` key — a changed key stops matching learned
  category rules, so auto-tag silently misses;
* the F3a ``merchant_label_map`` key — likewise for learned label rules.

F3's planned extension (regex stripping of RRNs, auth codes, date / reference
suffixes) is exactly such a change. It is therefore NOT a code edit but a
**recompute migration**: any change here must recompute stored fingerprints AND
rewrite both map-table keys in ONE revision, so the change is a controlled
backfill rather than silent corruption.

The procedure is written down — see ``docs/adr/0006-f4-dedup-key.md``
§Recompute procedure, with ``alembic/versions/0025_fingerprint_separator_and_occurrence.py``
as the reference implementation. Read it before touching this function. The trap
it exists to flag: RRN stripping will COLLAPSE two old keys onto one new key, and
both map tables carry a UNIQUE on ``merchant_normalized``, so the backfill must
MERGE the rows (sum ``hit_count``, max ``last_used_at``, OR ``pinned``, delete the
loser) — a naive ``UPDATE`` raises IntegrityError and ``UPDATE OR IGNORE`` silently
discards the user's learned history.
"""

from __future__ import annotations


def normalize_merchant(merchant_raw: str) -> str:
    """Lowercase + collapse all whitespace to single spaces."""
    return " ".join(merchant_raw.lower().split())
