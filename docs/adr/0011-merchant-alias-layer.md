# ADR-0011: Auto-tagging keys on a canonical merchant resolved through per-user aliases

**Status**: Accepted
**Date**: 2026-08-12 (decided) · **Revised**: 2026-11-15
**Scope**: PRD §F3, §F3a, §F8 (top merchants), §Success metrics · `app/models/merchant_alias.py`,
`app/services/merchant_alias.py` (`tokenize`, `build_resolver`, `AliasResolver`, `load_alias_resolver`),
`app/api/v1/rules.py` (`/rules/aliases`, `_alias_conflict`), `app/services/tag_service.py`,
`app/services/merchant_labels.py`, `app/services/provisioning.py` (seed dictionary),
`app/api/v1/dashboards.py` (`tagging_stats`, `top_merchants`)
**Supersedes / Related**: supersedes ADR-0008 · ADR-0006 (`normalize_merchant` is frozen) · ADR-0004
(what a teach writes) · ADR-0003 (`merchant_alias` is an owned table)

## Decision

F3 and F3a read on the [canonical merchant](../../GLOSSARY.md#transactions--import): the
[normalized merchant](../../GLOSSARY.md#transactions--import) passed once through the user's
[merchant aliases](../../GLOSSARY.md#transactions--import) by token-boundary, longest-pattern-wins
matching, at read time. `normalize_merchant` and the fingerprint never see a canonical. Every user starts
with the [seed dictionary](../../GLOSSARY.md#transactions--import).

```
merchant_raw -> normalize_merchant() -> merchant_normalized   [frozen; feeds the fingerprint]
                                              |
                                      resolve_alias() -> merchant_canonical   [F3/F3a key]
```

## Rules

1. **The alias layer never changes `normalize_merchant`**, whose formula (NFC included) is ADR-0006
   rule 3's, and the fingerprint never sees a canonical.
   — *Enforced by*: service · *Locked by*:
   `tests/services/test_import_service.py::test_fingerprint_is_byte_identical_with_full_alias_table_loaded`
2. **`merchant_alias(user_id, pattern, canonical, is_seeded)` is unique on `(user_id, pattern)`.**
   `pattern` and `canonical` are normalized by `normalize_merchant` (ADR-0006 rule 3, NFC included) at
   the `/rules/aliases` boundary, so canonically equivalent patterns collide on the unique key; blank
   after normalizing is 422. `pattern` is immutable; a PATCH changes `canonical` only and clears `is_seeded`. — *Enforced
   by*: DB UQ + service · *Locked by*: `tests/api/test_rules.py::test_create_alias_duplicate_pattern_422`,
   `::test_create_alias_blank_pattern_422`, `::test_patch_alias_clears_is_seeded`,
   `::test_create_alias_stores_nfc_pattern_and_canonical`,
   `::test_create_alias_decomposed_duplicate_pattern_422`
3. **A token is a maximal run of Unicode letters, combining marks and digits** (general categories
   L, M, N). Merchants and patterns reach the tokenizer already in NFC (ADR-0006 rule 3), so a
   precomposed `é` and `e` followed by a combining acute are one token. A pattern matches when its tokens are a contiguous run of the merchant's tokens, a UPI
   `@handle` excluded (rule 12): `ola` matches `upi ola 123`, not `chocolate hut`, and `स्विगी` is one
   token, not three. — *Enforced by*: service · *Locked by*:
   `tests/services/test_merchant_alias.py::test_canonical_does_not_match_disjoint_tokens`,
   `::test_tokenize_keeps_unicode_letters`,
   `::test_alias_matches_across_unicode_spellings_after_normalizing`
4. **The longest pattern wins**, ordered by token count DESC, then character length DESC, then pattern
   ASC. There is no user-editable priority. — *Enforced by*: service · *Locked by*:
   `tests/services/test_merchant_alias.py::test_load_alias_resolver_orders_longest_pattern_first`
5. **Resolution is single-pass and every canonical is a fixed point.** At most one alias applies. A
   create or PATCH is 422 unless its `canonical` resolves to itself under the prospective rule set and
   its `pattern` matches no existing canonical. — *Enforced by*: service · *Locked by*:
   `tests/services/test_merchant_alias.py::test_canonical_is_single_pass_no_chaining`,
   `tests/api/test_rules.py::test_create_alias_genuine_chain_still_422`
6. **Identity is the default**: a merchant no alias matches resolves to itself, unless it is a gateway
   handle (rule 12). An empty alias table therefore changes nothing but gateway handles. — *Enforced
   by*: service · *Locked by*:
   `tests/services/test_merchant_alias.py::test_canonical_identity_when_no_alias_matches`
7. **Zero tokens match nothing.** A zero-token pattern is 422 on create and skipped by the resolver; a
   zero-token merchant matches no alias and resolves to itself. — *Enforced by*: service · *Locked by*:
   `tests/api/test_rules.py::test_create_alias_zero_token_pattern_422`,
   `tests/services/test_merchant_alias.py::test_load_alias_resolver_skips_zero_token_rows`
8. **Every read site that ranks, prefills or groups by merchant resolves through a per-request
   `AliasResolver`**: import prefill, `list_candidates`, the `/rules` list, label prefill and top
   merchants. `resolver` is a required keyword. — *Enforced by*: service + `ty` · *Locked by*:
   `tests/services/test_import_service.py::test_import_prefills_via_alias`,
   `tests/api/test_rules.py::test_list_folds_aliased_merchants_into_one_group`,
   `tests/api/test_dashboards.py::test_top_merchants_groups_by_canonical` (planned)
9. **Aggregation happens on read**: a canonical's strength is the sum of `hit_count`, the max of
   `last_used` and the OR of `pinned` over its rows. What a teach writes is ADR-0004's. — *Enforced
   by*: service · *Locked by*:
   `tests/services/test_tag_service.py::test_aggregate_tag_rows_sums_across_aliased_merchants`
10. **Each user is seeded** at registration (and existing users by migration) with the dictionary's
    alias rows, identity rows included, and `merchant_tag_map` rows at `hit_count = 0`. Seeding skips
    any existing row, never merges or bumps one, and skips a category that is not a live spend
    category. — *Enforced by*: service + migration · *Locked by*:
    `tests/services/test_provisioning.py::test_provision_seed_merchant_dictionary_creates_aliases_and_map_rows`,
    `::test_seed_skips_existing_pinned_row`,
    `tests/test_migration_parity.py::test_merchant_dictionary_matches_migration_seed`
11. **A row is seeded iff `hit_count = 0 AND NOT pinned`**, at every read site: candidate confidence
    `"seeded"`, the `/rules` `seeded` flag and the `rules_count` exclusion. A pinned row is
    user-authored. — *Enforced by*: service · *Locked by*:
    `tests/api/test_imports_review.py::test_candidates_confidence_seeded_when_present_at_hit_count_zero`,
    `tests/api/test_dashboards.py::test_tagging_stats_rules_count_counts_pinned_seed_rows`,
    `::test_candidates_pinned_survives_seeded_hit_count_zero`
12. **A gateway handle resolves to `unknown via <psp>`, and an unknown-via canonical names no merchant.**
    Aliases match with every UPI `@handle` removed from both merchant and pattern, so
    `9876543210@airtel` never hits the `airtel` alias. When no alias matches, the rule judges the first
    UPI VPA (`local@handle`) in the merchant, and only its local part; other narration fields belong to
    the statement parser. The VPA is a gateway handle when its local part identifies nobody: six or
    more digits (a phone number or terminal id, optionally with a `-N` suffix), a gateway name alone or
    with digits (`razorpay`, `cashfree.123`), or a QR id (`paytmqr…`, `q123456789`, `bharatpe.9…`). A
    VPA whose local part identifies someone (`zomatoonline@paytm`, `razorpay.swiggy@icici`) is not a
    gateway handle. `<psp>` is the gateway or QR issuer the local part names, else the handle's app
    from the PSP list (`ybl` → `phonepe`), else the handle itself (`9876543210@hdfcbank` → `unknown via
    hdfcbank`). The PSP list and the id patterns are code beside the seed dictionary, not alias rows.
    An unknown-via canonical is never taught, prefilled, offered for past rows, listed or counted as a
    rule; a rule for one is 422, and `unknown via ` is a reserved canonical prefix. — *Enforced by*:
    service · *Locked by*:
    `tests/services/test_merchant_alias.py::test_psp_handle_resolves_to_unknown_via`,
    `::test_identifying_vpa_is_not_a_gateway_handle`, `::test_alias_never_matches_a_upi_handle`,
    `tests/services/test_tag_service.py::test_unknown_via_canonical_is_never_taught`,
    `tests/api/test_rules.py::test_create_category_rule_for_gateway_handle_422`
13. **`coverage_rate` = pre-tagged ÷ imported** over board rows with `source = "import"` and an
    auto-taggable type, `None` at zero; it measures the PRD's ≥80% pre-tag bar. — *Enforced by*:
    service · *Locked by*: `tests/api/test_dashboards.py::test_tagging_stats_coverage_excludes_manual_rows`

## Out of bounds

- Rule conditions on amount, account or date; regex patterns; a priority field.
- A second authoring surface (an inline "apply this rename to future imports" prompt): aliases are
  authored on `/settings/rules` only.
- Rewriting a committed transaction's category when an alias is added. A future import benefits at
  once; history is untouched (ADR-0004 owns "apply to past rows").
- Re-keying stored map rows onto the canonical (see Consequences).

## Why

`normalize_merchant` removes nothing variable, and Indian card and UPI descriptors embed an order id,
RRN, auth code or VPA. Measured on one real card statement (ADR-0008): 79 rows gave 49 distinct keys,
35 of them singletons that can never reach the confidence threshold of 3, and two cards' keys
intersected at zero. The learner was healthy; the key was wrong. Changing `normalize_merchant` would
force ADR-0006's identity recompute, so the fix is a layer after it.

Token boundaries make a contains-match safe (`ola` ≠ `chocolate`), and a wrong merge is irreversible
because summed `hit_count`s cannot be split, so precedence is derived from the data and chaining is
refused. ASCII-only tokens turned non-Latin descriptors into separators or into zero tokens, which would
match everything, and `str.isalnum` alone would split Indic words at every vowel sign. India has no public merchant taxonomy for a cold start, hence a hand-built seed
dictionary. A pin is a deliberate user choice, so it cannot read as "seeded". A PSP handle names the
gateway, not the shop, so naming it as the merchant would teach the wrong thing. A VPA's local part
is the only payee identity the alias layer can see; digits, a gateway name or a QR id identify nobody
there, so naming the gateway and refusing to learn is all that is honest.

## Alternatives rejected

- **Raw substring match** — `ola` merges into `chocolate hut`.
- **Exact-variant lists** — cannot match a reference it has not seen.
- **Widen `normalize_merchant`** — the frozen fingerprint key; triggers ADR-0006's recompute.
- **A rules engine** (ordered rules, boolean logic, amounts) — a normalisation problem, not a rules
  problem.
- **"Never matched by any pattern" no-chaining** — 422s every alias targeting a seeded brand, since
  each seeded canonical is also a pattern.

## Consequences

- `CONFIDENT_MIN` and `LABEL_PREFILL_MIN` now mean "this merchant 3×", not "this exact descriptor 3×".
- New teaches write the canonical (ADR-0004 rule 1); legacy raw rows are not re-keyed and fold at read
  time. The read-time aggregate stays permanently, since aliases are editable and an alias added later
  fans out over older rows.
- Until legacy rows are re-keyed, two descriptors under one canonical can each be pinned to a different
  category, falling back to the `hit_count` tiebreak. **Reopen** to re-key stored rows when a measured
  `coverage_rate` before/after justifies the migration.
- Rule 12 judges only the VPA because today's narrations carry an RRN, so no other field yields a
  stable key, and a phone or QR id "identifies nobody" only relative to that noise. **Reopen** rule 12
  when a bank parser emits a clean payee field: the parser's payee, not the narration, then decides
  identity.
- Merchants compare in NFC, not NFKC, so compatibility characters (ligatures such as `ﬁ`, full-width
  letters, `™`) stay distinct. **Reopen** for NFKC_Casefold if the real-PDF check finds ligatures or
  full-width text in a parser's `merchant_raw`: NFKC alone is not enough, because it runs after
  lower-casing and can bring capitals back (`™` → `TM`). Changing the formula then is an ADR-0006
  rule 18 recompute.

## History

- 2026-08-03 — ADR-0008 measured non-convergence and deferred a fix.
- 2026-08-12 — Accepted: alias layer, seed dictionary at `hit_count = 0`, `coverage_rate`; supersedes
  ADR-0008.
- 2026-11-15 — Rewritten to the rules-first template. Unicode tokens; seeded = `hit_count = 0 AND NOT
  pinned`; no-chaining stated as a fixed point; top merchants group by canonical; PSP handles resolve
  to "Unknown via <PSP>".
- 2026-11-16 — Tokens include combining marks; aliases ignore UPI handles; gateway-handle fallback defined (judges only the VPA's local part, applied after aliases, PSP naming order, reopen trigger); unknown-via canonicals never learn and the prefix is reserved.
- 2026-11-16 — `normalize_merchant` emits NFC (ADR-0006 rule 3): rule 1 reworded (ADR-0006 owns the formula; this layer still never changes it), rules 2 and 3 name the inherited NFC; NFKC_Casefold reopen trigger.
