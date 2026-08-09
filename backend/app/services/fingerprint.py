"""Transaction fingerprint — the dedup key from PRD §F4.

Single source of truth for the formula. Pure function: no DB, no HTTP,
no logging. Both the future ``import_service`` and any test asserting
dedup behavior should import from here rather than re-typing the hash.

The contract is locked in ``docs/adr/0006-f4-dedup-key.md``, which also carries the
**recompute procedure** any future change must follow. Two rules from it matter here:

* This stays a **pure 4-input identity function**. Nothing positional, derived, or
  install-local ever enters the payload — multiplicity lives in
  ``transactions.occurrence``, never in the hash.
* Callers pass an already-normalized merchant from the shared
  ``services/merchant.py:normalize_merchant``, whose output is ALSO the key for the
  F3 ``merchant_tag_map`` and F3a ``merchant_label_map`` tables. A change to its
  rules (the planned Indian RRN / auth-code stripping) breaks dedup AND both
  auto-tag stores at once, silently. Such a change is a recompute migration, not a
  code edit — see ADR-0006 §Recompute procedure and ``normalize_merchant``'s
  CHANGE HAZARD note.
"""

from __future__ import annotations

import hashlib
from datetime import date

# ASCII Unit Separator. Provably absent from every field, so the join is
# injection-proof: ``date.isoformat()`` is ``[0-9-]``, the two ints are ``[-0-9]``,
# and ``normalize_merchant``'s ``" ".join(raw.lower().split())`` deletes it because
# ``'\x1f'.isspace()`` is True. Pinned by
# ``tests/services/test_fingerprint.py::test_separator_cannot_appear_in_a_normalized_merchant``.
_SEP = "\x1f"


def transaction_fingerprint(
    *,
    txn_date: date,
    amount_paise: int,
    normalized_merchant: str,
    account_id: int,
) -> str:
    """Compute the SHA-256 fingerprint identifying a unique transaction.

    Returns a 64-character lowercase hex string. The four inputs are joined in a
    stable order with ``\\x1f`` (PRD §F4, as amended by ADR-0006).

    The separator is load-bearing, not cosmetic: concatenating the fields directly
    left two ambiguous boundaries between variable-length values, so merchant
    ``"amazon1"`` + account ``2`` hashed identically to ``"amazon"`` + account ``12``.
    """
    payload = _SEP.join(
        (txn_date.isoformat(), str(amount_paise), normalized_merchant, str(account_id))
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
