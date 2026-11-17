# ADR-0006: A transaction's identity is a frozen four-field hash; multiplicity and provenance live beside it

**Status**: Accepted
**Date**: 2026-07-30 (decided) · **Revised**: 2026-11-17
**Scope**: PRD §F1, §F2, §F4 · `app/services/fingerprint.py` (`transaction_fingerprint`),
`app/services/merchant.py` (`normalize_merchant`), `app/services/occurrence.py` (`OccurrenceAllocator`),
`app/services/import_service.py`, `app/services/backup_import_service.py`,
`app/api/v1/transactions.py` (`update_transaction`), `app/schemas/transactions.py` (`TransactionUpdate`),
`app/models/transaction.py` (`occurrence`, `origin_fingerprint`)
**Supersedes / Related**: absorbs ADR-0007 · Related: ADR-0002, ADR-0004, ADR-0009, ADR-0011

## Decision

The [fingerprint](../../GLOSSARY.md#transactions--import) is
`sha256("\x1f".join(date_iso, amount_paise, normalized_merchant, account_id))` and is frozen. It says
what a row *is now*. Which of several identical rows it is lives in `occurrence`. Which statement line
it came from lives in `origin_fingerprint`, recomputable on any install from the origin's stored inputs.
Every user-visible field is editable, and an identity edit
recomputes the hash.

## Rules

**A. Identity**

1. **The formula is frozen**: lowercase 64-hex `sha256` of the UTF-8 `\x1f`-join of `date.isoformat()`,
   `str(amount_paise)`, `normalized_merchant` (rule 3), `str(account_id)`, in that order. — *Enforced by*: service ·
   *Locked by*: `tests/services/test_fingerprint.py::test_golden_vector`,
   `::test_no_boundary_collision_between_merchant_and_account`, `::test_format`
2. **`\x1f` cannot occur in a field**: `normalize_merchant` collapses whitespace as its last step, which deletes it; dates
   are `[0-9-]`, ints `[-0-9]`. — *Enforced by*: service ·
   *Locked by*: `test_fingerprint.py::test_separator_cannot_appear_in_a_normalized_merchant`
3. **Every writer normalizes through `normalize_merchant`**: the raw merchant lower-cased (`str.lower`),
   then put in Unicode NFC, then its whitespace runs collapsed to single spaces and trimmed; a missing or
   blank merchant hashes as `""`. So F1 and F2 agree on the same tuple, and canonically equivalent
   spellings (a precomposed `é` and `e` followed by a combining acute) are one merchant in every store
   keyed on the output. NFC follows lower-casing because lower-casing NFC text can leave it non-NFC (`J`
   + combining caron lower-cases to `j` + caron, which composes to `ǰ`). NFC, not NFKC: compatibility
   characters (ligatures, full-width letters) stay distinct (ADR-0011 Consequences). — *Enforced by*:
   service · *Locked by*: `test_fingerprint.py::test_f1_f2_dedup_parity`,
   `::test_f1_f2_dedup_parity_across_unicode_spellings`,
   `tests/services/test_merchant.py::test_canonically_equivalent_spellings_normalize_alike`,
   `::test_nfc_runs_after_lowercasing`, `::test_already_nfc_text_is_unchanged`,
   `tests/api/test_transactions.py::test_post_decomposed_spelling_of_an_existing_merchant_409`
3b. **A merchant longer than 512 characters, raw or normalized, is refused, never truncated.** Every
   merchant column is `String(512)`, and `normalize_merchant` can lengthen text: `İ` lower-cases to two
   code points, and NFC decomposes composition exclusions (U+0958, U+FB2C) into up to three. A truncated
   key is a different string from the one rule 3 defines, so it would change identity. Manual POST and
   PATCH, rules and aliases answer 422; a statement with a too-long row is refused with a 422 naming the
   row's position; a backup restore skips the row with a warning counted in `rows_rejected`. —
   *Enforced by*: schema + service · *Locked by*:
   `tests/api/test_transactions.py::test_post_merchant_normalizing_past_512_chars_422`,
   `tests/api/test_rules.py::test_merchant_field_normalizing_past_512_chars_422`,
   `tests/api/test_imports.py::test_import_refuses_a_statement_whose_merchant_is_too_long`,
   `tests/services/test_backup.py::test_restore_skips_a_row_whose_merchant_normalizes_past_512_chars`,
   `tests/models/test_models.py::test_every_merchant_column_is_merchant_max_chars_wide`
4. **Nothing positional, derived or install-local enters the payload**: no ordinal, row id, batch id,
   version or `transaction_type`. — *Enforced by*: service (four keyword-only args) · *Locked by*: rule 1's
   golden vector
5. **Every stored multi-field hash in the project joins with `\x1f`**, including
   `investment_transactions.fingerprint`. — *Enforced by*: service ·
   *Locked by*: `tests/services/test_investment_import_service.py::test_fingerprint_disambiguates_amount_from_units`

**B. Multiplicity**

6. **`occurrence` is `SMALLINT NOT NULL DEFAULT 0`** inside `UNIQUE(user_id, account_id, fingerprint,
   occurrence)`, named `uq_transactions_user_account_fingerprint`; the 409 matcher keys on that name. —
   *Enforced by*: DB · *Locked by*: `tests/test_migration_parity.py::test_migration_matches_models`
7. **Import dedup is a multiset difference** through `OccurrenceAllocator`: with `n_db` rows and `n_file`
   lines on a key, `max(0, n_file − n_db)` are staged. COUNT keys on `COALESCE(origin_fingerprint,
   fingerprint)`; the next ordinal comes from MAX(`occurrence`) on the current `fingerprint`. The
   statement prefetch is scoped to the file's fingerprint set; each importer owns its prefetch. —
   *Enforced by*: service · *Locked by*: `tests/services/test_occurrence.py::test_next_ordinal_comes_from_max_not_count`,
   `tests/services/test_import_service.py::test_two_identical_rows_in_one_statement_both_import`
8. **Only importers (statement, backup, investment CSV) write `occurrence > 0`.** Manual POST, transfer
   legs, F4a and the seeder write 0, so an identical manual row is a 409. — *Enforced by*: DB + route ·
   *Locked by*: `tests/api/test_transactions.py::test_post_duplicate_fingerprint_409`,
   `::test_transfer_dup_dest_leg_409_and_source_rolled_back`
9. **A deleted or cancelled row [re-stages](../../GLOSSARY.md#transactions--import) on re-upload; an
   edited one never does.** — *Enforced by*: service ·
   *Locked by*: `tests/api/test_imports.py::test_reimport_resurfaces_rows_missing_from_the_db`,
   `test_import_service.py::test_edited_row_is_not_restaged_on_reimport`

**C. Editability**

10. **PATCH accepts the seven user-visible fields — six columns plus labels**: `date`, `amount_paise`,
    `merchant_raw`, `account_id`, `transaction_type`, `category_id`, `labels`. Any other key is 422; an
    explicit `null` on a NOT NULL column is 422. — *Enforced by*: schema (`extra="forbid"`) ·
    *Locked by*: `test_transactions.py::test_patch_transaction_unknown_field_422`,
    `::test_patch_explicit_null_on_a_not_null_column_422s`, `::test_patch_server_managed_key_422`
11. **An identity edit recomputes `merchant_normalized` and `fingerprint`; `occurrence` resets to 0 only
    when the fingerprint actually changes**, and the unique index then decides (409). A re-cased merchant
    keeps its ordinal; a no-op PATCH is a no-op. — *Enforced by*: route + DB ·
    *Locked by*: `::test_patch_cosmetic_merchant_recasing_keeps_the_occurrence`,
    `::test_patch_amount_into_an_existing_fingerprint_409s`, `::test_patch_identity_field_to_the_same_value_is_a_no_op`
12. **Sign and type are validated as the merged post-patch pair by `sign_error` (ADR-0009) only when
    `transaction_type` or `amount_paise` is in the body.** — *Enforced by*: route ·
    *Locked by*: `::test_patch_sign_and_type_are_validated_as_a_merged_pair`,
    `::test_patch_labels_only_does_not_revalidate_an_untouched_sign`
13. **Category kind follows the post-patch type**: a kind flip with an incompatible category and no
    `category_id` in the body is 422, never a silent clear. — *Enforced by*: route ·
    *Locked by*: `::test_patch_type_kind_flip_without_a_compatible_category_422s`
14. **A changed `account_id` must be owned, live, non-investment and INR**, else 422. — *Enforced by*:
    route · *Locked by*: `::test_patch_account_id_cross_user_422s`
15. **A paired row (ADR-0002) rejects identity and type edits** (422); `category_id` and `labels` stay
    editable, and re-sending a stored value is a no-op. An unpaired row may change its type to and
    from `transfer` (ADR-0002 rule 7d). — *Enforced by*: route · *Locked by*:
    `::test_patch_a_live_transfer_leg_422s`, `::test_patch_an_unpaired_row_to_transfer`,
    `::test_patch_resending_a_live_legs_own_values_is_a_no_op`
16. **PATCH never writes `auto_category_id`**, and pending and confirmed rows are equally editable. —
    *Enforced by*: schema + route · *Locked by*: `::test_patch_identity_edit_leaves_auto_category_id_frozen`,
    `::test_patch_a_pending_row_identity_field`

**D. Provenance**

17. **Provenance: `origin_fingerprint`, plus the origin's inputs once edited.** `origin_fingerprint`
    (nullable `String(64)`, not unique, not indexed) is written by the statement importer at stage time,
    equal to `fingerprint`. The first identity edit of a row that has one (rule 11) freezes the pre-edit
    inputs in `origin_date`, `origin_amount_paise`, `origin_merchant_normalized` and `origin_account_id` —
    all four or none (CHECK `origin_inputs_all_or_none`); the account is stored because an account move
    is an identity edit, and `origin_account_id` is a composite same-user FK. So `origin_fingerprint` is
    always recomputable: from the origin inputs when set, else it equals `fingerprint`. PATCH never
    rewrites them once set; manual POST, transfers, F4a and the seeder write none of them. Backup export
    carries the origin inputs and never a hash (a hash bakes in the install-local `account_id`); restore
    recomputes `fingerprint` and `origin_fingerprint` on the target. — *Enforced by*: service + DB CHECK ·
    *Locked by*: `test_import_service.py::test_import_stamps_origin_fingerprint_equal_to_fingerprint`,
    `test_transactions.py::test_patch_never_touches_origin_fingerprint`, `::test_first_identity_edit_freezes_the_origin_inputs`,
    `tests/models/test_models.py::test_origin_inputs_are_all_or_none`,
    `tests/services/test_backup.py::test_round_trip_preserves_origin_fingerprint`

**E. Changing the formula or `normalize_merchant`**

18. **One revision rewrites every store keyed on the output**: `transactions.merchant_normalized`,
    `.origin_merchant_normalized`, `.fingerprint`, `.origin_fingerprint` (recomputed from the origin
    inputs, or from the row's own inputs where none are stored), `merchant_tag_map`,
    `merchant_label_map`, `merchant_alias.pattern` / `.canonical`, and the seeded alias rows. The one
    exception is the 2026-11-16 NFC step (rule 3), which shipped with no revision and no backfill: no
    install had imported a real statement, so every stored key was dev or test data, and NFC leaves
    ASCII and already-NFC text byte-identical, so those keys did not move. A dev row stored with
    decomposed text keeps its old key and fingerprint and may not dedup against a re-import; reset that
    database. Every later change, NFKC included, follows rules 18–21. —
    *Enforced by*: procedure · *Locked by*: none (no such migration yet)
19. **The migration inlines the hash and never imports `app.*`**, through one bidirectional helper that
    `upgrade()` and `downgrade()` both call (see `0025_fingerprint_separator_and_occurrence`). —
    *Enforced by*: tooling (ruff) · *Locked by*: ruff `TID251` banned-api on `app` in
    `backend/alembic/versions/ruff.toml`, run by pre-commit
20. **Key collapse merges, never updates**: sum `hit_count`, take the max `last_used_at`, OR `pinned`,
    delete the loser. Rows newly sharing a fingerprint get ascending `occurrence` ordered by `id`. —
    *Enforced by*: procedure · *Locked by*: none
21. **A recompute is reversible only if the old formula is computable from stored columns**; otherwise
    its docstring says one-way, and a conditional downgrade fails loudly. — *Enforced by*: migration ·
    *Locked by*: `tests/test_migration_parity.py::test_0025_downgrade_restores_the_old_key_and_is_reversible`

## Out of bounds

- Editing investment transactions; this ADR covers `transactions` only.
- Tombstones for deleted rows: re-staging a deleted row is the contract.
- A manual "record a genuine duplicate" switch (`allow_duplicate`): not in the PRD.

## Why

The original payload had no separator, so `amazon1`+`2` hashed like `amazon`+`12`, and a set mutated
inside the row loop dropped the second of two identical auto fares. Putting multiplicity in a column
keeps the hash meaning "what is this transaction", and lets the importer prove a second row is a separate
event by counting the file. A lone POST proves nothing, so it gets a 409 as a double-submit guard.

Users must be able to correct any field: no parser can tell a refund from a cashback credit. But an
edited hash looks like a deletion to the importer, which would re-stage the pre-edit line beside the
correction, with different fingerprints by construction, so F4 could never catch it. `origin_fingerprint`
keeps the edited row matched to its statement line.

## Alternatives rejected

- **Ordinal inside the hash** — makes identity positional and leaves twins differing by nothing visible.
- **`fingerprint_version` column or `v2:` prefix** — inert until the next change; the recompute is the
  mechanism.
- **Service-only dedup, no unique constraint** — loses the safety net and the 409.
- **Freeze `fingerprint` on edit** — trades a detectable duplicate for an undetectable one.
- **Delete and re-create as the correction path** — lossy and undiscoverable.
- **Identity edits for manual rows only** — denies the fix to the imported rows that need it.
- **Carry `occurrence` across a fingerprint change** — lands the row in a gap and defeats the 409.
- **Auto-clear the category on a kind flip** — silently destroys a user choice.

## Consequences

- A `normalize_merchant` change (RRN or auth-code stripping) is one migration with a known recipe
  (rules 18–21). Edited rows carry their origin inputs, so the recompute covers them too.
- Postgres v2 can use `ON CONFLICT (user_id, account_id, fingerprint, occurrence) DO NOTHING`;
  `occurrence.py` is the single place to change.
- Reopen if users need manual same-day duplicates, or if the UPI UTR becomes a reliable extra dedup
  signal; Indian spending apps list duplicates as a known issue.

## History

- 2026-07-30 — Accepted: `\x1f` separator and `occurrence` (migration 0025); the investment table
  followed (0027).
- 2026-08-03 — ADR-0007 accepted: the mutable set, identity recompute and `origin_fingerprint` (0028).
- 2026-08-12 — ADR-0009 amended the sign rule: `refund` is no longer a type.
- 2026-11-15 — 0007 merged in; the ADR is now the single transaction-identity record. Added: the
  golden-vector lock, every keyed store in the recompute (including aliases and seeds), backups carry
  `origin_fingerprint`, recompute of unedited origins, and the transfer guard firing only on a real
  type change.
- 2026-11-15 — origin inputs stored on the first identity edit; backups carry inputs, never hashes;
  revision `0041_portable_origin_inputs` nulled the origin of rows edited before it (dev data only).
- 2026-11-16 — `normalize_merchant` puts the lower-cased text in NFC before collapsing whitespace (rules 2, 3); shipped with no recompute revision and no backfill because no install had imported a real statement (rule 18).
- 2026-11-17 — Rule 15: an unpaired row may change its type to `transfer` (ADR-0002 rule 7d); only a paired row's type is locked.
- 2026-11-17 — Rule 3b: a merchant over 512 characters, raw or normalized, is refused at every write boundary, never truncated (C9). No formula change, so no recompute.
