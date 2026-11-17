# ADR-0012: Categories are two levels, capped; the hierarchy only groups, and archive is soft

**Status**: Accepted
**Date**: 2026-08-14 (decided) · **Revised**: 2026-11-17
**Scope**: PRD §F5, §F8, §Verification 16 · `app/models/category.py` (`Category.parent_id`,
`Category.subcategories`), `app/api/v1/categories.py`, `app/services/provisioning.py`
(`_DEFAULT_SPEND_TAXONOMY`, `_DEFAULT_INCOME_TAXONOMY`), `app/services/category_service.py`
(`FALLBACK_CATEGORY_NAME`),
`app/services/category_colors.py` (`CATEGORY_PALETTE`), migrations `0046_root_category_color` (`ck_categories_root_has_color`) and `0047_retire_card_bill_category`, `app/api/v1/transactions.py` (`list_transactions`),
`app/api/v1/dashboards.py`, `app/services/backup_import_service.py`, `app/services/guest_service.py`, `app/services/user_teardown.py`,
`frontend/lib/categories.ts`
**Supersedes / Related**: ADR-0003 (composite FK), ADR-0004 (bulk teach), ADR-0001

## Decision

A [category](../../GLOSSARY.md#categories) is a parent (`parent_id IS NULL`) or a subcategory of a
parent of the same kind; depth is capped at two and both levels are taggable. Rollup, colour
inheritance and the archive cascade each reach exactly one hop. A category is never hard-deleted
except by whole-user teardown.

## Rules

1. **`parent_id` is a nullable composite same-user FK** `(parent_id, user_id) → categories(id, user_id)`
   with `ON DELETE NO ACTION`, indexed on `(user_id, parent_id)`. — *Enforced by*: DB constraint ·
   *Locked by*: `tests/api/test_auth_isolation.py` composite-FK case (planned)
2. **Depth is at most two.** Creating under a subcategory, giving a parent to a category with children,
   or self-parenting is a 422. — *Enforced by*: service (router) ·
   *Locked by*: `tests/api/test_categories.py::test_create_cannot_nest_3_levels_422`,
   `::test_patch_gives_parent_to_category_with_children_422`, `::test_patch_reparenting_and_validation`
3. **A subcategory's kind equals its parent's** (422 otherwise), and `kind` is immutable after create.
   — *Enforced by*: service + schema · *Locked by*: `tests/api/test_categories.py::test_create_subcategory_kind_mismatch_422`,
   `::test_patch_kind_rejected_422`
4. **The parent is the caller's own active category**; a foreign or archived parent is a 422 (ADR-0003
   rule 9). — *Enforced by*: service + DB (rule 1) ·
   *Locked by*: `tests/api/test_categories.py::test_create_subcategory_foreign_user_parent_422`
