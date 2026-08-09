# ADR-0006: The F4 dedup key — separator, occurrence ordinal, and the shared `normalize_merchant` contract

**Status**: Accepted
**Date**: 2026-07-30

## Problem

Three things, two of them live defects, one of them a debt the codebase has been carrying since the fingerprint was written.

**1. The payload had no separator.** `app/services/fingerprint.py` concatenated the four fields directly:

```python
payload = f"{txn_date.isoformat()}{amount_paise}{normalized_merchant}{account_id}"
```

Two of the three boundaries join variable-length values, so distinct inputs hashed identically:

- merchant `"amazon1"` + account `2` collided with merchant `"amazon"` + account `12`
- amount `-1` + merchant `"23x"` collided with amount `-12` + merchant `"3x"`

Only the `date | amount` boundary was safe, because `isoformat()` is fixed-width. The consequence is a cross-account false duplicate: a real transaction silently dropped on import.

**2. Two genuinely-distinct identical transactions were unstorable, and silently dropped.** `import_service` seeded a `set[str]` of existing fingerprints and then **mutated it inside the row loop**, so row N was compared against rows 1..N-1 of the *same* statement. Two auto rides at the same fare on one day — an ordinary Indian spending pattern, likewise two identical tolls or two same-price coffees at one outlet — imported as one row, counted as a duplicate, with no error.

Deleting the in-loop `add` would not have fixed it: the 3-column `UniqueConstraint(user_id, account_id, fingerprint)` independently forbade the second row, and `import_service` has no per-row savepoint to recover from the resulting `IntegrityError`. The same in-loop-mutation bug existed independently in `backup_import_service`.

**3. `normalize_merchant`'s output is the undocumented shared key for three stores.** `app/services/merchant.py` is four lines of code carrying a CHANGE HAZARD block, and `fingerprint.py` carried a matching `TODO(ADR)`, because that one function's output keys:

1. the F4 dedup fingerprint,
2. `merchant_tag_map` (F3 learned categories),
3. `merchant_label_map` (F3a learned labels).

A change to its rules — the planned Indian RRN / auth-code stripping — breaks dedup **and** both auto-tag stores at once, silently. Both files demanded an ADR before anyone touched it. Nobody had written down *how* such a migration would actually work, which is the part that matters.

## Decision

**The fingerprint is a pure 4-input identity function joined with `\x1f`; multiplicity lives in a separate `transactions.occurrence` column, never in the hash.**

**Five rules:**

1. **Canonical payload** — `sha256("\x1f".join(date_iso, amount_paise, normalized_merchant, account_id))`. Field order and count are frozen. The separator is `\x1f` (ASCII Unit Separator) because it is provably absent from every field: `date.isoformat()` is `[0-9-]`, both ints are `[-0-9]`, and `normalize_merchant`'s `" ".join(raw.lower().split())` deletes it, since `'\x1f'.isspace()` is `True`.
2. **The hash carries identity only.** Nothing positional, derived, or install-local ever enters the payload. A fingerprint answers *"what is this transaction?"*
3. **Multiplicity lives in `transactions.occurrence`** (SMALLINT NOT NULL DEFAULT 0), inside the unique constraint, which widens to `(user_id, account_id, fingerprint, occurrence)`. It answers *"which of the N identical ones is this?"* Dedup therefore becomes a per-fingerprint **multiset difference**: the DB holds `n_db` rows, the file yields `n_file`, and `max(0, n_file - n_db)` are staged. Assignment tracks `MAX(occurrence)`, not `COUNT`, because occurrences can be gapped once a user deletes one of a pair.
4. **Manual entry keeps its 409; only the import paths increment `occurrence`.** This asymmetry is deliberate epistemics, not an inconsistency: the importer can count the file's multiset and therefore *prove* the second row is a distinct event, while a lone `POST` proves nothing, so it asks. The 409 is a real feature (double-submit protection). Manual entry, both transfer legs, and the demo seeder all leave `occurrence` at its default 0, so they collide exactly as before and need no code change.
5. **No version discriminator.** The recompute migration is the mechanism; §Recompute procedure below is its executable form.

**What not to do:**

