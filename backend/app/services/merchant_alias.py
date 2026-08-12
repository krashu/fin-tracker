"""Merchant-string canonicalisation layer (PRD §F3 / ADR-0011).

Second normalisation stage, downstream of and independent from
:func:`app.services.merchant.normalize_merchant` (frozen -- read its CHANGE
HAZARD block before touching anything upstream of this module):

    merchant_raw -> normalize_merchant() -> merchant_normalized   [feeds the F4 fingerprint]
                                                  |
                                          AliasResolver.canonical() -> merchant_canonical   [F3/F3a]

The read sites that consume a resolver: ``import_service`` (the prefill),
``tag_service.prefetch_tag_map`` / ``prefetch_tag_strength``,
``merchant_labels.prefetch_label_map``, and ``api/v1/rules`` (the grouped list
plus the ``/rules/aliases`` write boundary). WRITES stay on the raw
``merchant_normalized`` key -- see the merchant-alias brief's §Writes-stay-raw
note -- so aggregation happens at read time and the F4 fingerprint never sees a
canonical.

PII: merchant strings are user data. This module logs nothing containing a
pattern, a canonical, or a merchant -- matching the deliberate omission
already in ``record_tag`` / ``record_label``.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.merchant_alias import MerchantAlias

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def tokenize(merchant: str) -> tuple[str, ...]:
    """Split on runs of non-alphanumerics.

    ``'swiggy*blr*12345'`` -> ``('swiggy', 'blr', '12345')``. An input with no
    alphanumeric characters at all (``'***'``) returns ``()`` -- the zero-token
    hazard :func:`load_alias_resolver` must guard against.
    """
    return tuple(_TOKEN_RE.findall(merchant))


def tokens_match(pattern_tokens: tuple[str, ...], target_tokens: tuple[str, ...]) -> bool:
    """True if ``pattern_tokens`` appears as a contiguous run within ``target_tokens``.

    The primitive :meth:`AliasResolver.canonical` uses internally. Exposed here
    too because the ``/rules/aliases`` write boundary (Phase A4) needs the same
    check in both directions for decision 7's no-chaining conflict rule: reject
    a submission whose ``canonical`` would itself be matched by an existing
    pattern, or whose ``pattern`` would match an existing ``canonical``.
    """
    n = len(pattern_tokens)
    if n == 0:
        return True  # callers must reject a zero-token pattern before this point
    return any(
        target_tokens[i : i + n] == pattern_tokens for i in range(len(target_tokens) - n + 1)
    )


@dataclass(frozen=True, slots=True)
class AliasResolver:
    """One user's alias table, prepared once per request and queried per row.

    Immutable and session-free by construction: built by load_alias_resolver,
    then pure. ``_rules`` is pre-sorted longest-first (decision 2: token count
    DESC, char length DESC, pattern ASC), so ``canonical()`` takes the first
    contiguous-subsequence match and stops -- resolution is SINGLE-PASS and
    never chains (decision 7): a canonical is never itself re-fed through the
    table.
    """

    _rules: tuple[tuple[tuple[str, ...], str], ...]
    # Per-instance result cache. Mutating a mutable field's CONTENTS is not
    # rebinding, so this is legal under frozen=True and does not weaken the
    # immutability the class docstring claims: canonical() stays a pure function
    # of _rules. compare=False keeps it out of the generated __eq__/__hash__.
    _memo: dict[str, str] = field(default_factory=dict, repr=False, compare=False)

    def canonical(self, merchant_normalized: str) -> str:
        """The F3/F3a key for this merchant.

        Returns the input unchanged when no alias matches (decision 8) --
        that identity is what keeps an empty alias table byte-identical to
        pre-alias behaviour.

        Memoized because the read sites call this per ROW and statement merchant
        strings repeat heavily: with the ~95-entry seed dictionary loaded, an
        unmatched string costs 95 tokens_match calls, each slicing a tuple per
        window position, and a 400-row import paid that on every row.

        EMPTY_RESOLVER is exempt: it is a module-level singleton alive for the
        whole process, so memoizing on it would grow without bound -- and it has
        nothing to cache, since every call returns its own input.
        """
        if not self._rules:
            return merchant_normalized
        hit = self._memo.get(merchant_normalized)
        if hit is not None:
            return hit
        tokens = tokenize(merchant_normalized)
        resolved = merchant_normalized
        for pattern_tokens, canonical in self._rules:
            if tokens_match(pattern_tokens, tokens):
                resolved = canonical
                break
        self._memo[merchant_normalized] = resolved
        return resolved

    def pattern_counts(self) -> dict[str, int]:
        """``{canonical: how many patterns resolve to it}``.

        What ``/rules``'s ``alias_count`` reports. Counted from the resolver's own
        rules rather than a ``SELECT ... GROUP BY canonical``, so a pattern that
        can never fire (zero-token, skipped by :func:`build_resolver`) is not
        counted as folding anything.
        """
        counts: dict[str, int] = {}
        for _, canonical in self._rules:
            counts[canonical] = counts.get(canonical, 0) + 1
        return counts


EMPTY_RESOLVER = AliasResolver(())


def build_resolver(rows: Iterable[tuple[str, str]]) -> AliasResolver:
    """Sort ``(pattern, canonical)`` pairs into a resolver (token count DESC, char
    length DESC, pattern ASC).

    Rows whose pattern tokenizes to ``()`` are SKIPPED -- an empty tuple is a
    contiguous subsequence of every sequence, so an unfiltered zero-token
    pattern would match every merchant and, sorted last, fire on exactly the
    merchants nothing else matched. This is the first of two required guards;
    the second is the 422 at the ``/rules/aliases`` write boundary (Phase A4).

    Split out of :func:`load_alias_resolver` for the second caller,
    ``rules._alias_conflict``, which needs a resolver over a PROSPECTIVE rule
    set (the user's other rows plus the row being written) that no session
    query can produce.
    """
    scored: list[tuple[tuple[str, ...], str, str]] = []
    for pattern, canonical in rows:
        tokens = tokenize(pattern)
        if not tokens:
            continue
        scored.append((tokens, canonical, pattern))
    scored.sort(key=lambda row: (-len(row[0]), -len(row[2]), row[2]))
    return AliasResolver(tuple((tokens, canonical) for tokens, canonical, _ in scored))


def load_alias_resolver(session: Session, *, user_id: uuid.UUID) -> AliasResolver:
    """One user-scoped SELECT, handed to :func:`build_resolver` for ordering.

    The comprehension is not ceremony: ``.execute()`` yields ``Row`` objects,
    which unpack like tuples at runtime but are not ``tuple`` to ``ty``.
    """
    return build_resolver(
        (pattern, canonical)
        for pattern, canonical in session.execute(
            select(MerchantAlias.pattern, MerchantAlias.canonical).where(
                MerchantAlias.user_id == user_id
            )
        )
    )
