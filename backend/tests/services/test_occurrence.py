"""Unit tests for the ADR-0006 occurrence allocator.

The three importers each own an integration test for the *observable* behaviour (two
identical rows import as occurrences 0 and 1; a re-upload stages nothing; deleting one
and re-uploading re-stages exactly one). These cover the allocator's own contract —
including the falsy-return trap, which no integration test can see because a caller who
gets it wrong produces "everything deduped", which reads as success.
"""

from __future__ import annotations

from app.services.occurrence import OccurrenceAllocator


def test_allocate_returns_zero_for_a_fresh_key() -> None:
    """``0`` is a VALID return, and the most common one — NOT a falsy "skip" signal.

    This is the trap the return type invites: a caller written ``if not occ: continue``
    would skip every genuinely-new row and import nothing, and the dedup suites would
    read that as "everything deduped" rather than as a failure. Asserting ``== 0``
    (never ``assert not``) is what pins it.
    """
    allocator = OccurrenceAllocator[str]({})

    occurrence = allocator.allocate("fp-never-seen")

    assert occurrence == 0
    assert occurrence is not None


def test_repeated_key_in_one_file_climbs() -> None:
    """The per-file tally is the other half of the multiset difference."""
    allocator = OccurrenceAllocator[str]({})

    assert allocator.allocate("fp") == 0
    assert allocator.allocate("fp") == 1
    assert allocator.allocate("fp") == 2


def test_first_db_count_copies_are_absorbed_then_the_surplus_is_new() -> None:
    """A multiset difference, not set membership: the DB holds 2, the file yields 3, so
    exactly one is new. Under set membership all three would collapse to a skip."""
    allocator = OccurrenceAllocator({"fp": (2, 1)})

    assert allocator.allocate("fp") is None
    assert allocator.allocate("fp") is None
    assert allocator.allocate("fp") == 2


def test_next_ordinal_comes_from_max_not_count() -> None:
    """Occurrences can be gapped — the user deleted occurrence 0 and kept 1 — so the DB
    holds 1 row whose MAX is 1. Assigning ``db_count`` would reuse the occupied slot 1
    and trip the uniqueness constraint; ``db_max_occ + surplus`` yields 2."""
    allocator = OccurrenceAllocator({"fp": (1, 1)})

    assert allocator.allocate("fp") is None  # the surviving copy absorbs the file's first
    assert allocator.allocate("fp") == 2


def test_keys_are_independent() -> None:
    """Each dedup key carries its own tally — one busy fingerprint can't shift another."""
    allocator = OccurrenceAllocator({("a", "x"): (1, 0)})

    assert allocator.allocate(("a", "x")) is None
    assert allocator.allocate(("b", "y")) == 0
    assert allocator.allocate(("a", "x")) == 1


def test_allocate_counts_a_row_the_caller_then_discards() -> None:
    """The tally increments on every call, including ones whose ordinal is thrown away.

    ``investment_import_service`` allocates BEFORE its FX and oversell rejects, so a
    rejected row still consumes a slot. That is deliberate — see the call-site comment
    and ``test_a_duplicate_row_is_skipped_before_the_oversell_guard_sees_it``.
    """
    allocator = OccurrenceAllocator[str]({})

    discarded = allocator.allocate("fp")
    assert discarded == 0

    assert allocator.allocate("fp") == 1