- Do not add fields to the hashed payload. `PRD.md` §F4 pins the field list; changing it is an ADR-level decision, not a code edit.
- Do not put an ordinal, a row id, a batch id, or anything else positional in the payload.
- Do not rename `uq_transactions_user_account_fingerprint`. The Postgres branch of `core/db_errors.py:is_unique_violation` matches on the index *name*, so keeping it means `api/v1/transactions._is_fingerprint_conflict` and its 409 mapping need no edit. Its SQLite branch is a subset test over `table.col` tokens, so the extra column is inert there too.
- Do not "fix" re-staging. Re-upload re-surfacing a row the user deleted is the documented contract (`import_service`'s module docstring; `test_reimport_resurfaces_rows_missing_from_the_db`), and it applies to duplicate groups exactly as it does to singletons. Suppressing it would need soft-delete tombstones.

**Implementation approach:** migration `0025_fingerprint_separator_and_occurrence` — one revision, two labelled steps (batch schema swap, then in-Python recompute). The backfill is lossless because the bug plus the old constraint made duplicates unstorable, so every pre-existing row is occurrence 0.

---

## Recompute procedure

**This section is the debt discharge.** It is what `fingerprint.py`'s `TODO(ADR)` and `merchant.py`'s CHANGE HAZARD block were asking for, and it is what makes the *next* formula change cheap to execute — not a dormant version column.

**a. Reference implementation.** `alembic/versions/0025_fingerprint_separator_and_occurrence.py`. Clone its `_recompute(separator)` helper: one bidirectional function both `upgrade()` and `downgrade()` call, mirroring `0018_recolor_seed_categories`'s `_remap(old, new)` shape. **Inline the hash; never import `app.services.fingerprint`.** A migration must be frozen against the formula as it stood at its own revision, or a future formula change silently rewrites history. No migration in this tree imports app code.

**b. A `normalize_merchant` change is one revision touching four columns.** It must rewrite, together:

- `transactions.merchant_normalized`
- `transactions.fingerprint`
- `merchant_tag_map.merchant_normalized`
- `merchant_label_map.merchant_normalized`

Splitting them across revisions leaves an intermediate state where dedup and auto-tag disagree.

**c. Key collapse is the trap.** `merchant_tag_map` carries `UNIQUE (user_id, merchant_normalized, category_id)` and `merchant_label_map` its sibling. RRN / auth-code stripping *will* collapse two old keys onto one new key — that is the point of it. So the backfill must **merge**, not update:

- sum `hit_count`
- take the max `last_used_at`
- OR the `pinned` flag
- then delete the loser

A naive `UPDATE` raises `IntegrityError`. A naive `UPDATE OR IGNORE` silently discards learned history, which is worse — the user's auto-tag memory quietly degrades with no error.

**d. `occurrence` usually needs no attention** on a normalization change — *unless* the collapse makes two previously-distinct rows share a fingerprint. Then assign ascending occurrences within each new group, ordered by `id`.

**e. Reversibility rule.** A recompute is reversible iff the old formula is computable from still-stored columns. If a change drops an input column, the migration is one-way and its docstring must say so. `0025` is reversible in its schema half always, and in its data half only when no `(user_id, account_id, base-tuple)` group holds more than one row — and it fails *loudly* otherwise, by recomputing before re-narrowing the constraint.

---

## Trade-offs

**Benefits:**

- The fingerprint stays the pure 4-input function `PRD.md` §F4 names, so the signature is unchanged and ~20 existing call sites (including all nine `**_BASE` sites in `test_fingerprint.py`) needed no edit.
- All five existing 409 tests pass unmodified — the double-submit guard is provably preserved.
- `SELECT occurrence` answers "why do these two identical rows differ?" from the row itself.
- Multiset semantics are invariant to *which* duplicate a user deleted.
- Postgres v2 can express a truthful `INSERT ... ON CONFLICT (user_id, account_id, fingerprint, occurrence) DO NOTHING`, replacing the "no per-row savepoint" note — which now lives in exactly one place, [`app/services/occurrence.py`](../../backend/app/services/occurrence.py), rather than in one of the three importers. That module is the ordinal-assignment rule's single home (A2.6/A3.1): all three importers had it character-identical, and the drift recorded above is what it prevents recurring. Each importer still owns its own prefetch `SELECT` — the key tuple, the scope window and investment's `fingerprint IS NOT NULL` filter are per-source decisions, not parameters of the algorithm. **The cutover has one grep target.**
- Four of the six `transaction_fingerprint` call sites needed no change at all.

**Drawbacks:** one extra column; one `batch_alter_table` rebuild of `transactions`; the downgrade's data half is conditional.

**Mitigation:** the batch rebuild is the proven path on this table (migrations 0009 / 0020 — the copy re-validates the self-referential composite FK against the pre-existing `uq_transactions_id_user` target, and `env.py` sets `PRAGMA foreign_keys=OFF` for the copy). Migration 0005's `foreign key mismatch` trap was a schema-shape error, from adding a composite FK and its target in one batch, which is why 0005 notes the pragma could not save it — not applicable here. The conditional downgrade fails loudly by op ordering rather than merging two real transactions. `test_0025_batch_rebuild_preserves_the_partial_index_predicate` covers the one thing `test_migration_matches_models` cannot see, since it does not compare partial-index predicates.

## Alternatives Considered

**Alternative 1 — Occurrence ordinal inside the hash payload**: rejected. It makes the identity hash carry a positional value, contradicting `PRD.md` §F4's pinned field list and `CLAUDE.md`'s "don't add fields without confirming"; it forces a 5th required kwarg through ~20 call sites; it requires the dedup prefetch to re-read and re-hash `date`/`amount`/`merchant` for every existing row instead of grouping on the stored hash; and it leaves two identical rows differing by nothing visible in the row. Note it is **not** worse on the re-upload invariant — both designs re-stage exactly one row after the user deletes one of two duplicates, because all members of a fingerprint group are identical on the four hashed fields, making "which one survived" unobservable either way.

**Alternative 2 — `v2:<hex>` value prefix**: rejected. Breaks the `String(64)` column width and the 64-hex format test *today*, for a benefit paid only at the next formula change, and pollutes every stored value and log line.

**Alternative 3 — `fingerprint_version` SMALLINT column**: rejected. Inert on its own. Firefly III's actual technique (which the idea was borrowed from) is dedupping against the *union* of both formulas during transition, which requires computing both hashes at every call site — that is the real cost, and it is incurred at the next change whether or not the column exists now. A column with exactly one live value is the speculative config knob `CLAUDE.md` §2 forbids.

**Alternative 4 — Drop the DB unique constraint, dedup in the service only**: rejected. Loses the safety net, breaks the 409 contract, and contradicts `PRD.md` §F4's unique-index requirement.

**Alternative 5 — Separator `\n`**: rejected, narrowly. Equally provably absent, but `\x1f` cannot be mistaken for a line break in a dumped or logged payload, and it is the character literally defined for this job.

**Alternative 6 — Soft-delete tombstones so a deleted duplicate never re-stages**: rejected as out of scope. It contradicts `import_service`'s documented re-upload contract, which deliberately re-surfaces discarded rows, and would touch every read path.

## Consequences

- **Follow-up, if users hit it:** an explicit `allow_duplicate: bool` on `TransactionCreate`, so manual entry can record a genuine same-day duplicate. Deliberately *not* silent auto-increment, which would destroy the double-submit guard. Not in the PRD, so out of scope here.
- **Two sibling defects, filed separately** (`CLAUDE.md` §3 — adjacent cleanups are their own commit). `investment_import_service` carries both of this ADR's defects independently, on a different table with a different hash function and a *nullable* fingerprint: the same in-loop `existing_fps.add(fp)`, and a worse-formed separator bug where `amount_native_paise | units_scaled` joins two variable-length integers (amount=1/units=23 hashes identically to amount=12/units=3). This ADR states the separator rule as a **project-wide convention** so each is a small migration clone plus a two-line formula edit.
  - **Both discharged** in migration `0027_investment_fingerprint_separator_and_occurrence` — `investment_transactions.occurrence`, the unique **index** widened to `(user_id, instrument_id, fingerprint, occurrence)`, and a `\x1f` recompute over `fingerprint IS NOT NULL` rows only (manual rows stay NULL, keeping the backstop inert for them). No ADR-0007: this applies the convention above to a second table and decides nothing new. Two differences from 0025 worth recording — the 3-column key was a `unique=True` **Index**, not a `UniqueConstraint`, so `op.drop_index` / `op.create_index` replaced the batch rebuild and sidestepped the self-referential-composite-FK hazard entirely; and rule 4's "manual entry keeps its 409" has **no analogue** on the investment table, because manual `POST /investment-transactions` writes `fingerprint = NULL` and so never participates in dedup at all.
- `PRD.md` §F4 amended (formula, unique-index scope, and the skip-vs-multiset wording).
- **A second stored hash exists as of [ADR-0007](0007-transaction-field-editability.md).** `transactions.origin_fingerprint` holds the value this formula produced at *import* time, so that a user edit to an identity column does not read as a deletion to the importer and re-stage the pre-edit row. The five rules above are unchanged and no field is added to the payload — it is the same function's earlier output, stored beside the current one, and the file-dedup prefetches key on `COALESCE(origin_fingerprint, fingerprint)`. Read ADR-0007 rule 9 before touching either column.
