# ADR-0012: Two-level category hierarchy — depth cap, rollup semantics, and the soft-delete contract

**Status**: Accepted
**Date**: 2026-08-14

## Problem

PRD §F5 shipped categories as a flat list of 17 defaults, and said so explicitly: *"Flat
list, no hierarchy in v1."* That was the right call while the taxonomy was small. It stops
being the right call once the seed dictionary ([ADR-0011](0011-merchant-alias-layer.md))
starts pre-tagging real Indian merchants, because a flat list forces one of two bad
answers:

- **Keep it coarse** — "Shopping" absorbs Myntra, Croma, Amazon and IKEA alike, and the
  §F8 by-category chart's largest slice is the least informative one.
- **Go fine-grained and stay flat** — 50 sibling categories in one picker and one pie
  chart. The picker becomes unusable, the chart becomes a colour-lottery, and the §F8
  "monthly spend by category" view stops answering *"where did the money go"* at a glance.

The second failure is the one that was actually reached: the merchant dictionary names
things like *Online Food Delivery*, *Ride-Hailing & Taxis*, *Digital Subscriptions &
Streaming*. Those are the useful buckets, and they need a grouping level above them.

A grouping level is not free. Three things in this codebase break in non-obvious ways if
the hierarchy is modelled carelessly:

1. **`transactions.category_id` is a plain FK with no `ON DELETE` clause.** Categories are
   soft-deleted via `archived_at` precisely so that FK never dangles
   (`app/models/category.py`). Any cascade that *hard*-deletes a category takes live
   transaction references with it.
2. **`kind` participates in the active-name unique index** (`uq_categories_active_user_name`
   on `user_id, name, kind`), and the F3/F4a "Other" default is looked up by name — so a
   parent/child pair that disagrees on `kind` makes that lookup ambiguous in a new way.
3. **Every aggregate is a flat `GROUP BY category_id`** across `dashboards`, the F8
   surface, and the `/spending` tag views. An unbounded tree turns each of those into a
   recursive query, on both SQLite v1 and Postgres v2 ([ADR-0001](0001-sqlite-postgres-portability.md)).

## Decision

**Categories get exactly two levels — a nullable self-FK `parent_id`, depth capped at 2 —
and the hierarchy is a *grouping* device, not a new identity for a transaction.**

**The rules:**

- `parent_id IS NULL` is a **parent**. A row pointing at one is a **subcategory**. A
  subcategory can never itself be a parent: a create or PATCH that would nest three deep
  is a **422**, enforced in `app/api/v1/categories.py`. Depth is a router concern, not a
  schema one — the schema layer carries no parent validation.
- **A subcategory's `kind` must equal its parent's.** Mismatch is a 422. `kind` remains
  immutable after create.
- **Both levels are taggable.** A transaction may point at a parent or at a subcategory.
  There is no leaf-only rule, so every pre-hierarchy row and the F3/F4a "Other" default
  stay valid with no data migration.
- **Rollup is exactly one hop, at the query boundary.** `GET /transactions?category_id=X`
  matches `Category.id == X OR Category.parent_id == X`, scoped to the caller
  ([ADR-0003](0003-multi-user-auth.md)). No recursive CTE, no adjacency-walk helper, no
  materialised path. Two levels is what makes the one-hop form total.
- **`color` inherits one hop.** A subcategory with `color IS NULL` renders in its parent's
  hue; siblings are separated by a derived shade, never an unrelated colour. Every
  *seeded* subcategory is `NULL` — a seeded subcategory carrying its own hex is drift
  between `provisioning.py` and the migration, and `tests/test_migration_parity.py` exists
  to fail on it.
- **Archiving cascades one level, forward only, and stays soft.** Archiving a parent
  stamps `archived_at` on it and on its active children in the same request, in Python.
  Archiving a child leaves the parent alone. The timestamp is **naive UTC**
  ([ADR-0001](0001-sqlite-postgres-portability.md) rule 5) on every row the cascade
  touches.
- **Backup carries `parent_name`, not `parent_id`.** The link travels as a label so it
  survives a restore into a database where ids differ (§F10). A `parent_name` that cannot
  be resolved flattens the row to a root — but says so in `warnings`.
