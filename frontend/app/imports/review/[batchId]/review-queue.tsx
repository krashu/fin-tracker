"use client";

/**
 * Live import-review queue — the data half of /imports/review/[batchId]. The
 * server shell (page.tsx) owns theme/chrome; this island fetches the
 * batch's pending candidates and drives the tag → stage → commit loop.
 *
 * Contract notes that shape the logic (PRD §F1/§F3/§F9):
 *  - candidates are rows with confirmed_at IS NULL; committing removes them, so
 *    there is no "confirmed" group here.
 *  - staging is category-based, with one exception: a row with a category is
 *    selected by default, and so is an untagged cashback-named income credit —
 *    it commits under the seeded "Cashback" category with zero interaction
 *    (imports.py's commit-time fallback, excluded from F3 learning; see
 *    `defaultStagedFor`). Untagged spend still starts unchecked — a refund is
 *    a positive-amount `spend` row (ADR-0009), not its own type, so it gets no
 *    special-cased default either. A staged untagged spend commits under the
 *    seeded "Other" category (the backend defaults it); other income stays null.
 *  - propagation is invalidate-on-mutation (no SSE): a category PATCH refreshes
 *    this batch; a commit refreshes the board.
 */
import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandSeparator,
} from "@/components/ui/command";
import { TagPicker } from "@/components/review/tag-picker";
import { LabelInput } from "@/components/labels/label-input";
import { ImportStepper } from "@/components/ui/stepper";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { Sensitive } from "@/components/balance-visibility";
import {
  IconArrowRight,
  IconRefresh,
  IconX,
} from "@/components/icons";
import {
  ApiError,
  cancelImport,
  commitImport,
  deleteTransaction,
  getBatchReconciliation,
  listAccounts,
  listCandidates,
  listCategories,
  listLabels,
  patchTransaction,
  type AccountRead,
  type CategoryKind,
  type CategoryRead,
  type LabelRead,
  type TransactionCandidate,
} from "@/lib/api/client";
import {
  buildCategoryTree,
  categoryKindForType,
  resolveCategoryColor,
} from "@/lib/categories";
import { CategoryDot } from "@/components/category-dot";
import { formatINR, formatDateWithYear } from "@/lib/format";
import { sameLabelSet } from "@/lib/labels";
import { invalidateRules } from "@/lib/queries/invalidate";
import { type EditableTransactionType } from "@/lib/transaction-types";
import { cn } from "@/lib/utils";

// checkbox · date · merchant(flex) · tags · category · amount · discard.
// No Account column: a batch is single-account (ImportBatch.account_id — every
// row shares it), so the account is shown once in the header instead of repeated
// per row, and that width goes to Merchant — the primary identifier (long raw
// statement strings), which takes the flex space. Date is 100px to fit the
// always-shown year ("11 May 2026"). Fixed tracks ≈ 631px + gaps.
const GRID = "40px 100px minmax(0,1fr) 150px 190px 115px 36px";
const GRID_MIN_WIDTH = 900;

// Max concurrent PATCHes in a bulk-categorize fan-out. There is no bulk PATCH
// endpoint (transactions.py exposes only PATCH /{transaction_id}), so a fan-out is
// the only option — but 79 rows is ADR-0008's design case, the sync endpoints share
// the 40-slot anyio threadpool and SQLite serialises writes, so the fan-out runs in
// sequential chunks of this size rather than all at once. (The auth rate limiter is
// not a factor — core/rate_limit.py is applied in api/v1/auth.py only.)
const BULK_CHUNK = 8;

/** Mirrors the backend's `is_cashback_credit` (import_service.py) — same
 * keyword, so the credit-row default shown here is exactly what the backend
 * will actually stamp at commit. Duplicated rather than fetched because it's
 * a one-line rule; `categoryKindForType`/`kind_for_type` is the precedent for
 * mirroring a rule this small on both sides. */
const CASHBACK_RE = /cashback/i;
function isCashbackMerchant(merchantRaw: string | null): boolean {
  return merchantRaw != null && CASHBACK_RE.test(merchantRaw);
}

/**
 * A staged row with no category commits under a seeded fallback — "Other" for
 * spend (a refund included: it's a positive-amount `spend` row, not its own
 * type — ADR-0009), "Cashback" for an income row whose merchant names it
 * cashback (`isCashbackMerchant`). Any other income stays uncategorized (an
 * auto-linked CC payment must become a category-null transfer, so income
 * never gets a blanket default). Drives the "will import as …" hint + the
 * picker's default label; the backend applies the actual category at commit.
 */
function defaultsToOther(c: TransactionCandidate): boolean {
  return (
    c.category_id == null &&
    (c.transaction_type === "spend" ||
      (c.transaction_type === "income" && isCashbackMerchant(c.merchant_raw)))
  );
}

/**
 * Whether `c` is staged by DEFAULT, before any user toggle — what `toggled`
 * (below) represents a flip AWAY from. A row with a category counts, same as
 * always. An untagged cashback-named income row additionally counts: it
 * commits under a seeded fallback category with zero interaction, and
 * `commit_import_batch`'s `defaulted_ids` guard only excludes a row from F3
 * learning when its `category_id` is still NULL at commit time — so leaving
 * it null (never writing the fallback category id here) is what keeps this
 * auto-stage safe: the fallback can never masquerade as a real
 * merchant→category decision. Spend — a refund included, ADR-0009 collapsed
 * it into a positively-signed spend rather than its own type, so it no longer
 * gets a safe default of its own — and any other income are deliberately NOT
 * extended this way: spend's "Other" fallback is a bigger silent call (a
 * genuinely new merchant), and income otherwise has no safe default at all
 * (Salary vs Cashback vs Freelancing).
 */
function defaultStagedFor(c: TransactionCandidate): boolean {
  return (
    c.category_id != null ||
    (c.transaction_type === "income" && isCashbackMerchant(c.merchant_raw))
  );
}

/** Pull `invalid_ids` from a commit 422 body; [] for any other error shape. */
function invalidIdsFromError(err: unknown): number[] {
  if (!(err instanceof ApiError) || err.status !== 422) return [];
  const detail = (err.body as { detail?: unknown } | undefined)?.detail;
  const ids = (detail as { invalid_ids?: unknown } | undefined)?.invalid_ids;
  return Array.isArray(ids)
    ? ids.filter((x): x is number => typeof x === "number")
    : [];
}

