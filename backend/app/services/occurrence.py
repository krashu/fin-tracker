"""The ADR-0006 occurrence allocator — one home for the multiset-difference rule.

`ADR-0006 <../../../docs/adr/0006-f4-dedup-key.md>`_ pins identity in the fingerprint
hash and *multiplicity* in an ``occurrence`` ordinal. Assigning that ordinal was written
three times, character-identical, in ``import_service``, ``backup_import_service`` and
``investment_import_service`` — and the drift the ADR exists to record already happened:
the same in-loop-mutation defect was fixed in two copies by ``6b8e10c`` and in the third
by ``83d157d`` two commits later, during which window investment CSV import silently
dropped genuinely-distinct duplicate rows.

**Dedup is a multiset difference, not set membership.** The DB already holds ``db_count``
rows for a key, so the file's first ``db_count`` copies are accounted for and only the
surplus is new. That is identical to a set-membership test whenever every count is 0 or
1, which is why adopting it left the pre-existing dedup tests unchanged.

**MAX is tracked, not just COUNT.** Occurrences can be gapped — a user deletes occurrence
0 and keeps 1 — and reusing an occupied slot would trip the uniqueness constraint. So the
next ordinal is ``db_max_occ + (seen - db_count)``, not ``db_count``.

What deliberately does **not** live here: each importer keeps its own prefetch ``SELECT``.
The key tuple (``fingerprint`` / ``(account_id, fingerprint)`` /
``(instrument_id, fingerprint)``), the scope window (``import_service`` narrows to the
statement's date range; the other two do not) and investment's ``fingerprint IS NOT NULL``
filter are genuinely per-source decisions, not parameters of this algorithm. Each importer
also keeps its own skip counter, because they do not all mean the same thing —
``import_service``'s ``skipped`` also counts zero-paise rows.

**Postgres v2 cutover, one grep target.** None of the three call sites has a per-row
SAVEPOINT to recover from the ``IntegrityError`` a concurrent upload would raise: two
requests can each pass the check and both insert, leaving the uniqueness constraint as a
safety net rather than a recovery path. The real fix when Postgres lands is a
dialect-aware ``INSERT ... ON CONFLICT (user_id, account_id, fingerprint, occurrence) DO
NOTHING`` (ADR-0006 §Benefits). That note used to exist in exactly one of the three files,
so a grep-driven migration author would have found one call site and missed two.
"""

from __future__ import annotations


class OccurrenceAllocator[K]:
    """Assigns the next ``occurrence`` ordinal for a dedup key, or skips a duplicate.

    Construct with the caller's prefetch of committed DB state, then call
    :meth:`allocate` once per parsed row **in file order** — the allocator holds the
    per-file ``seen`` tally that is the other half of the multiset difference.

    One instance per import run. It is not reusable across files: ``seen`` is cumulative
    by design.
    """

    def __init__(self, existing_counts: dict[K, tuple[int, int]]) -> None:
        """``existing_counts`` maps a dedup key to ``(row_count, max_occurrence)``.

        A key absent from the map is one the DB has never seen, treated as
        ``(0, -1)`` so its first sighting allocates occurrence 0.
        """
        self._existing_counts = existing_counts
        self._seen: dict[K, int] = {}

    def allocate(self, key: K) -> int | None:
        """Next occurrence for ``key``, or ``None`` when it is an already-known duplicate.

        ``0`` is a VALID return — it is the first sighting of a key, and the most common
        outcome on a fresh import. Callers MUST test ``is None``, never truthiness:
        ``if not occ:`` would treat every new row as a duplicate and skip the whole
        file, which reads in the dedup tests as "everything deduped" rather than as a
        failure. ``test_allocate_returns_zero_for_a_fresh_key`` pins this.

        Increments the per-file tally on every call, including the ones that return an
        ordinal the caller then discards. Callers that reject a row **after** allocating
        must not compensate for that — see ``investment_import_service``, where counting
        only persisted rows would let a re-upload duplicate a row the DB already holds.
        """
        db_count, db_max_occ = self._existing_counts.get(key, (0, -1))
        self._seen[key] = self._seen.get(key, 0) + 1
        if self._seen[key] <= db_count:
            return None
        return db_max_occ + (self._seen[key] - db_count)