5. **Both levels are taggable**; there is no leaf-only rule. Spend tagged on a parent is its
   [direct spend](../../GLOSSARY.md#categories). — *Enforced by*: service ·
   *Locked by*: `tests/api/test_transactions.py::test_list_filter_by_parent_category_includes_subcategories`
6. **[Rollup](../../GLOSSARY.md#categories) is one hop and follows `parent_id` whatever the archive
   state.** `?category_id=X` matches `id = X OR parent_id = X`, scoped to the caller. Every aggregate is a
   flat `GROUP BY` plus at most one parent join, with no recursive query. Spend-by-category rows carry
   `parent_id`, so an archived subcategory's history still rolls up under its parent. — *Enforced by*:
   query + frontend · *Locked by*: `tests/api/test_dashboards.py::test_spend_by_category_rows_carry_their_parent_even_when_archived`,
   `tests/api/test_transactions.py::test_list_filter_by_category_cross_user_parent_returns_empty_not_other_users_rows`,
   `tests/api/test_dashboards.py::test_category_trend_drilldown_sums_to_parent_and_keeps_archived_children`,
   `frontend/lib/categories.test.ts` "rolls an archived subcategory up under its parent via the row's parent_id"
7. **Every root has a colour, a subcategory inherits it one hop, and the server resolves what each
   category renders as.** A root's `color` is never `NULL` (CHECK `ck_categories_root_has_color`). A
   create that omits it, a subcategory promoted to root without one, and a root restored from a backup
   without one each get the `CATEGORY_PALETTE` slot (`app/services/category_colors.py`) held by the
   fewest active roots of the user's of the same kind, the earliest slot on a tie; an explicit `null` on
   a root is a 422. A subcategory created under a parent defaults to `NULL` and inherits; an explicit
   colour is still allowed. Every seeded subcategory is `NULL`. Each category read carries
   `display_color`, the category's own colour or else its parent's; every view that lists categories or
   compares them renders it, so a family reads as one colour, and the frontend never derives it. In one
   family's stacked trend drilldown, an inheriting subcategory's series colour is the palette slot
   `k + 1` places after its parent's, cyclically over the other 13 slots, where `k` is its 0-based
   position by id among its current siblings, archived ones included; the parent's own slot is never
   used, because the drilldown's direct row keeps it. If the parent's colour is not a palette slot,
   counting starts at slot 0. The trend endpoint sends that colour on each series. Neighbouring palette
   slots, the last and first included, clear the dataviz normal-vision floor on the light and dark chart
   surfaces, so one hex serves both themes; a stack ordered by spend rank can put non-neighbours side by
   side and relies on its legend, labels and table view, as the root level does. Archiving never
   repaints a family; reparenting or promoting a subcategory shifts every later inheriting sibling of the
   family it leaves and the family it joins. — *Enforced by*: service + seed + CHECK ·
   *Locked by*: `tests/api/test_categories.py::test_create_root_without_color_gets_the_least_used_slot_of_its_kind`,
   `::test_root_color_cannot_be_null_422`, `::test_promoting_an_inheriting_subcategory_assigns_a_root_color`,
   `::test_promoting_with_an_explicit_null_color_422`, `::test_list_carries_the_server_resolved_display_color`,
   `tests/services/test_category_colors.py::test_next_root_color_takes_the_least_used_slot_earliest_first`,
   `::test_inheriting_siblings_take_the_palette_slots_after_their_parents`,
   `::test_palette_neighbours_clear_the_normal_vision_floor`,
   `tests/services/test_backup.py::test_restore_gives_every_root_without_a_color_a_palette_color`,
   `tests/models/test_models.py::test_a_root_category_needs_a_color`,
   `tests/test_migration_parity.py::test_root_color_migration_adds_the_check_and_keeps_rows`,
   `::test_root_color_migration_refuses_a_colourless_root`, `::test_seed_categories_have_default_color`,
   `tests/api/test_dashboards.py::test_category_trend_colours_its_series_on_the_server`,
   `::test_spend_by_category_rows_carry_their_display_colors`,
   `frontend/lib/category-trend.test.ts` "colours every drill series with the colour the server sent",
   `frontend/components/dashboard/spend-by-category.test.tsx` "draws view 2 in the rows' display colours,
   an archived subcategory included", `frontend/app/settings/categories/categories-manager.test.tsx`
   "creates a new root category and invalidates caches", "creates a subcategory under an existing parent"
8. **Each kind's [fallback category](../../GLOSSARY.md#categories) "Other" is an unarchivable root.**
   Archiving it, or giving it a parent, is a 422, so the fallback always exists. Renaming it is a 422
   too, because the import fallback finds it by name and kind. — *Enforced by*: service ·
   *Locked by*: `tests/api/test_categories.py::test_fallback_category_cannot_be_archived_or_parented`,
   `tests/test_migration_parity.py::test_fallback_migration_leaves_one_active_root_other_per_kind`
9. **[Archive](../../GLOSSARY.md#categories) is soft, one-way, and cascades one level forward, with no
   exceptions.** Archiving a parent stamps the same naive-UTC `archived_at` on it and all its active
   subcategories in one request; archiving a subcategory leaves the parent alone; no API clears
   `archived_at`, and PATCH on an archived row is a 404. — *Enforced by*: service ·
   *Locked by*: `tests/api/test_categories.py::test_delete_parent_cascades_archive`,
   `::test_patch_archived_404`, `::test_archive_child_leaves_parent_active`
10. **Archiving offers to reassign the category's transactions** to another live category of the same
   kind. The reassignment is one bulk gesture and teaches once per merchant (ADR-0004 rule 4). It also
   rewrites a linked leg's stored pre-link category (ADR-0002 rule 9). — *Enforced by*: service ·
   *Locked by*: `tests/api/test_categories.py::test_archive_with_reassign_moves_transactions`,
   `::test_archive_with_reassign_rewrites_a_pre_link_category`,
   `frontend/app/settings/categories/categories-manager.test.tsx` ("archives with reassign and moves the
   transactions to the picked category")
11. **Whole-user teardown is the only hard delete** (guest sandbox expiry and account deletion, ADR-0003); it deletes
   subcategories before parents. No user-facing path hard-deletes a category, and `subcategories` carries
   no ORM `delete-orphan` cascade, so promoting a child to root is a plain update. — *Enforced by*: DB
   (`NO ACTION`) + ORM config · *Locked by*: `tests/api/test_categories.py::test_subcategories_relationship_has_no_delete_orphan_cascade`,
   `::test_patch_promote_to_root_preserves_row_and_transaction_link`
12. **Tree endpoints never drop a row**: a subcategory whose parent is absent from the result set is
   returned as a root, by both `list_categories?tree=true` and `buildCategoryTree`. — *Enforced by*:
   service + frontend · *Locked by*: `tests/api/test_categories.py::test_list_tree_active_child_of_archived_parent_promoted_to_root`
13. **Backup carries `parent_name`, not `parent_id`.** Restore builds parents first; an unresolvable or
   non-root `parent_name` flattens the row to a root and adds a `warnings` entry. — *Enforced by*: service ·
   *Locked by*: `tests/services/test_backup.py::test_backup_restore_hierarchical_categories_roundtrip`,
   `::test_backup_with_a_3_level_category_chain_flattens_the_grandchild_with_a_warning`
14. **The default taxonomy is written down only in `provisioning.py`** (plus each migration's frozen
   copy). The PRD points there; tests that assert the taxonomy read those constants. — *Enforced by*:
   convention · *Locked by*: `tests/test_migration_parity.py::test_provisioning_matches_migration_seed`

## Out of bounds

- A third level. It breaks one-hop rollup, colour, cascade and both tree builders, so it supersedes
  this ADR rather than extending it.
- Un-archive. Archive is one-way; reassignment (rule 10) is the recovery path.
- Many-to-many grouping: that is what [labels](../../GLOSSARY.md#categories) are for.

## Why

A flat list forces a bad choice once the seed dictionary (ADR-0011) tags real merchants. Either
"Shopping" swallows everything, or 50 siblings flood one picker and one pie chart. A grouping level
fixes that, but three things break if it is modelled carelessly. `transactions.category_id` must never
dangle, hence soft archive. The fallback lookup uses name plus kind, hence a shared kind. Every
aggregate is a flat `GROUP BY`, hence a depth cap that keeps rollup one hop on both dialects (ADR-0001).

An exempt fallback child made the cascade leave one live row behind, which the tree then showed as a
stray root. Making the fallback a root that can never be archived keeps the cascade total.

Lightness-only sibling shades cannot be told apart: the dark-mode lightness band (OKLCH L 0.48–0.67) holds at most two steps that clear the dataviz normal-vision floor, and seeded families have up to nine members. Where families mix, every member renders in the family hue, so a family reads as one colour and the label tells siblings apart. The stacked trend drilldown is the one place siblings sit edge to edge, so only it rotates the already-validated palette. Spend and income never share a chart, so each kind draws from its own pool. Handing out the least-used slot keeps a full palette spreading instead of piling onto one slot. A CHECK makes the root colour hold for every writer, including ones that bypass the API.

## Alternatives rejected

- **Unlimited depth with a recursive CTE** — every aggregate turns recursive, on two dialects.
- **A separate `category_groups` table** — a second isolation, archive and export contract for one
  self-FK's worth of meaning.
- **Labels as groups** — labels are many-to-many on transactions; a group is 1:N on categories.
- **Leaf-only tagging** — needs a migration of live rows and leaves the fallback nowhere to point.
- **Fallback exempt from the cascade** — the cascade has an exception, and the PRD's "whole family
  disappears" is false.
- **`ON DELETE CASCADE` on `parent_id`** — a hard parent delete silently takes its children.
- **Lightness-only (or hue-nudged) family shades** — the dark band fits two separable steps; seeded families have up to nine members.
- **A 422 for a root created without a colour** — restore and promotion still need a server-assigned colour, so the API would have two rules for one fact.
- **A series colour on every category read** — only the stacked trend drilldown needs it, and that endpoint already has its own payload.
- **First free slot with a modulo fallback** — once the palette is full it hands out the same slot every time.
- **A palette slot index on the wire** — a user may pick any hex, and every palette hex already serves both themes.

## Consequences

- A parent's total is direct spend plus its subcategories, so every view that shows a parent must
  render the direct part (PRD §Verification 16).
- Depth is enforced in the router; every other writer (provisioning, migrations, restore, guest
  seeding) must be unable to build a third level by construction.
- Every writer that can make a root (create, promotion, restore) assigns a colour, and the CHECK refuses one that does not; a dev database holding a colourless root fails the 0046 upgrade until it is fixed.
- Spend-by-category rows carry their own and their parent's display colour, so an archived subcategory keeps its family hue in view 2.
- Reopen if users need a third level or un-archive.

## History

- 2026-08-14 — Accepted: two levels, one-hop rollup, colour inheritance and soft cascade (migrations
  0033–0035 reparented the flat defaults without renaming any).
- 2026-11-15 — Rewritten to the rules-first template. The fallback became an unarchivable root and the
  cascade total. Teardown is the only hard delete, and `parent_id` became a composite same-user FK with
  `NO ACTION`. A new subcategory inherits its colour. Rollup follows `parent_id` for archived
  subcategories too. Added reassign-on-archive.
- 2026-11-15 — Amended rule 8: renaming the fallback is a 422 too, since a renamed "Other" is no
  longer found by the name-and-kind lookup.
- 2026-11-16 — Rule 6's Locked-by gains the category-trend drilldown test: view 6 is the second aggregate that must roll archived subcategories into their parent.
- 2026-11-16 — Rule 10: archive-with-reassign also rewrites a linked leg's stored pre-link category.
- 2026-11-16 — Rule 6's Locked-by moves from the deleted `/hierarchical-spend` to `spend-by-category`'s
  `parent_id` and `parent_name`, which view 2 rolls up from.
- 2026-11-17 — Rule 7 rewritten: every root has a colour (CHECK ck_categories_root_has_color; the server assigns the least-used palette slot of the root's kind; null on a root is a 422), reads carry a server-resolved display_color, and the stacked trend drilldown rotates the palette from the parent's slot instead of lightness-only shades. Palette neighbours clear the dataviz floor; a rank-ordered stack relies on its legend, labels and table view.
- 2026-11-17 — The seeded spend subcategory "Credit Card Payments" is retired: provisioning drops it and migration `0047_retire_card_bill_category` archives every user's seeded row, so no default category files a card bill as spend (ADR-0002 rule 7b). Rows already in it keep it; tag rules pointing at it stop prefilling (ADR-0004 rule 8).