- **An existing user is migrated by reparenting, never renaming.** Migration 0034 hangs
  the old flat defaults under the new parents and adds the new subcategories. It renames
  nothing and deletes nothing, so no transaction changes category.

**What not to do:**

- **Do not add an ORM `cascade="all, delete-orphan"` to the `subcategories` relationship.**
  It converts *reparenting a child to root* — a legitimate, user-facing PATCH — into a row
  deletion, and takes that category's transaction references with it. The soft-delete
  model means no code path should ever hard-delete a category; the relationship must not
  quietly add one.
- **Do not deepen the tree.** A third level is not a small change: it invalidates the
  one-hop rollup in `transactions.py`, the one-hop colour inheritance, the one-level
  archive cascade, and the two-pass tree builders on both sides of the API. If a third
  level is ever wanted, it supersedes this ADR rather than extending it.
- **Do not enumerate the taxonomy anywhere but `provisioning.py`.** The names live in
  `_DEFAULT_SPEND_TAXONOMY` / `_DEFAULT_INCOME_TAXONOMY`. Migration 0034 necessarily holds
  a frozen copy — that is what a migration is — but prose, tests, and the frontend must
  read, not restate. Duplicated taxonomy prose is the defect class `AGENTS.md` §Surgical
  changes names.
- **Do not let a tree endpoint silently drop a row.** A subcategory whose parent is absent
  from the result set (archived parent, or filtered out by `kind`) must be returned as a
  root, not omitted. Both tree builders — `api/v1/categories.py` and
  `frontend/lib/categories.ts` — must agree on that.

**Implementation approach:**

- One nullable self-FK plus a `(user_id, parent_id)` index (migration 0033), a data-only
  reparent-and-seed migration (0034), and a merchant-dictionary re-point (0035).
- Validation lives in the router next to the 404/422 tenant rules it shares state with.
- The frontend mirrors the backend cap: `buildCategoryTree` promotes orphans to roots and
  assumes depth 2.

---

## Trade-offs

**Benefits:** The §F8 chart groups into ~10 legible slices while the underlying tagging
stays fine-grained enough for the seed dictionary to be useful. No data migration for
existing transactions. Every aggregate stays a flat `GROUP BY` plus at most one
`OR parent_id = X` predicate, so nothing becomes dialect-specific. The picker gets a
natural two-level shape instead of a 50-item flat list.

**Drawbacks:** Depth 2 is arbitrary and will eventually feel tight for someone who wants
*Shopping → Electronics → Phones*. Allowing both levels to be taggable means a parent's
total is "direct spend + children", so every UI that shows a parent must decide how to
render the direct part — a genuine, recurring source of off-by-one-row bugs. And the cap
is enforced in exactly one place (the router), so a future service-layer or bulk-import
write path could bypass it.

**Mitigation:** The depth cap is asserted by API tests rather than left to convention, and
the *only* write paths are the router and the backup importer (which builds roots before
children and can therefore never construct a third level). The "direct spend" rendering
rule is pinned by PRD §Verification step 16 rather than left to each component.

## Alternatives Considered

**Alternative 1 — Unlimited depth with a recursive CTE**: rejected. It buys a level nobody
asked for at the cost of making every dashboard aggregate recursive, on two dialects, for
a personal-scale dataset of a few tens of thousands of rows. ADR-0001's portability rules
get materially harder to hold.

**Alternative 2 — A separate `category_groups` table**: rejected. A second table, a second
set of tenant-isolation rules, a second archive contract, and a second thing to export —
for what one nullable self-FK expresses. `AGENTS.md` §Simplicity first: no abstraction
ahead of the second concrete use, and there is only one.

**Alternative 3 — Use F3a labels instead of a hierarchy**: rejected. Labels are already
shipped and are deliberately **many-to-many and orthogonal** to the single `category_id`
(PRD §F3a). Grouping is a strict 1:N property *of* the category, not a cross-cutting tag
of the transaction; expressing it as labels would make "which group is this category in"
a query over transactions rather than a column.

**Alternative 4 — Leaf-only tagging (a transaction may only point at a subcategory)**:
rejected. It is the cleaner model on paper — a parent's total is unambiguously the sum of
its children — but it requires migrating every existing transaction off the old flat
categories, and it leaves the F3/F4a "Other" default with nowhere valid to point. The cost
is a real data migration on live rows; the benefit is avoiding one rendering rule.
