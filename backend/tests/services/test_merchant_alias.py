"""Tests for :mod:`app.services.merchant_alias` -- the resolver primitives.

Scope is this module only: ``tokenize`` / ``tokens_match`` semantics, longest-first
ordering, the zero-token guard, user scoping, and the ``canonical()`` memo. The
read sites that consume a resolver are covered where they live
(``tests/api/test_rules.py``, ``tests/services/test_provisioning.py``,
``tests/services/test_import_service.py``).
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.models import MerchantAlias, User
from app.services.merchant_alias import (
    EMPTY_RESOLVER,
    AliasResolver,
    load_alias_resolver,
    tokenize,
)

# ---------- tokenize --------------------------------------------------------


def test_tokenize_splits_on_non_alphanumeric_runs() -> None:
    assert tokenize("swiggy*blr*12345") == ("swiggy", "blr", "12345")


def test_tokenize_slash_and_at() -> None:
    assert tokenize("upi/swiggy/9876@ybl") == ("upi", "swiggy", "9876", "ybl")


def test_tokenize_empty_string() -> None:
    assert tokenize("") == ()


def test_tokenize_all_punctuation_is_the_zero_token_hazard() -> None:
    """normalize_merchant("***") == "***" (non-blank) but tokenize sees no
    alphanumerics at all -- this is the case load_alias_resolver must guard."""
    assert tokenize("***") == ()


# ---------- AliasResolver.canonical -----------------------------------------


def _resolver(*rules: tuple[tuple[str, ...], str]) -> AliasResolver:
    return AliasResolver(tuple(rules))


def test_canonical_matches_contiguous_subsequence_anywhere_in_merchant() -> None:
    r = _resolver((("swiggy",), "swiggy"))
    assert r.canonical("swiggy blr 12345") == "swiggy"
    assert r.canonical("swiggy blr 67890") == "swiggy"
    assert r.canonical("upi swiggy 9876 ybl") == "swiggy"


def test_canonical_does_not_match_disjoint_tokens() -> None:
    """'ola' must not match 'chocolate hut' -- no shared token at all."""
    r = _resolver((("ola",), "ola"))
    assert r.canonical("chocolate hut") == "chocolate hut"


def test_canonical_identity_when_no_alias_matches() -> None:
    assert EMPTY_RESOLVER.canonical("some random merchant") == "some random merchant"


def test_canonical_takes_first_match_and_stops() -> None:
    """AliasResolver trusts its rules are pre-sorted (longest-first, decision 2)
    and takes the first contiguous match without scanning further -- the
    *sorting* that makes longest-pattern-wins true is load_alias_resolver's
    job (see test_load_alias_resolver_orders_longest_pattern_first below),
    not canonical()'s."""
    r = _resolver((("big", "basket"), "groceries"), (("basket",), "wrong"))
    assert r.canonical("big basket online") == "groceries"


def test_canonical_is_single_pass_no_chaining() -> None:
    """A canonical equal to another rule's pattern must not be re-fed through
    the table (decision 7)."""
    r = _resolver((("swiggy",), "food"), (("food",), "should-not-apply"))
    assert r.canonical("swiggy blr") == "food"


# ---------- load_alias_resolver ---------------------------------------------


def _make_alias(
    session: Session, user_id: uuid.UUID, pattern: str, canonical: str
) -> MerchantAlias:
    row = MerchantAlias(user_id=user_id, pattern=pattern, canonical=canonical)
    session.add(row)
    session.flush()
    return row


def test_load_alias_resolver_skips_zero_token_rows(session: Session, user: User) -> None:
    """A '***'-style row must never load -- an empty pattern-token tuple would
    match every merchant and, sorted last, fire on exactly the unmatched
    ones."""
    _make_alias(session, user.id, "***", "junk")
    _make_alias(session, user.id, "swiggy", "swiggy")
    resolver = load_alias_resolver(session, user_id=user.id)
    unrelated = "some totally unrelated merchant"
    assert resolver.canonical(unrelated) == unrelated
    assert resolver.canonical("swiggy blr") == "swiggy"


def test_load_alias_resolver_orders_longest_pattern_first(session: Session, user: User) -> None:
    _make_alias(session, user.id, "basket", "wrong")
    _make_alias(session, user.id, "big basket", "groceries")
    resolver = load_alias_resolver(session, user_id=user.id)
    assert resolver.canonical("big basket online") == "groceries"


def test_load_alias_resolver_is_user_scoped(session: Session, user: User) -> None:
    other = User(id=uuid.uuid4())
    session.add(other)
    session.flush()
    _make_alias(session, other.id, "swiggy", "food")
    resolver = load_alias_resolver(session, user_id=user.id)
    # identity -- other user's alias must be invisible to this one
    assert resolver.canonical("swiggy blr") == "swiggy blr"


def test_load_alias_resolver_empty_table_matches_empty_resolver_behaviour(
    session: Session, user: User
) -> None:
    resolver = load_alias_resolver(session, user_id=user.id)
    assert resolver.canonical("anything") == "anything"


# ---------- memoization -----------------------------------------------------


def test_canonical_memoizes_repeat_lookups() -> None:
    """A second lookup of the same string must not re-scan the rules.

    The read sites call ``canonical()`` per row and statement merchant strings
    repeat heavily, so this is the difference between one scan and hundreds.
    """
    r = _resolver((("swiggy",), "swiggy"))
    assert r.canonical("swiggy blr 12345") == "swiggy"
    assert r._memo == {"swiggy blr 12345": "swiggy"}

    # Swap in a rule set that would resolve the same string differently. A cached
    # lookup must not consult it; an uncached one must.
    object.__setattr__(r, "_rules", ((("blr",), "bengaluru"),))
    assert r.canonical("swiggy blr 12345") == "swiggy"  # served from the memo
    assert r.canonical("zomato blr 999") == "bengaluru"  # scanned afresh


def test_canonical_memoizes_the_identity_fallback_too() -> None:
    """An unmatched string is the EXPENSIVE case (every rule is tried and fails),
    so it is exactly the one worth caching."""
    r = _resolver((("swiggy",), "swiggy"))
    assert r.canonical("zomato blr") == "zomato blr"
    assert r._memo == {"zomato blr": "zomato blr"}


def test_empty_resolver_never_memoizes() -> None:
    """EMPTY_RESOLVER is a module-level singleton alive for the whole process, so
    a memo on it would grow without bound. It also has nothing to cache — every
    call returns its own input."""
    assert EMPTY_RESOLVER.canonical("anything") == "anything"
    assert EMPTY_RESOLVER._memo == {}
