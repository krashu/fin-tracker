"""Merchant-string normalization.

Single source of truth for the normalized merchant string used by:

* :func:`app.services.fingerprint.transaction_fingerprint` (PRD §F4 dedup).
* :class:`app.models.merchant_tag_map.MerchantTagMap` (PRD §F3 auto-tagging).

v1 is intentionally minimal: lowercase, NFC, whitespace collapse.

CHANGE HAZARD (ADR-gated, ADR-0006 rules 18-21). This one function's output is the
shared key for every store below, so changing it for previously-seen inputs breaks
them, silently:

* ``transactions.merchant_normalized`` and ``.origin_merchant_normalized``;
* ``transactions.fingerprint`` and ``.origin_fingerprint`` (the F4 dedup key,
  :mod:`app.services.fingerprint`) -- recomputed from the origin inputs when set,
  else from the row's own; a changed key no longer matches stored fingerprints, so
  re-imports duplicate;
* ``merchant_tag_map.merchant_normalized`` (F3) and ``merchant_label_map.merchant_normalized``
  (F3a) -- they hold canonicals, which are normalized strings; a changed key stops
  matching learned rules, so auto-tag silently misses. Unique keys:
  ``uq_merchant_tag_map_user_merchant_category`` is ``(user_id, merchant_normalized,
  category_id)`` and the label map's is ``(user_id, merchant_normalized, label_id)``;
* ``merchant_alias.pattern`` and ``.canonical`` (unique on ``(user_id, pattern)``);
* the seeded alias rows (``provisioning._MERCHANT_DICTIONARY`` and migration 0032's literal).

F3's planned extension (regex stripping of RRNs, auth codes, date / reference
suffixes) is exactly such a change. It is therefore NOT a code edit but a
**recompute migration**: ONE revision must rewrite all of the stores above, so the
change is a controlled backfill rather than silent corruption. NFC (ADR-0006 rule 3) shipped
without a recompute under rule 18's one-time exception; every further change is a recompute.

The procedure is ADR-0006 rules 18-21, with
``alembic/versions/0025_fingerprint_separator_and_occurrence.py`` as the reference
implementation. Read it before touching this function. Rule 19: the migration inlines
the hash and never imports ``app.*``. Rule 20, the trap: RRN stripping will COLLAPSE two
old keys onto one new key, and the unique keys above would then collide, so the
backfill must MERGE the rows (sum ``hit_count``, max ``last_used``, OR ``pinned``,
delete the loser) -- a naive ``UPDATE`` raises IntegrityError and ``UPDATE OR IGNORE``
silently discards the user's learned history.

Also read by (not stores): ADR-0011 rule 12's VPA regexes and PSP lists in
``merchant_alias.py`` / ``provisioning.py`` depend on this output's shape (lowercase,
``@`` and digit runs kept). A recompute that strips digit runs or VPAs must revisit them.
"""

from __future__ import annotations

import unicodedata

# The width of every merchant column (ADR-0006 rule 3b). Models spell it ``String(512)``
# and do not import services; ``tests/models`` pins the two together.
MERCHANT_MAX_CHARS = 512


def normalize_merchant(merchant_raw: str) -> str:
    """Lowercase, then NFC, then collapse all whitespace to single spaces (ADR-0006 rule 3)."""
    return " ".join(unicodedata.normalize("NFC", merchant_raw.lower()).split())


def merchant_too_long(raw: str) -> bool:
    """True when ``raw`` or its normalized form will not fit a merchant column.

    Normalizing can lengthen text (``İ`` lower-cases to two code points; NFC decomposes
    U+0958 and U+FB2C into up to three), so a capped raw does not bound the stored key.
    Every write boundary refuses such a merchant rather than truncating it, because a
    truncated key is a different string from the one ADR-0006 rule 3 defines.
    """
    return len(raw) > MERCHANT_MAX_CHARS or len(normalize_merchant(raw)) > MERCHANT_MAX_CHARS
