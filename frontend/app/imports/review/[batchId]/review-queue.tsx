"use client";

/**
 * Live import-review queue — the data half of /imports/review/[batchId]. The
 * server shell (page.tsx) owns theme/chrome; this island fetches the
 * batch's pending candidates and drives the tag → stage → commit loop.
 *
 * Contract notes that shape the logic (PRD §F1/§F3/§F9):
 *  - candidates are rows with confirmed_at IS NULL; committing removes them, so
 *    there is no "confirmed" group here.
 *  - staging is category-based: a row with a category is selected by default;
 *    untagged rows start unchecked. A staged untagged spend/refund commits under
 *    the seeded "Other" category (the backend defaults it); income stays null.
 *  - propagation is invalidate-on-mutation (no SSE): a category PATCH refreshes
 *    this batch; a commit refreshes the board.
 */
import { useRef, useState } from "react";
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
import { TagPicker } from "@/components/review/tag-picker";
import { LabelInput } from "@/components/labels/label-input";
import { ImportStepper } from "@/components/ui/stepper";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { Sensitive } from "@/components/balance-visibility";
import { IconArrowRight, IconRefresh, IconX } from "@/components/icons";
import {
  ApiError,
  cancelImport,
  commitImport,
  deleteTransaction,
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
import { categoryKindForType } from "@/lib/categories";
import { formatINR, formatDateWithYear } from "@/lib/format";
import { sameLabelSet } from "@/lib/labels";
import { invalidateRules } from "@/lib/queries/invalidate";
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

/**
 * A staged row with no category commits under the seeded spend "Other" category
 * — but only spend/refund. Income stays uncategorized (an auto-linked CC payment
 * must become a category-null transfer), so it never defaults to Other. Drives
 * the "will import as Other" hint + the picker's default label; the backend
 * applies the actual category at commit.
 */
function defaultsToOther(c: TransactionCandidate): boolean {
  return (
    c.category_id == null &&
    (c.transaction_type === "spend" || c.transaction_type === "refund")
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

  const candidatesQuery = useQuery({
    queryKey: ["candidates", batchId],
    queryFn: () => listCandidates(batchId),
  });
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
  // A batch is single-account (ImportBatch.account_id) — every row shares it, so
  // the account is resolved once from the first candidate and shown in the header
  // rather than repeated per row.
  const batchAccount = accountsById.get(candidates[0]?.account_id ?? -1);
  // Checkbox state is category-based: a row is default-staged iff it has a
  // category (auto-tagged spends, or anything the user tags — incl. Cashback).
  // Untagged rows (spends AND credits) start unchecked. `toggled` holds the ids
  // the user flipped away from that default, so the effective set derives at
  // render with no reconcile effect. Tagging a row auto-selects it: after the
  // category PATCH refetch it carries a category → default-staged.
  const isStaged = (c: TransactionCandidate): boolean =>
    (c.category_id != null) !== toggled.has(c.id);
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
  // Staged spend/refund rows without a category commit under "Other" (backend).
  const otherDefaultCount = candidates.filter(
    (c) => isEffectivelyStaged(c) && defaultsToOther(c),
  ).length;
  // Header select-all. The `stagedCount > 0` conjunct is load-bearing: with every
  // row marked for deletion the counts would read `candidates.length > 0 && 0 === 0`
  // → the box would show fully checked at zero staged.
  const allStaged =
    stagedCount > 0 && stagedCount === candidates.length - markedCount;

  // --- Bulk categorize target set (D8/D9) -----------------------------------
  // D8: one selection set — the staged rows, board parity — with the overwrite
  // count surfaced rather than a hidden skip-already-categorized rule.
  const stagedCandidates = candidates.filter((c) => isEffectivelyStaged(c));
  // D9 mixed-type selection: the picker offers spend categories when the selection
  // holds any spend-kind row (spend/refund/transfer), else income. Rows of the
  // other kind are SKIPPED, not failed — `_assert_category_id_or_422`
  // (transactions.py) rejects a kind mismatch, so PATCHing them would 422.
  const bulkKind: CategoryKind = stagedCandidates.some(
    (c) => categoryKindForType(c.transaction_type) === "spend",
  )
    ? "spend"
    : "income";
  const bulkTargets = stagedCandidates
    .filter((c) => categoryKindForType(c.transaction_type) === bulkKind)
    .map((c) => c.id);
  const bulkSkippedCount = stagedCandidates.length - bulkTargets.length;
  // Overwrite count (D8), counted over the whole selection so it shares a base
  // with the "N selected" it sits beside.
  const bulkOverwriteCount = stagedCandidates.filter(
    (c) => c.category_id != null,
  ).length;

  // Force a set of ids to an ABSOLUTE staged state. Staging is derived — `toggled`
  // holds the ids flipped away from the category-based default — so forcing a state
  // means storing a flip only when the wanted state differs from that default: a
  // default-staged (categorized) row flips ON in `toggled` to go OFF; an untagged
  // row flips ON to go ON. Used by select-all / range-select and the label-save
  // auto-stage (`on: true`), and by commit success/error via `forceUnstage`.
  const setStagedFor = (ids: Iterable<number>, on: boolean) =>
    setToggled((prev) => {
      const next = new Set(prev);
      for (const id of ids) {
        const c = candidatesById.get(id);
        if (!c) continue;
        const defaultStaged = c.category_id != null;
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
    onMutate: (vars) => setPendingRowIds((prev) => new Set(prev).add(vars.id)),
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
    },
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
  // `skipped` / `skippedKind` ride along as mutation variables (click-time snapshot,
  // like commitMutation's sets) so the notice can't be written from a set that the
  // post-PATCH refetch has already moved on from.
  const bulkMutation = useMutation({
    mutationFn: async (vars: {
      ids: number[];
      categoryId: number;
      skipped: number;
      skippedKind: CategoryKind;
    }) => {
      const updated: number[] = [];
      let failed = 0;
      for (let i = 0; i < vars.ids.length; i += BULK_CHUNK) {
        const chunk = vars.ids.slice(i, i + BULK_CHUNK);
        const results = await Promise.allSettled(
          chunk.map((id) =>
            patchTransaction(id, { category_id: vars.categoryId }),
          ),
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
      setBulkNotice(
        `${updated.length} updated · ${vars.skipped} ${vars.skippedKind} ${
          vars.skipped === 1 ? "row" : "rows"
        } skipped · ${failed} failed`,
      );
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

  function saveLabels(id: number, labels: string[]) {
    labelMutation.mutate({ id, labels });
  }

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

      {stagedCount > 0 ? (
        <BulkBar
          selectedCount={stagedCount}
          overwriteCount={bulkOverwriteCount}
          categories={categories}
          categoryKind={bulkKind}
          busy={bulkMutation.isPending}
          onCategorize={(categoryId) =>
            bulkMutation.mutate({
              ids: bulkTargets,
              categoryId,
              skipped: bulkSkippedCount,
              skippedKind: bulkKind === "spend" ? "income" : "spend",
            })
          }
        />
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
                  onToggle={(shiftKey) => toggleRow(c.id, i, shiftKey)}
                  onPickCategory={(categoryId) => setCategory(c.id, categoryId)}
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
        onCommit={() =>
          commitMutation.mutate({ staged: effectiveStaged, marked: markedIds })
        }
        onCancel={() => setCancelConfirmOpen(true)}
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
  accountName,
  accountLast4,
}: {
  pending: number;
  otherDefaultCount: number;
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
      </div>
    </div>
  );
}

/**
 * Bulk-category bar for the staged rows (UX-07 / ADR-0008's v1 mitigation). Local
 * to this file and NOT `SelectionBar` — three verified blockers rule reuse out:
 * SelectionBar is `fixed bottom-6 z-30` and would overlap the queue's own
 * `fixed bottom-0 h-16 z-30` CommitBar; it owns a bulk Delete the queue already
 * covers twice (per-row discard + Cancel import); and it calls `invalidateRules`
 * unconditionally, which is wrong here — transactions.py gates learning on
 * `confirmed_at IS NOT NULL`, so a pending-row PATCH teaches nothing. Only the
 * mutation shape and the `role="toolbar"` semantics are mirrored.
 *
 * No bulk TAG action: PRD.md:262 puts bulk apply out of scope for v1 labels.
 */
function BulkBar({
  selectedCount,
  overwriteCount,
  categories,
  categoryKind,
  busy,
  onCategorize,
}: {
  selectedCount: number;
  overwriteCount: number;
  categories: CategoryRead[];
  categoryKind: CategoryKind;
  busy: boolean;
  onCategorize: (categoryId: number) => void;
}) {
  const visibleCategories = categories.filter((c) => c.kind === categoryKind);
  return (
    <div
      role="toolbar"
      aria-label="Bulk actions"
      className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-2 rounded-[6px] border border-border bg-muted/40 px-3 py-2"
    >
      <span className="text-[12px] tabular-nums text-muted-foreground">
        <span className="font-medium text-foreground">{selectedCount}</span>{" "}
        selected
        {/* The overwrite count is shown rather than silently skipping rows that
            already have a category — a hidden skip reads as a bug (D8). */}
        {overwriteCount > 0 ? ` · ${overwriteCount} already categorized` : null}
      </span>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            type="button"
            variant="outline"
            disabled={busy || visibleCategories.length === 0}
            className="h-8 px-3 text-[12px] font-medium"
          >
            {busy ? "Categorizing…" : "Categorize"}
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="max-h-72 w-56">
          {visibleCategories.map((cat) => (
            <DropdownMenuItem
              key={cat.id}
              onSelect={() => onCategorize(cat.id)}
            >
              {cat.name}
            </DropdownMenuItem>
          ))}
        </DropdownMenuContent>
      </DropdownMenu>
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
  onToggle,
  onPickCategory,
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
  onToggle: (shiftKey: boolean) => void;
  onPickCategory: (categoryId: number | null) => void;
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
  // Radix's `onCheckedChange` carries no event, so shift-click range-select reads
  // the modifier off the click instead. This is safe ONLY because Radix composes the
  // caller's handler FIRST — `onClick: composeEventHandlers(onClick, …)` in
  // @radix-ui/react-checkbox's dist/index.mjs — so the ref is already set by the
  // time onCheckedChange fires. Reverse that composition order upstream and
  // range-select silently degrades to a plain toggle with no type error.
  const shiftRef = useRef(false);

  // Picker options are kind-filtered to the row's transaction type — income rows
  // draw income categories, spend/refund draw spend (transfer falls back to
  // spend, but commits category-null regardless). Mirrors the other four
  // pickers; without it the merged list shows both scopes' "Other". Name lookup
  // still uses the parent's full-list map (categoriesById).
  const visibleCategories = categories.filter(
    (cat) => cat.kind === categoryKindForType(c.transaction_type),
  );

  return (
    <li
      className={cn(
        "group grid items-center px-5 transition-colors duration-100 hover:bg-muted/50",
        isLast ? "" : "border-b border-border/70",
        marked && "opacity-55",
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

      <div className="flex min-w-0 flex-col justify-center py-2">
        {c.merchant_raw ? (
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

      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <TagPicker
            confidence={c.confidence}
            categoryName={categoryName}
            defaultsToOther={defaultsToOther}
            priorMatches={c.prior_matches}
            pinned={c.pinned}
            disabled={pending}
            aria-busy={pending}
            className={cn(pending && "opacity-60")}
          />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="max-h-72 w-56">
          <DropdownMenuItem onSelect={() => onPickCategory(null)}>
            <span className="text-muted-foreground">Uncategorized</span>
          </DropdownMenuItem>
          {visibleCategories.map((cat) => (
            <DropdownMenuItem
              key={cat.id}
              onSelect={() => onPickCategory(cat.id)}
            >
              {cat.name}
            </DropdownMenuItem>
          ))}
        </DropdownMenuContent>
      </DropdownMenu>

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

function CommitBar({
  stagedCount,
  markedCount,
  pendingCount,
  busy,
  cancelBusy,
  onCommit,
  onCancel,
}: {
  stagedCount: number;
  markedCount: number;
  pendingCount: number;
  busy: boolean;
  cancelBusy: boolean;
  onCommit: () => void;
  onCancel: () => void;
}) {
  const remaining = pendingCount - stagedCount - markedCount;
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

      <div className="flex items-center gap-2">
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