export function ReviewQueue({ batchId }: { batchId: number }) {
  const queryClient = useQueryClient();
  const router = useRouter();
  // How many rows from this upload were skipped as already-present (passed by the
  // upload form on a re-upload). Shown as a note so the user knows the file's
  // other rows are already imported and aren't in this queue. Not a live value —
  // it reflects the upload that navigated here.
  const alreadyPresent = Number(useSearchParams().get("present")) || 0;

  // Staging default is category-based: rows WITH a category are pre-checked
  // (auto-tagged spends, or anything the user tags — incl. Cashback); untagged
  // rows start UNCHECKED. `toggled` holds the ids whose checkbox the user flipped
  // away from that default, so the effective staged set derives at render
  // (`isStaged`) with no reconcile effect. See also `defaultsToOther`.
  const [toggled, setToggled] = useState<Set<number>>(new Set());
  // Rows the user marked for deletion via the row's discard button. This is a
  // *soft* (client-side, reversible) delete: the row stays visible, struck
  // through, until Commit — which hard-deletes it (DELETE /transactions/{id}).
  // A misclick is undoable any time before Commit. Not persisted; navigating
  // away just leaves the row in the pending queue, unmarked.
  const [markedForDeletion, setMarkedForDeletion] = useState<Set<number>>(
    new Set(),
  );
  const [unstagedNotice, setUnstagedNotice] = useState<string | null>(null);
  // Rows with an in-flight category PATCH — their picker is disabled until it
  // settles, so a rapid A→B re-pick on the same row can't fire an overlapping
  // PATCH whose *arrival* order (not the user's final choice) decides the tag.
  const [pendingRowIds, setPendingRowIds] = useState<Set<number>>(new Set());
  // Rows with an in-flight label PATCH — their editor disables until it settles
  // (mirrors pendingRowIds' discipline for the category). Labels commit once per
  // editing session, on popover close, so overlap is rare, but the guard keeps a
  // reopen-before-settle from racing a replace-set write.
  const [savingLabelIds, setSavingLabelIds] = useState<Set<number>>(new Set());
  // A label PATCH that failed — shown as a notice; cleared on the next attempt.
  const [labelError, setLabelError] = useState<string | null>(null);
  // A category/type PATCH that failed (patchMutation or typeMutation) — same
  // pattern as labelError, cleared on the next attempt via each mutation's
  // onMutate. Without this, a failed single-row PATCH renders nothing at all.
  const [mutationError, setMutationError] = useState<string | null>(null);
  // Per-row remount counter: bumped on a label-save error to force the row to
  // re-seed its once-seeded `draftLabels` from server-truth `c.labels` (the
  // optimistic set lived only in ReviewRow, never in the candidates cache).
  const [labelResetTicks, setLabelResetTicks] = useState<
    Record<number, number>
  >({});
  // Outcome of the last bulk-categorize run — separates deliberate skips from real
  // failures (a collapsed "N of M, K failed" leaves the user unable to tell what to
  // retry). Modelled on labelError; cleared on the next attempt.
  const [bulkNotice, setBulkNotice] = useState<string | null>(null);
  // Whether Cancel-import's confirmation dialog is open (UX-08). Cancel is the one
  // irreversible action on this screen: the backend hard-deletes the pending rows.
  const [cancelConfirmOpen, setCancelConfirmOpen] = useState(false);
  // Anchor for shift-click range-select, held as a transaction ID rather than a row
  // index and resolved against the live `candidates` array at click time. A partial
  // commit does NOT navigate away — it stamps confirmed_at, the committed rows drop
  // out of the refetched list, and the user is still here — so a stored index would
  // then point at a different row. (Per-row discard is a soft client-side mark that
  // never shrinks the list, and list_candidates orders by date DESC, id DESC, so
  // nothing else can reorder or shorten the array underneath the anchor.)
  const rangeAnchorId = useRef<number | null>(null);

  // Phase 6 keyboard loop. `focusedIndex` is a purely visual roving highlight —
  // it never moves real DOM focus, so it can't fight the popover's own focus
  // trap or the tag editor's input focus. `rowRefs` backs `scrollIntoView` when
  // `j`/`k` moves the highlight off-screen. `openPickerFor` replaces the
  // category picker's old row-local `useState` (see `ReviewRow`) — `c` needs to
  // open a specific row's popover from the page-level keydown handler, which a
  // row-local flag can't do.
  const [focusedIndex, setFocusedIndex] = useState<number | null>(null);
  const [openPickerFor, setOpenPickerFor] = useState<number | null>(null);
  const rowRefs = useRef<(HTMLLIElement | null)[]>([]);

  const candidatesQuery = useQuery({
    queryKey: ["candidates", batchId],
    queryFn: () => listCandidates(batchId),
  });
  // Statement balance reconciliation (PRD §F1/§F4a). The backend recomputes +
  // persists the delta on every GET, so this is always current with the DB —
  // not just an echo of what the upload computed. Invalidated below by the
  // commit mutation, whose DELETEs of marked-for-deletion rows are the one
  // review-queue action that can move the batch's window sum — a category
  // PATCH can't (reconcile_batch sums amount_paise over the date window,
  // never filtered by category_id), so it deliberately does NOT invalidate
  // this query.
  const reconciliationQuery = useQuery({
    queryKey: ["reconciliation", batchId],
    queryFn: () => getBatchReconciliation(batchId),
  });
  // The GET above recomputes + persists reconciliation_delta_paise onto the
  // SAME ImportBatch row that /imports/pending reads, purely as a side effect
  // of being fetched — just opening this page can change the persisted
  // figure (e.g. the window-fallback stamp on a first check). Keep the
  // pending-batches badge / bell in sync by invalidating whenever the fetched
  // value actually changes; structural sharing keeps `.data`'s reference
  // stable when it hasn't, so this doesn't fire on every remount or
  // mutation-triggered refetch.
  useEffect(() => {
    if (reconciliationQuery.data) {
      queryClient.invalidateQueries({ queryKey: ["imports", "pending"] });
    }
  }, [reconciliationQuery.data, queryClient]);
  const accountsQuery = useQuery({
    queryKey: ["accounts"],
    queryFn: listAccounts,
  });
  const categoriesQuery = useQuery({
    queryKey: ["categories"],
    queryFn: listCategories,
  });
  // Prime the label catalog on mount so the per-row tag type-ahead has
  // suggestions immediately (its own query is `enabled: open`, which would fetch
  // cold on first popover open — showing no matches + a spurious "Create" for a
  // name that already exists). This page is reached straight after upload, so the
  // board (the only other primer) may never have loaded it.
  useQuery({ queryKey: ["labels"], queryFn: listLabels });

  const accountsById = new Map<number, AccountRead>(
    (accountsQuery.data ?? []).map((a) => [a.id, a]),
  );
  const categories = categoriesQuery.data ?? [];
  const categoriesById = new Map<number, CategoryRead>(
    categories.map((c) => [c.id, c]),
  );

  const candidates = candidatesQuery.data ?? [];
  const candidatesById = new Map(candidates.map((c) => [c.id, c]));
  // Rows sharing an exact merchant_raw — the batching unit for review. Exact string
  // only: normalize_merchant is lowercase + whitespace-collapse, so it would not
  // collapse "MYNTRA,BANGALORE" into "MYNTRA DESIGNS PRIVATE L,BANGALORE" either.
  // Widening that is the canonical-alias layer (plans/merchant-alias.md, ADR-0011).
  const merchantGroups = new Map<string, number[]>();
  for (const c of candidates) {
    if (c.merchant_raw == null) continue;
    const ids = merchantGroups.get(c.merchant_raw) ?? [];
    ids.push(c.id);
    merchantGroups.set(c.merchant_raw, ids);
  }
  // A batch is single-account (ImportBatch.account_id) — every row shares it, so
  // the account is resolved once from the first candidate and shown in the header
  // rather than repeated per row.
  const batchAccount = accountsById.get(candidates[0]?.account_id ?? -1);
  // Checkbox state is category-based, plus one exception — see
  // `defaultStagedFor`: a row is default-staged if it has a category
  // (auto-tagged spends, or anything the user tags — incl. Cashback) OR is an
  // untagged cashback-named income credit. `toggled` holds the ids the user
  // flipped away from that default, so the effective set derives at render
  // with no reconcile effect. Tagging a row (or picking a spend/income
  // category on a credit row) auto-selects it: after the PATCH refetch it
  // carries the new state → default-staged.
  const isStaged = (c: TransactionCandidate): boolean =>
    defaultStagedFor(c) !== toggled.has(c.id);
  // Commit-eligible = staged AND not marked for deletion (marked rows are
  // removed, not committed). Single source for the four places this rule is used.
  const isEffectivelyStaged = (c: TransactionCandidate): boolean =>
    isStaged(c) && !markedForDeletion.has(c.id);
  // Marked rows can't also be staged — intersect both sets with live candidates
  // so ids of rows already deleted/committed on a prior pass drop out.
  const markedIds = candidates
    .filter((c) => markedForDeletion.has(c.id))
    .map((c) => c.id);
  const markedCount = markedIds.length;
  const effectiveStaged = candidates
    .filter((c) => isEffectivelyStaged(c))
    .map((c) => c.id);
  const stagedCount = effectiveStaged.length;
  // Staged spend rows without a category commit under "Other" (backend; a
  // refund is a spend row, so it's covered here too — ADR-0009); staged
  // cashback-named income rows without one commit under "Cashback" — split so
  // the header names the right bucket instead of collapsing both under one
  // "Other" count.
  const otherDefaultCount = candidates.filter(
    (c) => isEffectivelyStaged(c) && c.category_id == null && c.transaction_type === "spend",
  ).length;
  const cashbackDefaultCount = candidates.filter(
    (c) =>
      isEffectivelyStaged(c) &&
      c.category_id == null &&
      c.transaction_type === "income" &&
      isCashbackMerchant(c.merchant_raw),
  ).length;
  // Header select-all. The `stagedCount > 0` conjunct is load-bearing: with every
  // row marked for deletion the counts would read `candidates.length > 0 && 0 === 0`
  // → the box would show fully checked at zero staged.
  const allStaged =
    stagedCount > 0 && stagedCount === candidates.length - markedCount;

  // --- Bulk categorize target set --------------------------------------------
  // Reversed 2026-08-12 (previously D8: "apply + warn" instead of a hidden
  // skip). Found live-testing: categorizing 3 rows, then staging 4 MORE for a
  // different category, silently re-applied the second category to the first
  // 3 too — staging and "this action's target" were the same set, with no
  // exclusion for a decision already made. Now: an already-categorized row is
  // never a bulk target, full stop — surfaced as a count, never hidden (D9's
  // own no-hidden-skip rule, extended to cover this reason too).
  const stagedCandidates = candidates.filter((c) => isEffectivelyStaged(c));
  const bulkAlreadyCategorizedCount = stagedCandidates.filter(
    (c) => c.category_id != null,
  ).length;
  // Also excludes `pendingRowIds` — a row mid-PATCH from the per-row picker
  // still reads `category_id == null` in the cache for that tick; without
  // this, a bulk click racing that PATCH could re-target the same row the
  // per-row pick just decided, defeating the guarantee this fix adds through
  // a different door.
  //
  // Also excludes untagged `cc_payment_candidate` rows (2026-08-12 follow-up
  // to the fix above, same shape). The per-row picker has a real guard for
  // these — TagPicker's `cardPayment` branch keeps PRD §F4a-1's auto-link
  // live by defaulting them to no category — but the bulk path never got
  // one, so a card-bill-payment row swept into a staged batch could still
  // have a real category force-applied over it here, silently defeating
  // that guarantee. Punting on the real fix (a bulk-safe version of the
  // per-row "Card bill payment" option, so a batch of these can still be
  // resolved in one action later) — for now, just keep bulk categorize from
  // ever touching them, same skip-and-surface treatment as the others.
  const bulkUndecided = stagedCandidates.filter(
    (c) =>
      c.category_id == null &&
      !pendingRowIds.has(c.id) &&
      !c.cc_payment_candidate,
  );
  const bulkCardPaymentSkippedCount = stagedCandidates.filter(
    (c) => c.category_id == null && c.cc_payment_candidate,
  ).length;
  // D9 mixed-type selection: the picker offers spend categories when the
  // UNDECIDED subset holds any spend-kind row (spend, including a refund —
  // ADR-0009 — or a transfer), else income. Computed from `bulkUndecided`,
  // not the full staged set: the rule is "any spend row forces spend" (not a
  // majority vote), so even ONE leftover already-categorized spend row was
  // already enough to force the kind away from genuinely-uncategorized
  // income rows before this fix — scoping to the undecided subset closes
  // that too. Rows of the other kind are SKIPPED, not failed —
  // `_assert_category_id_or_422` (transactions.py) rejects a kind mismatch,
  // so PATCHing them would 422.
  const bulkKind: CategoryKind = bulkUndecided.some(
    (c) => categoryKindForType(c.transaction_type) === "spend",
  )
    ? "spend"
    : "income";
  const bulkTargets = bulkUndecided
    .filter((c) => categoryKindForType(c.transaction_type) === bulkKind)
    .map((c) => c.id);
  const bulkKindSkippedCount = bulkUndecided.length - bulkTargets.length;

  // Force a set of ids to an ABSOLUTE staged state. Staging is derived — `toggled`
  // holds the ids flipped away from the default (`defaultStagedFor`) — so forcing a
  // state means storing a flip only when the wanted state differs from that
  // default: a default-staged row (categorized, or an untagged cashback-named
  // income credit) flips ON in `toggled` to go OFF; a not-default-staged row
  // flips ON to go ON. Used by select-all / range-select and the label-save
  // auto-stage (`on: true`), and by commit success/error via `forceUnstage`.
  const setStagedFor = (ids: Iterable<number>, on: boolean) =>
    setToggled((prev) => {
      const next = new Set(prev);
      for (const id of ids) {
        const c = candidatesById.get(id);
        if (!c) continue;
        const defaultStaged = defaultStagedFor(c);
        if (defaultStaged === on) next.delete(id);
        else next.add(id);
      }
      return next;
    });

  // Force a set of ids OFF (used by commit success/error).
  const forceUnstage = (ids: Iterable<number>) => setStagedFor(ids, false);

  const patchMutation = useMutation({
    mutationFn: (vars: { id: number; categoryId: number | null }) =>
      patchTransaction(vars.id, { category_id: vars.categoryId }),
    onMutate: (vars) => {
      setMutationError(null);
      setPendingRowIds((prev) => new Set(prev).add(vars.id));
    },
    // Picking a category is a fresh staging decision: reset the row to the pure
    // category-based default by clearing any prior `toggled` flip, THEN refetch so
    // the refreshed category_id drives `isStaged`. Without the reset, a row the
    // user had flipped away from its default silently un-stages (or re-stages) the
    // instant they categorize it — `(category_id!=null) !== toggled.has(id)` flips
    // to the wrong value. Works for any type — a Cashback (income) row auto-checks
    // the same as a spend.
    onSuccess: (_data, vars) => {
      setToggled((prev) => {
        if (!prev.has(vars.id)) return prev;
        const next = new Set(prev);
        next.delete(vars.id);
        return next;
      });
      queryClient.invalidateQueries({ queryKey: ["candidates", batchId] });
      // Deliberately NOT ["reconciliation", batchId] — a category PATCH can't
      // move reconcile_batch's window sum (it filters on account_id/date, never
      // category_id), so invalidating here would only cost an extra GET-that-
      // writes on every single categorize click for no visible change.
    },
    onError: () => setMutationError("Couldn't update the category — try again."),
    onSettled: (_data, _err, vars) =>
      setPendingRowIds((prev) => {
        const next = new Set(prev);
        next.delete(vars.id);
        return next;
      }),
  });

  // Explicit Spend/Income retype (the review queue's own affordance for the
  // same PATCH transaction-dialog.tsx exposes on the board). ADR-0007 rule 5:
  // a kind flip must carry `category_id` in the SAME request, even when it's
  // unchanged — so callers always state it (kept if the row's current category
  // still matches the new kind, cleared otherwise), never relying on the
  // backend to infer it. The category picker itself no longer touches
  // `transaction_type` (see `patchMutation` above).
  const typeMutation = useMutation({
    mutationFn: (vars: { id: number; type: EditableTransactionType; categoryId: number | null }) =>
      patchTransaction(vars.id, { transaction_type: vars.type, category_id: vars.categoryId }),
    onMutate: (vars) => {
      setMutationError(null);
      setPendingRowIds((prev) => new Set(prev).add(vars.id));
    },
    // Mirrors patchMutation.onSuccess: a retype is also a fresh staging decision.
    onSuccess: (_data, vars) => {
      setToggled((prev) => {
        if (!prev.has(vars.id)) return prev;
        const next = new Set(prev);
        next.delete(vars.id);
        return next;
      });
      queryClient.invalidateQueries({ queryKey: ["candidates", batchId] });
    },
    onError: () => setMutationError("Couldn't update the category — try again."),
    onSettled: (_data, _err, vars) =>
      setPendingRowIds((prev) => {
        const next = new Set(prev);
        next.delete(vars.id);
        return next;
      }),
  });

  // Bulk categorize the staged rows — UX-07 / ADR-0008's named v1 mitigation. Same
  // endpoint as the single-row picker, fanned out in BULK_CHUNK-sized waves.
  // `patchMutation`'s per-row discipline is reused wholesale: targets go into
  // `pendingRowIds` so their pickers grey out, and the successful ids are cleared
  // from `toggled` — the generalisation of patchMutation.onSuccess, and load-bearing:
  // a row the user had flipped away from its default would otherwise land on the
  // WRONG staged value the instant it gains a category.
  // `alreadyCategorized` / `kindSkipped` / `skippedKind` / `cardPaymentSkipped`
  // ride along as mutation variables (click-time snapshot, like
  // commitMutation's sets) so the notice can't be written from a set that the
  // post-PATCH refetch has already moved on from.
  const bulkMutation = useMutation({
    mutationFn: async (vars: {
      ids: number[];
      categoryId: number;
      alreadyCategorized: number;
      kindSkipped: number;
      skippedKind: CategoryKind;
      cardPaymentSkipped: number;
    }) => {
      const updated: number[] = [];
      let failed = 0;
      for (let i = 0; i < vars.ids.length; i += BULK_CHUNK) {
        const chunk = vars.ids.slice(i, i + BULK_CHUNK);
        const results = await Promise.allSettled(
          chunk.map((id) => patchTransaction(id, { category_id: vars.categoryId })),
        );
        results.forEach((r, j) => {
          if (r.status === "fulfilled") updated.push(chunk[j]);
          else failed += 1;
        });
      }
      return { updated, failed };
    },
    onMutate: (vars) => {
      setBulkNotice(null);
      setUnstagedNotice(null);
      setPendingRowIds((prev) => {
        const next = new Set(prev);
        for (const id of vars.ids) next.add(id);
        return next;
      });
    },
    onSuccess: ({ updated, failed }, vars) => {
      setToggled((prev) => {
        const next = new Set(prev);
        for (const id of updated) next.delete(id);
        return next;
      });
      // One invalidation for the batch, not one per row.
      queryClient.invalidateQueries({ queryKey: ["candidates", batchId] });
      const parts = [`${updated.length} updated`];
      if (vars.alreadyCategorized > 0) {
        parts.push(`${vars.alreadyCategorized} already categorized`);
      }
      if (vars.kindSkipped > 0) {
        parts.push(
          `${vars.kindSkipped} ${vars.skippedKind} ${
            vars.kindSkipped === 1 ? "row" : "rows"
          } skipped`,
        );
      }
      if (vars.cardPaymentSkipped > 0) {
        parts.push(
          `${vars.cardPaymentSkipped} card payment ${
            vars.cardPaymentSkipped === 1 ? "row" : "rows"
          } skipped`,
        );
      }
      parts.push(`${failed} failed`);
      setBulkNotice(parts.join(" · "));
    },
    onError: () => setBulkNotice("Couldn’t categorize — try again."),
    onSettled: (_data, _err, vars) =>
      setPendingRowIds((prev) => {
        const next = new Set(prev);
        for (const id of vars.ids) next.delete(id);
        return next;
      }),
  });

  // Label edits: PATCH the row's full label set (replace-set), then patch the
  // returned labels into the cached candidate in place via setQueryData — NOT
  // invalidateQueries. Labels have no effect on prior_matches/confidence/staging,
  // so a full-list refetch would only flash/reorder the queue.
  const labelMutation = useMutation({
    mutationFn: (vars: { id: number; labels: string[] }) =>
      patchTransaction(vars.id, { labels: vars.labels }),
    onMutate: (vars) => {
      setLabelError(null);
      setSavingLabelIds((prev) => new Set(prev).add(vars.id));
    },
    onSuccess: (data, vars) => {
      queryClient.setQueryData<TransactionCandidate[]>(
        ["candidates", batchId],
        (old) =>
          old?.map((c) =>
            c.id === vars.id ? { ...c, labels: data.labels } : c,
          ),
      );
      // A tag save stages the row, matching the category path (UX-17): adding a tag
      // asserts "I reviewed this", and `isStaged` reads only `category_id`, so
      // without this the row stays unchecked and the user has to tick it too.
      // Deliberately NOT symmetric — clearing the last tag is a retraction, not a
      // reason to unstage, and `onCommit` only fires on a real diff so a no-op edit
      // changes nothing either way. Marked-for-deletion rows need no special case:
      // `isEffectivelyStaged` masks them.
      if (data.labels.length > 0) setStagedFor([vars.id], true);
      // A newly-typed tag is get-or-created server-side; refresh the catalog so
      // the board filter / Settings / autocomplete pick it up. Skip the refetch
      // when the save only reused/removed existing tags (the common case) — but
      // only when we can trust the cached catalog: if it isn't present in a
      // success state, invalidate unconditionally rather than miss a real create.
      const catalog = queryClient.getQueryState<LabelRead[]>(["labels"]);
      const known = new Set(
        (catalog?.data ?? []).map((l) => l.name.toLowerCase()),
      );
      const hasNewTag =
        catalog?.status !== "success" ||
        data.labels.some((l) => !known.has(l.name.toLowerCase()));
      if (hasNewTag) {
        queryClient.invalidateQueries({ queryKey: ["labels"] });
      }
    },
    onError: (_err, vars) => {
      // The PATCH is atomic and the optimistic set was never written to the
      // candidates cache (only ReviewRow's local draftLabels), so c.labels is
      // still server-truth. Surface the failure and bump the row's reset tick so
      // it remounts and re-seeds draftLabels from c.labels.
      setLabelError("Couldn’t save tags — try again.");
      setLabelResetTicks((prev) => ({
        ...prev,
        [vars.id]: (prev[vars.id] ?? 0) + 1,
      }));
    },
    onSettled: (_data, _err, vars) =>
      setSavingLabelIds((prev) => {
        const next = new Set(prev);
        next.delete(vars.id);
        return next;
      }),
  });

  // Commit does two things: commit the staged rows to the board AND hard-delete
  // the rows marked for deletion. Commit-first — a failed commit (422) aborts
  // before any destructive delete. Sets are snapshotted as mutation variables at
  // click time so a concurrent category-PATCH refetch can't desync onSuccess.
  const commitMutation = useMutation({
    mutationFn: async ({
      staged,
      marked,
    }: {
      staged: number[];
      marked: number[];
    }) => {
      // Skip the commit call when nothing is staged — ImportCommit requires
      // min_length=1, so an empty commit would 422. A delete-only Commit is valid.
      if (staged.length > 0) {
        await commitImport(batchId, staged);
      }
      let failedDeletes = 0;
      if (marked.length > 0) {
        const results = await Promise.allSettled(
          marked.map((id) => deleteTransaction(id)),
        );
        // A 404 (row already gone) is expected + idempotent; any other rejection
        // is a genuine failure we surface rather than hide.
        failedDeletes = results.filter(
          (r) =>
            r.status === "rejected" &&
            !(r.reason instanceof ApiError && r.reason.status === 404),
        ).length;
      }
      return { failedDeletes };
    },
    onSuccess: (result, { staged }) => {
      queryClient.invalidateQueries({ queryKey: ["transactions"] });
      queryClient.invalidateQueries({ queryKey: ["dashboards"] });
      // Commit is the primary rule-learning event (imports.py learns both maps),
      // so refresh /settings/rules + tagging-health + candidate confidence.
      invalidateRules(queryClient);
      // Force the just-committed rows off so stagedCount drops to 0 immediately
      // — the commit button disables before the candidates refetch lands,
      // preventing a second POST of now-confirmed ids. Uses the click-time
      // snapshot, not the render-derived set.
      forceUnstage(staged);
      // Commit succeeded (commit-first). Surface any real delete failures softly:
      // the failed rows are still in the DB and still in `markedForDeletion`, so
      // they reappear marked on refetch and a re-Commit retries them.
      setUnstagedNotice(
        result.failedDeletes > 0
          ? `Committed, but couldn't remove ${result.failedDeletes} row${
              result.failedDeletes === 1 ? "" : "s"
            } — still in the queue. Try Commit again.`
          : null,
      );
    },
    onError: (err) => {
      const bad = invalidIdsFromError(err);
      if (bad.length === 0) return;
      forceUnstage(bad);
      // Commit-first aborts before deletes, so marked rows were NOT removed either.
      setUnstagedNotice(
        `${bad.length} row${bad.length === 1 ? "" : "s"} became ineligible and ${
          bad.length === 1 ? "was" : "were"
        } unstaged; nothing was committed or removed. Re-check and commit again.`,
      );
    },
    onSettled: () => {
      // Queue + top-bar bell change regardless of outcome (deletes may have run;
      // a 422 unstaged rows).
      queryClient.invalidateQueries({ queryKey: ["candidates", batchId] });
      queryClient.invalidateQueries({ queryKey: ["imports", "pending"] });
      // Commit's DELETEs of marked-for-deletion rows are the other action that
      // can move the batch's window sum — refresh the reconciliation verdict
      // regardless of outcome, same as the two invalidations above.
      queryClient.invalidateQueries({ queryKey: ["reconciliation", batchId] });
    },
  });

  const cancelMutation = useMutation({
    mutationFn: () => cancelImport(batchId),
    onSuccess: () => {
      // Cancelling clears this batch's pending rows — refresh the top-bar bell.
      queryClient.invalidateQueries({ queryKey: ["imports", "pending"] });
      router.push("/imports/statements");
    },
  });

  // Per-row soft delete: toggle a row in/out of the marked-for-deletion set.
  // No API call — the actual DELETE fires on Commit. Reversible until then.
  function toggleMarked(id: number) {
    setUnstagedNotice(null);
    setMarkedForDeletion((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  // Flip a row's checkbox relative to its sign-based default. A shift-click extends
  // from the last-clicked row instead: every row between the anchor and this one is
  // forced to the state the clicked row is moving to (standard extend semantics),
  // then the anchor moves here. A vanished anchor degrades to a plain click.
  function toggleRow(id: number, index: number, shiftKey: boolean) {
    setUnstagedNotice(null);
    const anchorId = rangeAnchorId.current;
    const anchorIndex =
      anchorId == null ? -1 : candidates.findIndex((c) => c.id === anchorId);
    rangeAnchorId.current = id;
    const clicked = candidatesById.get(id);
    if (shiftKey && clicked && anchorIndex >= 0 && anchorIndex !== index) {
      const [from, to] =
        anchorIndex < index ? [anchorIndex, index] : [index, anchorIndex];
      setStagedFor(
        candidates.slice(from, to + 1).map((c) => c.id),
        !isStaged(clicked),
      );
      return;
    }
    setToggled((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function setCategory(id: number, categoryId: number | null) {
    setUnstagedNotice(null);
    patchMutation.mutate({ id, categoryId });
  }

  // Credit-row (refund/income) categorization — a merged spend + income
  // category dropdown replaces the separate type toggle + category picker for
  // a positive row, because sign alone can't tell a merchant refund from a
  // cashback credit apart (PRD §F5); the category the user picks is what
  // decides. Picking a spend category makes the row a positive `spend` — a
  // refund, netting against that category (ADR-0009) — rather than resolving
  // some seeded "Refund" bucket; picking an income category makes it
  // `income`. Both PATCH `category_id` in the SAME request ADR-0007 rule 5
  // requires on a kind flip. Unlike the old flat-Refund shortcut, this never
  // resolves to a null category on its own — an untagged credit stays
  // unstaged and uncategorized until the user picks one, same as an untagged
  // spend (see `defaultStagedFor`).
  function setCreditCategory(
    id: number,
    type: "spend" | "income",
    categoryId: number,
  ) {
    setUnstagedNotice(null);
    typeMutation.mutate({ id, type, categoryId });
  }

  // Merchant-group select (F1 review throughput): adds every row sharing this
  // merchant to the staged set — never clears (D8), so hand-picked rows survive.
  function selectGroup(ids: number[]) {
    setUnstagedNotice(null);
    setStagedFor(ids, true);
  }

  // "Card bill payment" (PRD §F4a-1): the row stays `income` with
  // `category_id` NULL, so auto_link_cc_bill can pair it with a matching
  // bank debit at commit instead of a category ever being applied.
  function markCardPayment(id: number) {
    setUnstagedNotice(null);
    const c = candidatesById.get(id);
    if (c?.category_id != null) {
      // Clear a mis-set category first; stage only after the PATCH settles, because
      // patchMutation.onSuccess clears this row's `toggled` flip and an income row
      // with a null category is NOT default-staged — staging before would be undone.
      patchMutation.mutate(
        { id, categoryId: null },
        { onSuccess: () => setStagedFor([id], true) },
      );
      return;
    }
    setStagedFor([id], true);
  }

  function saveLabels(id: number, labels: string[]) {
    labelMutation.mutate({ id, labels });
  }

  // Scrolling is kept out of the keydown handler's state updater below — updater
  // functions must stay pure, since React may invoke them more than once.
  useEffect(() => {
    if (focusedIndex != null) {
      rowRefs.current[focusedIndex]?.scrollIntoView({ block: "nearest" });
    }
  }, [focusedIndex]);

  // Phase 6 keyboard loop (j/k/x/Space/c/Escape). Guards, all required:
  // - a modifier held means this is a browser/OS shortcut, not ours;
  // - typing inside an input/textarea/contenteditable, the cmdk search box, a
  //   Radix dialog, or a Radix popper (the category popover's positioned
  //   content) must reach that control untouched — otherwise `c` types into
  //   the tag editor, Escape fights cmdk's own close-on-Escape, and Space
  //   toggles a row while the Cancel-import confirm is open;
  // - the same in-flight lock the checkboxes use (`discardDisabled` elsewhere)
  //   disables the shortcuts too, inlined here rather than via `commitBusy`
  //   (declared below, after this component's early returns — referencing it
  //   from here would read an uninitialized binding on the loading/empty
  //   render paths).
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      const target = e.target as HTMLElement | null;
      if (
        target?.closest(
          'input, textarea, [contenteditable], [cmdk-input], [role="dialog"], [data-radix-popper-content-wrapper]',
        )
      ) {
        return;
      }
      if (
        commitMutation.isPending ||
        bulkMutation.isPending ||
        cancelMutation.isPending
      ) {
        return;
      }
      if (candidates.length === 0) return;

      // Plain current-value updates, not the `prev =>` functional form —
      // `focusedIndex` is already this effect's own dependency, so the
      // closure is never stale, and the functional form buys nothing here.
      if (e.key === "j" || e.key === "ArrowDown") {
        e.preventDefault();
        setFocusedIndex(
          focusedIndex == null
            ? 0
            : Math.min(focusedIndex + 1, candidates.length - 1),
        );
        return;
      }
      if (e.key === "k" || e.key === "ArrowUp") {
        e.preventDefault();
        setFocusedIndex(focusedIndex == null ? 0 : Math.max(focusedIndex - 1, 0));
        return;
      }
      if (focusedIndex == null) return;
      const focused = candidates[focusedIndex];
      // Defensive, not in the brief's guard list verbatim: `candidates` can
      // shrink (a commit or discard) while `focusedIndex` still points past
      // the new end.
      if (!focused) return;

      if (e.key === "x" || e.key === " ") {
        e.preventDefault();
        toggleRow(focused.id, focusedIndex, false);
        return;
      }
      if (e.key === "c") {
        e.preventDefault();
        setOpenPickerFor(focused.id);
        return;
      }
      if (e.key === "Escape") {
        setFocusedIndex(null);
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [
    candidates,
    focusedIndex,
    commitMutation.isPending,
    bulkMutation.isPending,
    cancelMutation.isPending,
    toggleRow,
  ]);

  // --- States ---------------------------------------------------------------
  if (candidatesQuery.isPending) {
    return <Centered>Loading…</Centered>;
  }
  if (candidatesQuery.isError) {
    const notFound =
      candidatesQuery.error instanceof ApiError &&
      candidatesQuery.error.status === 404;
    return notFound ? (
      <Centered>
        This import batch doesn’t exist — it may have been committed or
        cancelled.{" "}
        <NavLink href="/imports/statements">Import a statement</NavLink>
      </Centered>
    ) : (
      <Centered tone="error">
        Couldn’t load the queue — is the API running?
      </Centered>
    );
  }
  if (candidates.length === 0) {
    return (
      <Centered>
        Nothing pending here — every row has been committed or cancelled.{" "}
        <NavLink href="/expenses">View expenses</NavLink>
      </Centered>
    );
  }

  // Only while a commit is in flight. After success, the committed ids are
  // forced off (`forceUnstage`) so stagedCount drops to 0 and the button
  // disables itself until rows are re-staged; also gating on
  // candidatesQuery.isFetching would grey it out during the harmless post-PATCH
  // refetch too.
  //
  // A bulk-categorize fan-out folds in here (and into `discardDisabled` below) so
  // Commit, Cancel and select-all all go inert while it runs — the whole in-flight
  // lock rides wiring that already exists rather than a sixth flag. Two concrete
  // failures it closes: (i) a Commit landing mid-fan-out stamps confirmed_at, which
  // flips learn_category's gate false→true, so a *pending-row* PATCH would silently
  // teach a merchant rule (ADR-0004 / PRD §F3 exist to prevent exactly that) — and
  // a Cancel mid-fan-out 404s the remaining PATCHes; (ii) unchecking a row
  // mid-fan-out silently re-stages it, because onSuccess clears fulfilled ids from
  // `toggled` against a click-time snapshot.
  const commitBusy = commitMutation.isPending || bulkMutation.isPending;

  return (
    <>
      <Header
        pending={candidates.length}
        otherDefaultCount={otherDefaultCount}
        cashbackDefaultCount={cashbackDefaultCount}
        accountName={batchAccount?.name ?? null}
        accountLast4={batchAccount?.last4 ?? null}
      />

      {alreadyPresent > 0 ? (
        <p className="mb-3 rounded-md border border-border bg-muted/40 px-3 py-2 text-[12px] text-muted-foreground">
          {alreadyPresent} other{" "}
          {alreadyPresent === 1 ? "transaction" : "transactions"} from this
          statement {alreadyPresent === 1 ? "is" : "are"} already imported and{" "}
          {alreadyPresent === 1 ? "isn’t" : "aren’t"} shown here.
        </p>
      ) : null}

      {unstagedNotice ? (
        <p className="mb-3 text-[12px] text-warn">{unstagedNotice}</p>
      ) : null}

      {labelError ? (
        <p className="mb-3 text-[12px] text-neg">{labelError}</p>
      ) : null}
      {mutationError ? (
        <p className="mb-3 text-[12px] text-neg">{mutationError}</p>
      ) : null}
      {bulkNotice ? (
        <p className="mb-3 text-[12px] text-muted-foreground">{bulkNotice}</p>
      ) : null}
      {commitMutation.isError &&
      invalidIdsFromError(commitMutation.error).length === 0 ? (
        <p className="mb-3 text-[12px] text-neg">
          {commitMutation.error instanceof ApiError
            ? commitMutation.error.detail
            : "Couldn’t commit — try again."}
        </p>
      ) : null}

      {/* PRD §F1/§F4a statement balance reconciliation. Only the mismatched
          state renders anything — "matched" and "unavailable" (no usable
          statement metadata) are both non-events here, never a warning. The
          amount is a rupee figure, so it renders through Sensitive.
          rows_removed_since_import (discard-noise qualifier) is a live count,
          not a rupee figure — no Sensitive wrap — surfaced only alongside a
          mismatch, since it exists to explain one, not to stand alone. */}
      {reconciliationQuery.data?.status === "mismatched" ? (
        <div className="mb-3 rounded-md border border-warn-soft-border bg-warn-soft-bg px-3 py-2.5 text-[12.5px] text-foreground">
          <p>
            This statement’s closing balance does not match:{" "}
            <Sensitive>
              {formatINR(Math.abs(reconciliationQuery.data.delta_paise ?? 0))}
            </Sensitive>{" "}
            unaccounted for.
          </p>
          {reconciliationQuery.data.rows_removed_since_import > 0 ? (
            <p className="mt-1 text-muted-foreground">
              {reconciliationQuery.data.rows_removed_since_import}{" "}
              {reconciliationQuery.data.rows_removed_since_import === 1
                ? "row was"
                : "rows were"}{" "}
              removed from this import after it was staged — that can
              explain some or all of this.
            </p>
          ) : null}
        </div>
      ) : null}

      <section className="overflow-hidden rounded-[6px] border border-border bg-card">
        {/* Always-on horizontal scroll: the 8-track grid needs ~1040px, which
            exceeds the content area well before the sidebar breakpoint — so the
            scroll wrapper is decoupled from `md` (keying it to md would leave a
            768→~1040px dead zone that overflows the page with no scroller). The
            min-w container keeps the header + rows aligned inside the scroll. */}
        <div className="overflow-x-auto">
          <div style={{ minWidth: GRID_MIN_WIDTH }}>
            <div
              className="grid items-center border-b border-border bg-muted px-5 py-2 text-[11px] font-medium text-muted-foreground/80"
              style={{ gridTemplateColumns: GRID, columnGap: 16 }}
            >
              {/* Select-all rides the header row's EXISTING empty 40px checkbox
                  cell, so GRID / GRID_MIN_WIDTH are untouched (ADR-0007's
                  row-expand rebase depends on that). Tri-state: a partial
                  selection passes `indeterminate`, which Radix advertises as
                  aria-checked="mixed" and checkbox.tsx draws as a dash, exactly
                  as on expenses-board.tsx. Note `allStaged` keeps its
                  `stagedCount > 0` conjunct, so marking EVERY row for deletion
                  reads unchecked (not mixed) — zero staged is not partial. */}
              <span className="flex items-center">
                <Checkbox
                  aria-label="Stage all rows"
                  checked={
                    allStaged
                      ? true
                      : stagedCount > 0
                        ? "indeterminate"
                        : false
                  }
                  disabled={
                    candidates.length === 0 ||
                    commitBusy ||
                    cancelMutation.isPending
                  }
                  onCheckedChange={(c) =>
                    setStagedFor(
                      candidates.map((x) => x.id),
                      c === true,
                    )
                  }
                />
              </span>
              <span>Date</span>
              <span>Merchant</span>
              <span>Tags</span>
              <span>Category</span>
              <span className="text-right">Amount</span>
              <span />
            </div>

            <ol>
              {candidates.map((c, i) => (
                <ReviewRow
                  key={`${c.id}:${labelResetTicks[c.id] ?? 0}`}
                  c={c}
                  isLast={i === candidates.length - 1}
                  categoryName={
                    c.category_id != null
                      ? (categoriesById.get(c.category_id)?.name ?? null)
                      : null
                  }
                  categories={categories}
                  staged={isEffectivelyStaged(c)}
                  defaultsToOther={isEffectivelyStaged(c) && defaultsToOther(c)}
                  marked={markedForDeletion.has(c.id)}
                  pending={pendingRowIds.has(c.id)}
                  discardDisabled={
                    commitMutation.isPending ||
                    cancelMutation.isPending ||
                    bulkMutation.isPending
                  }
                  labelSaving={savingLabelIds.has(c.id)}
                  groupIds={
                    c.merchant_raw != null
                      ? (merchantGroups.get(c.merchant_raw) ?? [c.id])
                      : [c.id]
                  }
                  focused={focusedIndex === i}
                  rowRef={(el) => {
                    rowRefs.current[i] = el;
                  }}
                  pickerOpen={openPickerFor === c.id}
                  onPickerOpenChange={(open) =>
                    setOpenPickerFor(open ? c.id : null)
                  }
                  onToggle={(shiftKey) => toggleRow(c.id, i, shiftKey)}
                  onPickCategory={(categoryId) => setCategory(c.id, categoryId)}
                  onPickCredit={(type, categoryId) =>
                    setCreditCategory(c.id, type, categoryId)
                  }
                  onMarkCardPayment={() => markCardPayment(c.id)}
                  onSelectGroup={selectGroup}
                  onDiscard={() => toggleMarked(c.id)}
                  onSaveLabels={(labels) => saveLabels(c.id, labels)}
                />
              ))}
            </ol>
          </div>
        </div>
      </section>

      <CommitBar
        stagedCount={stagedCount}
        markedCount={markedCount}
        pendingCount={candidates.length}
        busy={commitBusy}
        cancelBusy={cancelMutation.isPending}
        categories={categories}
        categoryKind={bulkKind}
        bulkTargetCount={bulkTargets.length}
        bulkAlreadyCategorizedCount={bulkAlreadyCategorizedCount}
        bulkCardPaymentSkippedCount={bulkCardPaymentSkippedCount}
        bulkBusy={bulkMutation.isPending}
        onCommit={() =>
          commitMutation.mutate({ staged: effectiveStaged, marked: markedIds })
        }
        onCancel={() => setCancelConfirmOpen(true)}
        onCategorize={(categoryId) =>
          bulkMutation.mutate({
            ids: bulkTargets,
            categoryId,
            alreadyCategorized: bulkAlreadyCategorizedCount,
            kindSkipped: bulkKindSkippedCount,
            skippedKind: bulkKind === "spend" ? "income" : "spend",
            cardPaymentSkipped: bulkCardPaymentSkippedCount,
          })
        }
      />

      <CancelImportConfirm
        open={cancelConfirmOpen}
        pendingCount={candidates.length}
        busy={cancelMutation.isPending}
        error={
          cancelMutation.isError
            ? cancelMutation.error instanceof ApiError
              ? cancelMutation.error.detail
              : "Couldn’t cancel — try again."
            : null
        }
        onOpenChange={setCancelConfirmOpen}
        onConfirm={() => cancelMutation.mutate()}
      />
    </>
  );
}

/**
 * Confirmation for Cancel import (UX-08) — the one irreversible action on this
 * screen, previously wired straight to the mutation. The copy tracks what the
 * backend actually does (imports.py cancel): the pending rows are HARD-deleted and a
 * full cancel drops the ImportBatch too, so re-uploading the statement is the only
 * recovery. Per-row discard is deliberately NOT behind a confirm — it's a soft,
 * reversible client-side mark that the commit label spells out.
 *
 * `Dialog`, not `AlertDialog`: there is no AlertDialog primitive in components/ui/,
 * and Dialog is the established confirm pattern (SelectionBar, ArchiveConfirm). It
 * inherits the focus-restore-to-opener fix; here the opener unmounts on success
 * (the route navigates), which is the documented degraded case — focus falls to
 * <body>, no exception.
 */
function CancelImportConfirm({
  open,
  pendingCount,
  busy,
  error,
  onOpenChange,
  onConfirm,
}: {
  open: boolean;
  pendingCount: number;
  busy: boolean;
  error: string | null;
  onOpenChange: (open: boolean) => void;
  onConfirm: () => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <DialogTitle>Cancel this import?</DialogTitle>
          <DialogDescription>
            This permanently deletes the {pendingCount}{" "}
            {pendingCount === 1 ? "row" : "rows"} still in this queue. Committed
            rows are unaffected — but the only way back is to upload the
            statement again.
          </DialogDescription>
        </DialogHeader>
        {error ? <p className="text-[12px] text-neg">{error}</p> : null}
        <DialogFooter>
          <Button
            variant="ghost"
            className="h-8 px-3 text-[12.5px]"
            onClick={() => onOpenChange(false)}
            disabled={busy}
          >
            Keep reviewing
          </Button>
          <Button
            variant="destructive"
            className="h-8 px-3 text-[12.5px]"
            onClick={onConfirm}
            disabled={busy}
          >
            {busy ? "Cancelling…" : "Cancel import"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// -----------------------------------------------------------------------------
function Header({
  pending,
  otherDefaultCount,
  cashbackDefaultCount,
  accountName,
  accountLast4,
}: {
  pending: number;
  otherDefaultCount: number;
  cashbackDefaultCount: number;
  accountName: string | null;
  accountLast4: string | null;
}) {
  return (
    <div className="pt-9 pb-5">
      <ImportStepper current="review" />
      <div className="mt-5 flex flex-wrap items-baseline gap-3">
        <h1 className="text-[22px] font-semibold leading-none tracking-[-0.014em] text-foreground">
          Review queue
        </h1>
        {/* A batch is single-account, so the account is shown once here rather
            than repeated on every row. */}
        {accountName ? (
          <span className="text-[12px] text-muted-foreground">
            <span className="text-foreground">{accountName}</span>
            {accountLast4 ? (
              <span className="tabular-nums text-muted-foreground/70">
                {" "}
                {accountLast4}
              </span>
            ) : null}
          </span>
        ) : null}
        <span className="text-[12px] tabular-nums text-muted-foreground">
          <span className="text-foreground">{pending}</span> pending
        </span>
        {otherDefaultCount > 0 ? (
          <span className="text-[12px] tabular-nums text-muted-foreground">
            · {otherDefaultCount} will import as Other
          </span>
        ) : null}
        {cashbackDefaultCount > 0 ? (
          <span className="text-[12px] tabular-nums text-muted-foreground">
            · {cashbackDefaultCount} will import as Cashback
          </span>
        ) : null}
      </div>
      <p className="mt-1 text-[11px] text-muted-foreground">
        j/k move · space stage · c category
      </p>
    </div>
  );
}

function ReviewRow({
  c,
  isLast,
  categoryName,
  categories,
  staged,
  defaultsToOther,
  marked,
  pending,
  discardDisabled,
  labelSaving,
  groupIds,
  focused,
  rowRef,
  pickerOpen,
  onPickerOpenChange,
  onToggle,
  onPickCategory,
  onPickCredit,
  onMarkCardPayment,
  onSelectGroup,
  onDiscard,
  onSaveLabels,
}: {
  c: TransactionCandidate;
  isLast: boolean;
  categoryName: string | null;
  categories: CategoryRead[];
  staged: boolean;
  defaultsToOther: boolean;
  marked: boolean;
  pending: boolean;
  /** Row-wide in-flight lock: a commit, a cancel or a bulk fan-out is running, so
   * discard, the tag editor and the stage checkbox are all inert. */
  discardDisabled: boolean;
  labelSaving: boolean;
  /** Every candidate id sharing this row's exact `merchant_raw` (incl. itself).
   * A singleton array when the merchant is unique or null — no `×N` renders. */
  groupIds: number[];
  /** Phase 6 keyboard loop: this row is the current `j`/`k` roving highlight.
   * Purely visual — never moves real DOM focus. */
  focused: boolean;
  /** Backs the parent's `scrollIntoView` when `j`/`k` moves the highlight off-screen. */
  rowRef: (el: HTMLLIElement | null) => void;
  /** Lifted out of row-local state (Phase 5 left it local; see the call site) so
   * the keyboard loop's `c` binding can open THIS row's popover from outside it. */
  pickerOpen: boolean;
  onPickerOpenChange: (open: boolean) => void;
  onToggle: (shiftKey: boolean) => void;
  onPickCategory: (categoryId: number | null) => void;
  onPickCredit: (type: "spend" | "income", categoryId: number) => void;
  onMarkCardPayment: () => void;
  onSelectGroup: (ids: number[]) => void;
  onDiscard: () => void;
  onSaveLabels: (labels: string[]) => void;
}) {
  // Sign is the source of truth (PRD §F4a): positive = credit (refund/income),
  // rendered green; negative = spend, shown as magnitude.
  const isCredit = c.amount_paise > 0;
  // Local draft of this row's tags; committed (PATCHed) once when the editor
  // popover closes (onCommit), not per keystroke. Seeded once — subsequent edits
  // flow draft → PATCH → setQueryData, keeping the cached candidate in sync.
  const [draftLabels, setDraftLabels] = useState<string[]>(() =>
    c.labels.map((l) => l.name),
  );
  // The category picker's popover — cmdk's CommandItem has no DropdownMenuItem-style
  // auto-close, so every pick below closes it explicitly. `pickerOpen` itself is
  // now parent-controlled (`onPickerOpenChange` prop) rather than row-local state,
  // so the Phase 6 keyboard loop's `c` binding can open THIS row's popover from
  // outside it.
  function selectCategory(categoryId: number | null) {
    onPickCategory(categoryId);
    onPickerOpenChange(false);
  }
  function selectCredit(type: "spend" | "income", categoryId: number) {
    onPickCredit(type, categoryId);
    onPickerOpenChange(false);
  }
  function selectCardPayment() {
    onMarkCardPayment();
    onPickerOpenChange(false);
  }
  // Radix's `onCheckedChange` carries no event, so shift-click range-select reads
  // the modifier off the click instead. This is safe ONLY because Radix composes the
  // caller's handler FIRST — `onClick: composeEventHandlers(onClick, …)` in
  // @radix-ui/react-checkbox's dist/index.mjs — so the ref is already set by the
  // time onCheckedChange fires. Reverse that composition order upstream and
  // range-select silently degrades to a plain toggle with no type error.
  const shiftRef = useRef(false);

  // The row's OWN transaction_type (set via the type picker below), not the
  // amount's sign, drives which categories are offered on a spend row — a
  // credit row uses the merged spend/income dropdown instead (`spendCategories`
  // + `incomeCategories` below).
  const visibleCategories = categories.filter(
    (cat) => cat.kind === categoryKindForType(c.transaction_type),
  );
  // Credit-row category options (PRD §F5): a merged spend + income list
  // restores the "which spend category did this refund reduce" choice a flat
  // Refund shortcut used to drop — precise netting no longer requires
  // re-tagging on the board afterwards. Picking a spend category makes the
  // row a positive `spend` (a refund, ADR-0009); picking an income category
  // makes it `income` (see `onPickCredit`).
  const spendCategories = categories.filter((cat) => cat.kind === "spend");
  const incomeCategories = categories.filter((cat) => cat.kind === "income");

  return (
    <li
      ref={rowRef}
      className={cn(
        "group grid items-center px-5 transition-colors duration-100 hover:bg-muted/50",
        isLast ? "" : "border-b border-border/70",
        marked && "opacity-55",
        // Phase 6 keyboard loop's roving highlight — purely visual, never moves
        // real DOM focus.
        focused && "ring-1 ring-ring ring-inset",
      )}
      style={{ gridTemplateColumns: GRID, columnGap: 16, minHeight: 56 }}
    >
      <span className="flex items-center">
        <Checkbox
          aria-label={`Stage ${c.merchant_raw}`}
          checked={staged}
          disabled={marked || discardDisabled}
          onClick={(e) => {
            shiftRef.current = e.shiftKey;
          }}
          onCheckedChange={() => onToggle(shiftRef.current)}
        />
      </span>

      <span
        className="whitespace-nowrap text-[12px] tabular-nums text-foreground/80"
        style={{ fontVariantNumeric: "tabular-nums lining-nums" }}
      >
        {formatDateWithYear(c.date)}
      </span>

      <div className="flex min-w-0 flex-col justify-center gap-0.5 py-2">
        {c.merchant_raw ? (
          <>
            <Tooltip>
              <TooltipTrigger asChild>
                <span
                  className={cn(
                    "cursor-default truncate text-[13px] font-medium text-foreground",
                    marked && "line-through",
                  )}
                  style={{ letterSpacing: "-0.005em" }}
                >
                  {c.merchant_raw}
                </span>
              </TooltipTrigger>
              {/* Styled, theme-aware tooltip (not the native `title`) so the full
                  raw merchant reads legibly when the cell truncates. */}
              <TooltipContent className="max-w-sm break-words">
                {c.merchant_raw}
              </TooltipContent>
            </Tooltip>
            {groupIds.length > 1 ? (
              <button
                type="button"
                disabled={discardDisabled}
                onClick={() => onSelectGroup(groupIds)}
                title={`Select all ${groupIds.length} rows from this merchant`}
                aria-label={`Select all ${groupIds.length} rows from this merchant`}
                className="self-start text-[11px] text-muted-foreground hover:bg-muted hover:text-foreground rounded-[4px] px-1 py-0.5"
              >
                ×{groupIds.length}
              </button>
            ) : null}
          </>
        ) : (
          <span className="text-[13px] text-muted-foreground">—</span>
        )}
      </div>

      <div className="flex min-w-0 items-center">
        <LabelInput
          compact
          value={draftLabels}
          onChange={setDraftLabels}
          disabled={labelSaving || discardDisabled}
          onCommit={(next) => {
            const saved = c.labels.map((l) => l.name);
            if (!sameLabelSet(next, saved)) onSaveLabels(next);
          }}
        />
      </div>

      {isCredit ? (
        <Popover open={pickerOpen} onOpenChange={onPickerOpenChange}>
          <PopoverTrigger asChild>
            <TagPicker
              confidence={c.confidence}
              categoryName={categoryName}
              defaultsToOther={defaultsToOther}
              defaultCategoryName={c.transaction_type === "spend" ? "Other" : "Cashback"}
              cardPayment={c.cc_payment_candidate && staged && c.category_id == null}
              priorMatches={c.prior_matches}
              pinned={c.pinned}
              disabled={pending}
              aria-busy={pending}
              className={cn(pending && "opacity-60")}
            />
          </PopoverTrigger>
          <PopoverContent align="start" className="w-56 p-0">
            <Command>
              <CommandInput placeholder="Search categories…" />
              <CommandList className="max-h-72">
                <CommandEmpty>No matching categories.</CommandEmpty>
                {c.cc_payment_candidate ? (
                  <>
                    <CommandGroup>
                      <CommandItem
                        value="__card_payment__"
                        onSelect={selectCardPayment}
                      >
                        <span className="flex flex-col gap-0.5">
                          <span>Card bill payment</span>
                          <span className="text-[11px] text-muted-foreground">
                            links to your bank debit at commit, if that
                            account is imported
                          </span>
                        </span>
                      </CommandItem>
                    </CommandGroup>
                    <CommandSeparator />
                  </>
                ) : null}
                <CommandGroup>
                  <CommandItem
                    value="__uncategorized__"
                    onSelect={() => selectCategory(null)}
                  >
                    <span className="text-muted-foreground">Uncategorized</span>
                  </CommandItem>
                </CommandGroup>
                <CommandSeparator />
                <CommandGroup heading="Spend (refund)">
                  {buildCategoryTree(spendCategories).map((parent) => (
                    <div key={parent.id} className="py-0.5">
                      <CommandItem
                        value={`spend-${parent.name}`}
                        onSelect={() => selectCredit("spend", parent.id)}
                        className="flex items-center gap-2 font-medium"
                      >
                        <CategoryDot
                          categoryId={parent.id}
                          color={resolveCategoryColor(parent, categories)}
                        />
                        <span className="truncate">{parent.name}</span>
                      </CommandItem>
                      {parent.subcategories.map((sub) => (
                        <CommandItem
                          key={sub.id}
                          value={`spend-${parent.name}-${sub.name}`}
                          onSelect={() => selectCredit("spend", sub.id)}
                          className="ml-3 flex items-center gap-2 border-l border-border/60 pl-3 text-[12px]"
                        >
                          <CategoryDot
                            categoryId={sub.id}
                            color={resolveCategoryColor(sub, categories)}
                            className="size-1.5"
                          />
                          <span className="truncate">{sub.name}</span>
                        </CommandItem>
                      ))}
                    </div>
                  ))}
                </CommandGroup>
                <CommandSeparator />
                <CommandGroup heading="Income">
                  {buildCategoryTree(incomeCategories).map((parent) => (
                    <div key={parent.id} className="py-0.5">
                      <CommandItem
                        value={`income-${parent.name}`}
                        onSelect={() => selectCredit("income", parent.id)}
                        className="flex items-center gap-2 font-medium"
                      >
                        <CategoryDot
                          categoryId={parent.id}
                          color={resolveCategoryColor(parent, categories)}
                        />
                        <span className="truncate">{parent.name}</span>
                      </CommandItem>
                      {parent.subcategories.map((sub) => (
                        <CommandItem
                          key={sub.id}
                          value={`income-${parent.name}-${sub.name}`}
                          onSelect={() => selectCredit("income", sub.id)}
                          className="ml-3 flex items-center gap-2 border-l border-border/60 pl-3 text-[12px]"
                        >
                          <CategoryDot
                            categoryId={sub.id}
                            color={resolveCategoryColor(sub, categories)}
                            className="size-1.5"
                          />
                          <span className="truncate">{sub.name}</span>
                        </CommandItem>
                      ))}
                    </div>
                  ))}
                </CommandGroup>
              </CommandList>
            </Command>
          </PopoverContent>
        </Popover>
      ) : (
        <Popover open={pickerOpen} onOpenChange={onPickerOpenChange}>
          <PopoverTrigger asChild>
            <TagPicker
              confidence={c.confidence}
              categoryName={categoryName}
              defaultsToOther={defaultsToOther}
              defaultCategoryName="Other"
              priorMatches={c.prior_matches}
              pinned={c.pinned}
              disabled={pending}
              aria-busy={pending}
              className={cn(pending && "opacity-60")}
            />
          </PopoverTrigger>
          <PopoverContent align="start" className="w-56 p-0">
            <Command>
              <CommandInput placeholder="Search categories…" />
              <CommandList className="max-h-72">
                <CommandEmpty>No matching categories.</CommandEmpty>
                <CommandGroup>
                  <CommandItem
                    value="__uncategorized__"
                    onSelect={() => selectCategory(null)}
                  >
                    <span className="text-muted-foreground">Uncategorized</span>
                  </CommandItem>
                </CommandGroup>
                <CommandSeparator />
                {buildCategoryTree(visibleCategories).map((parent) => (
                  <CommandGroup key={parent.id} className="p-0.5">
                    <CommandItem
                      value={`cat-${parent.name}`}
                      onSelect={() => selectCategory(parent.id)}
                      className="flex items-center gap-2 font-medium"
                    >
                      <CategoryDot
                        categoryId={parent.id}
                        color={resolveCategoryColor(parent, categories)}
                      />
                      <span className="truncate">{parent.name}</span>
                    </CommandItem>
                    {parent.subcategories.map((sub) => (
                      <CommandItem
                        key={sub.id}
                        value={`sub-${parent.name}-${sub.name}`}
                        onSelect={() => selectCategory(sub.id)}
                        className="ml-3 flex items-center gap-2 border-l border-border/60 pl-3 text-[12px]"
                      >
                        <CategoryDot
                          categoryId={sub.id}
                          color={resolveCategoryColor(sub, categories)}
                          className="size-1.5"
                        />
                        <span className="truncate">{sub.name}</span>
                      </CommandItem>
                    ))}
                  </CommandGroup>
                ))}
              </CommandList>
            </Command>
          </PopoverContent>
        </Popover>
      )}

      <span
        className={cn(
          "text-right text-[13px] font-medium tabular-nums",
          isCredit ? "text-pos" : "text-foreground",
          marked && "line-through",
        )}
        style={{
          fontFamily: "var(--font-jbmono), ui-monospace, monospace",
          fontVariantNumeric: "tabular-nums lining-nums",
          letterSpacing: "-0.012em",
        }}
      >
        <Sensitive>
          {isCredit ? "+" : ""}
          {formatINR(Math.abs(c.amount_paise))}
        </Sensitive>
      </span>

      <button
        type="button"
        aria-label={
          marked ? `Restore ${c.merchant_raw}` : `Discard ${c.merchant_raw}`
        }
        title={marked ? "Restore — keep this row" : "Remove on commit"}
        disabled={discardDisabled}
        onClick={onDiscard}
        className={cn(
          "flex size-6 items-center justify-center rounded-[4px] transition-opacity",
          marked
            ? // Marked rows keep the affordance visible so the row reads as "will
              // be removed, click to restore".
              "text-neg opacity-100 hover:bg-muted"
            : // Faint at rest (not opacity-0) so it stays visible + tappable on
              // touch, where there's no hover; lifts to full on hover/focus.
              // Resting opacity stays above disabled:opacity-40 to read distinctly.
              "text-muted-foreground/60 opacity-60 hover:bg-muted hover:text-neg focus-visible:opacity-100 group-hover:opacity-100",
          "disabled:pointer-events-none disabled:opacity-40",
        )}
      >
        {marked ? (
          <IconRefresh className="size-3.5" />
        ) : (
          <IconX className="size-3.5" />
        )}
      </button>
    </li>
  );
}

/**
 * Fixed footer: the queue's one action surface (UX-07 / ADR-0008's v1
 * mitigation). Carries the Categorize control too, as of Phase 7 — it used to
 * live in a separately-mounted `BulkBar` that appeared above the table the
 * moment a row was staged, pushing every row down. Folded in here instead,
 * since this bar is already always mounted and fixed to the bottom, so
 * staging a row no longer shifts anything.
 *
 * The Categorize dropdown is NOT `SelectionBar` — three verified blockers
 * rule reuse out: SelectionBar is `fixed bottom-6 z-30` and would overlap
 * this bar's own `fixed bottom-0 h-16 z-30`; it owns a bulk Delete the queue
 * already covers twice (per-row discard + Cancel import); and it calls
 * `invalidateRules` unconditionally, which is wrong here — transactions.py
 * gates learning on `confirmed_at IS NOT NULL`, so a pending-row PATCH
 * teaches nothing. Only the mutation shape and the `role="toolbar"`
 * semantics are mirrored.
 *
 * No bulk TAG action: PRD.md:262 puts bulk apply out of scope for v1 labels.
 */
function CommitBar({
  stagedCount,
  markedCount,
  pendingCount,
  busy,
  cancelBusy,
  categories,
  categoryKind,
  bulkTargetCount,
  bulkAlreadyCategorizedCount,
  bulkCardPaymentSkippedCount,
  bulkBusy,
  onCommit,
  onCancel,
  onCategorize,
}: {
  stagedCount: number;
  markedCount: number;
  pendingCount: number;
  busy: boolean;
  cancelBusy: boolean;
  categories: CategoryRead[];
  categoryKind: CategoryKind;
  bulkTargetCount: number;
  bulkAlreadyCategorizedCount: number;
  bulkCardPaymentSkippedCount: number;
  bulkBusy: boolean;
  onCommit: () => void;
  onCancel: () => void;
  onCategorize: (categoryId: number) => void;
}) {
  const remaining = pendingCount - stagedCount - markedCount;
  const visibleCategories = categories.filter((c) => c.kind === categoryKind);
  const bulkSkippedHint =
    bulkAlreadyCategorizedCount > 0 || bulkCardPaymentSkippedCount > 0
      ? [
          bulkAlreadyCategorizedCount > 0
            ? `${bulkAlreadyCategorizedCount} already categorized`
            : null,
          bulkCardPaymentSkippedCount > 0
            ? `${bulkCardPaymentSkippedCount} card payment skipped`
            : null,
        ]
          .filter(Boolean)
          .join(" · ")
      : null;
  // Label reflects both actions Commit performs; the destructive part is always
  // spelled out so it's never a silent side effect.
  const commitLabel =
    stagedCount > 0 && markedCount > 0
      ? `Commit ${stagedCount} · remove ${markedCount}`
      : stagedCount > 0
        ? `Commit ${stagedCount} ${stagedCount === 1 ? "row" : "rows"} to board`
        : `Remove ${markedCount} ${markedCount === 1 ? "row" : "rows"}`;
  return (
    <div
      // Left inset clears the 200px sidebar + 40px gutter at md+ (where the
      // sidebar is present); below md the sidebar collapses to a hamburger, so
      // it drops to just the gutter. Right inset is the gutter at all widths.
      className="fixed inset-x-0 bottom-0 z-30 flex h-16 items-center justify-between border-t border-border bg-background/85 pl-10 pr-10 backdrop-blur-md md:pl-[240px]"
    >
      <div className="flex items-center gap-4 text-[12px]">
        <span className="tabular-nums text-foreground/80">
          <span className="font-medium text-foreground">{stagedCount}</span>
          <span className="text-muted-foreground"> ready to commit</span>
        </span>
        {markedCount > 0 ? (
          <>
            <span className="text-muted-foreground/60">·</span>
            <span className="tabular-nums text-neg">
              {markedCount} to remove
            </span>
          </>
        ) : null}
        <span className="text-muted-foreground/60">·</span>
        <span className="text-muted-foreground">{remaining} stay in queue</span>
      </div>

      <div role="toolbar" aria-label="Bulk actions" className="flex items-center gap-2">
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              type="button"
              variant="outline"
              title={bulkSkippedHint ?? undefined}
              disabled={
                stagedCount === 0 ||
                bulkTargetCount === 0 ||
                visibleCategories.length === 0 ||
                busy ||
                cancelBusy
              }
              className="h-9 px-3 text-[12px] font-medium"
            >
              {bulkBusy ? "Categorizing…" : "Categorize"}
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" side="top" className="max-h-72 w-56 overflow-y-auto">
            {bulkSkippedHint ? (
              <div className="px-2 py-1.5 text-[11px] text-muted-foreground">
                {bulkSkippedHint}
              </div>
            ) : null}
            {buildCategoryTree(visibleCategories).map((parent) => (
              <div key={parent.id} className="py-0.5">
                <DropdownMenuItem
                  onSelect={() => onCategorize(parent.id)}
                  className="flex items-center gap-2 font-medium"
                >
                  <CategoryDot
                    categoryId={parent.id}
                    color={resolveCategoryColor(parent, categories)}
                  />
                  <span className="truncate">{parent.name}</span>
                </DropdownMenuItem>
                {parent.subcategories.map((sub) => (
                  <DropdownMenuItem
                    key={sub.id}
                    onSelect={() => onCategorize(sub.id)}
                    className="ml-3 flex items-center gap-2 border-l border-border/60 pl-3 text-[12px]"
                  >
                    <CategoryDot
                      categoryId={sub.id}
                      color={resolveCategoryColor(sub, categories)}
                      className="size-1.5"
                    />
                    <span className="truncate">{sub.name}</span>
                  </DropdownMenuItem>
                ))}
              </div>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
        <Button
          variant="outline"
          className="h-9 px-3 text-[12px] font-medium"
          onClick={onCancel}
          disabled={cancelBusy || busy}
        >
          {cancelBusy ? "Cancelling…" : "Cancel import"}
        </Button>
        <Button
          type="button"
          disabled={
            (stagedCount === 0 && markedCount === 0) || busy || cancelBusy
          }
          onClick={onCommit}
          className="h-9 gap-1.5 px-3.5 text-[12px] font-medium"
        >
          <IconArrowRight className="size-3.5" />
          <span>{commitLabel}</span>
        </Button>
      </div>
    </div>
  );
}

function Centered({
  children,
  tone,
}: {
  children: React.ReactNode;
  tone?: "error";
}) {
  return (
    <div
      className={cn(
        "rounded-[6px] border border-border bg-card px-4 py-16 text-center text-[13px]",
        tone === "error" ? "text-neg" : "text-muted-foreground",
      )}
    >
      {children}
    </div>
  );
}

function NavLink({
  href,
  children,
}: {
  href: string;
  children: React.ReactNode;
}) {
  return (
    <Link href={href} className="font-medium text-primary hover:underline">
      {children}
    </Link>
  );
}
