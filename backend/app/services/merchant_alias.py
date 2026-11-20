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
plus the ``/rules/aliases`` write boundary). New teaches write the canonical
(``learn_merchant_memory`` folds through a resolver, ADR-0004 rule 1); rows keyed on a
descriptor an alias now covers are not re-keyed and fold at read time, which stays because
aliases are editable. The F4 fingerprint never sees a canonical.

Identity is the default, except gateway handles: a merchant no alias matches whose first
UPI VPA identifies nobody resolves to ``unknown via <psp>`` (ADR-0011 rule 12), and aliases
never match a UPI ``@handle``.

PII: merchant strings are user data. This module logs nothing containing a
pattern, a canonical, or a merchant -- matching the deliberate omission
already in ``record_tag`` / ``record_label``.
"""

from __future__ import annotations

import itertools
import re
import unicodedata
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.merchant_alias import MerchantAlias
from app.services.provisioning import PAYMENT_GATEWAYS, PSP_HANDLES, UPI_QR_IDS

# ADR-0011 rule 12: the canonical of a gateway handle, and the reserved canonical prefix.
UNKNOWN_VIA_PREFIX = "unknown via "

_HANDLE_RE = re.compile(r"@[a-z0-9]+")
# First UPI VPA. ``-`` is excluded from the local part because narrations use it as a field
# separator (HDFC: ``UPI-<name>-<vpa>-<ifsc>-...``); the one exception is PhonePe's secondary
# form ``9876543210-2``, which the first alternative captures whole.
_VPA_RE = re.compile(r"([0-9]{6,}-[0-9]{1,2}|[a-z0-9._]+)@([a-z0-9]+)")
_NUMERIC_LOCAL_RE = re.compile(r"[0-9]{6,}(?:-[0-9]{1,2})?")
_GATEWAY_LOCAL_RE = re.compile(
    "(" + "|".join(sorted(PAYMENT_GATEWAYS, key=len, reverse=True)) + r")(?:[._]?[0-9]+)?"
)
_QR_LOCALS = tuple((re.compile(pattern), issuer) for pattern, issuer in UPI_QR_IDS)
_PSP_BY_HANDLE = dict(PSP_HANDLES)


def _is_token_char(c: str) -> bool:
    """Unicode letters, combining marks and digits (general categories L, M, N).

    Marks matter: ``str.isalnum`` alone splits an Indic word at every vowel sign
    and virama (both are marks), so ``स्विगी`` would become three tokens.
    """
    return unicodedata.category(c)[0] in "LMN"


def tokenize(merchant: str) -> tuple[str, ...]:
    """Split into maximal runs of Unicode letters, combining marks and digits.

    ``'swiggy*blr*12345'`` -> ``('swiggy', 'blr', '12345')``; ``'café'`` is one
    token. Anything else, underscore included, separates. An input with no
    token characters at all (``'***'``) returns ``()`` -- the zero-token hazard
    :func:`load_alias_resolver` must guard against.
    """
    return tuple("".join(run) for keep, run in itertools.groupby(merchant, _is_token_char) if keep)


def alias_tokens(merchant: str) -> tuple[str, ...]:
    """The tokens an alias is matched on: :func:`tokenize` with every UPI ``@handle`` removed.

    ADR-0011 rule 12: ``9876543210@airtel`` must not hit the ``airtel`` alias, and a pattern's
    own handle is ignored too, so ``9876543210@ybl`` matches that payee on any handle. The
    local part's tokens are kept (``upi/airtel.recharge@icici`` still reaches ``airtel``).
    """
    return tokenize(_HANDLE_RE.sub(" ", merchant))


def is_unknown_via(canonical: str) -> bool:
    """True for a gateway-handle canonical, which names no merchant (ADR-0011 rule 12).

    Sound because the prefix is reserved at the ``/rules/aliases`` boundary.
    """
    return canonical.startswith(UNKNOWN_VIA_PREFIX)


def gateway_canonical(merchant_normalized: str) -> str | None:
    """``"unknown via <psp>"`` when the first UPI VPA's local part identifies nobody, else ``None``.

    Judges only the VPA (ADR-0011 rule 12). Shapes are tried in order: a gateway name
    (alone or with digits), a QR id, then six-plus digits (a phone number or terminal id,
    optionally ``-N``). The PSP is the gateway or QR issuer the local part names, else the
    handle's app (``ybl`` -> ``phonepe``), else the raw handle.
    """
    vpa = _VPA_RE.search(merchant_normalized)
    if vpa is None:
        return None
    local, handle = vpa.group(1), vpa.group(2)
    by_handle = _PSP_BY_HANDLE.get(handle, handle)
    gateway = _GATEWAY_LOCAL_RE.fullmatch(local)
    if gateway is not None:
        return UNKNOWN_VIA_PREFIX + gateway.group(1)
    for qr, issuer in _QR_LOCALS:
        if qr.fullmatch(local):
            return UNKNOWN_VIA_PREFIX + (issuer or by_handle)
    if _NUMERIC_LOCAL_RE.fullmatch(local):
        return UNKNOWN_VIA_PREFIX + by_handle
    return None


def tokens_match(pattern_tokens: tuple[str, ...], target_tokens: tuple[str, ...]) -> bool:
    """True if ``pattern_tokens`` appears as a contiguous run within ``target_tokens``.

    The primitive :meth:`AliasResolver.canonical` uses internally. Exposed here
    too because the ``/rules/aliases`` write boundary uses it for the pattern-side check
    of ADR-0011 rule 5: reject a submission whose ``pattern`` would match an existing
    ``canonical``. The canonical side is a fixed-point check through
    :func:`build_resolver`.
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
    never chains (ADR-0011 rule 5): a canonical is never itself re-fed through the
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

        Returns the input unchanged when no alias matches (ADR-0011 rule 6), unless
        it is a gateway handle, which resolves to ``unknown via <psp>`` (rule 12);
        aliases never match a UPI ``@handle``.

        Memoized because the read sites call this per ROW and statement merchant
        strings repeat heavily: with the ~95-entry seed dictionary loaded, an
        unmatched string costs 95 tokens_match calls, each slicing a tuple per
        window position, and a 400-row import paid that on every row.

        EMPTY_RESOLVER is exempt: it is a module-level singleton alive for the
        whole process, so memoizing on it would grow without bound -- and it has
        nothing to cache (it only runs the gateway check, which is not worth a memo).
        """
        if not self._rules:
            return gateway_canonical(merchant_normalized) or merchant_normalized
        hit = self._memo.get(merchant_normalized)
        if hit is not None:
            return hit
        tokens = alias_tokens(merchant_normalized)
        resolved = None
        for pattern_tokens, canonical in self._rules:
            if tokens_match(pattern_tokens, tokens):
                resolved = canonical
                break
        if resolved is None:
            resolved = gateway_canonical(merchant_normalized) or merchant_normalized
        self._memo[merchant_normalized] = resolved
        return resolved

    def patterns_for(self, canonical: str) -> list[tuple[str, ...]]:
        """Return all pattern token-tuples that resolve to ``canonical``."""
        return [pattern_tokens for pattern_tokens, c in self._rules if c == canonical]

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


# Identity except gateway handles (ADR-0011 rules 6 and 12).
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
        tokens = alias_tokens(pattern)
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
