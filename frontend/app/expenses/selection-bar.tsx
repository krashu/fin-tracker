"use client";

/**
 * Bulk-action bar for /expenses — floats when rows are selected. Owns its
 * mutations (board owns the ids).
 *
 * Categorize and Delete are each ONE all-or-nothing request, capped at BULK_MAX rows
 * (the triggers disable above the cap): POST /transactions/bulk-categorise teaches once
 * per canonical merchant (ADR-0004 rule 4); POST /transactions/bulk-delete deletes each
 * row as a single DELETE would (a linked partner outside the selection gets its pre-link
 * type and category back, ADR-0002 rule 9). A 422 changes nothing and the banner says how
 * many rows were refused; the refetch then drops vanished rows from the selection.
 *
 * Both invalidate ["transactions"] (PRD §F9) and ["dashboards"]. Triggers disable
 * while in flight; full success clears the selection (unmounts the bar).
 */
import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
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
import { IconX } from "@/components/icons";
import {
  ApiError,
  BULK_MAX,
  bulkCategorise,
  bulkDeleteTransactions,
  invalidIdsFromError,
  type CategoryKind,
  type CategoryRead,
} from "@/lib/api/client";
import {
  buildCategoryTree,
} from "@/lib/categories";
import { CategoryDot } from "@/components/category-dot";
import { invalidateRules } from "@/lib/queries/invalidate";
import { cn } from "@/lib/utils";

export function SelectionBar({
  ids,
  categories,
  categoryKind,
  onClear,
  onDone,
}: {
  ids: number[];
  categories: CategoryRead[];
  // Bulk-recategorize offers only categories matching the board's active type
  // view, so a spending selection can't be tagged with an income category.
  categoryKind: CategoryKind;
  onClear: () => void;
  onDone: () => void;
}) {
  const visibleCategories = categories.filter((c) => c.kind === categoryKind);
  const queryClient = useQueryClient();
  const [banner, setBanner] = useState<string | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);

  // The board and the SummaryStrip's month total depend on these rows (PRD §F9). A
  // mounted dashboards query won't refetch on staleTime alone, so invalidate it. Runs
  // on settle: a 422 must still refetch the board, since the rows may have changed.
  function resync() {
    queryClient.invalidateQueries({ queryKey: ["transactions"] });
    queryClient.invalidateQueries({ queryKey: ["dashboards"] });
    // Bulk categorise teaches (learn_merchant_memory), so ["rules"] and
    // ["candidates"] go stale as they would for one row (invalidate.ts contract).
    invalidateRules(queryClient);
  }

  const overCap = ids.length > BULK_MAX;

  const categorize = useMutation({
    mutationFn: (categoryId: number) =>
      bulkCategorise({ transaction_ids: ids, category_id: categoryId }),
    onSettled: resync,
    onSuccess: () => {
      setBanner(null);
      onDone();
    },
    onError: (err) => {
      const bad = invalidIdsFromError(err);
      setBanner(
        bad.length > 0
          ? `Nothing changed: ${bad.length} of ${ids.length} rows can't be categorised.`
          : err instanceof ApiError
            ? err.detail
            : "Couldn't categorise — try again.",
      );
    },
  });

  const remove = useMutation({
    mutationFn: () => bulkDeleteTransactions({ transaction_ids: ids }),
    onSettled: () => {
      setConfirmOpen(false);
      resync();
    },
    onSuccess: () => {
      setBanner(null);
      onDone();
    },
    onError: (err) => {
      const bad = invalidIdsFromError(err);
      setBanner(
        bad.length > 0
          ? `Nothing deleted: ${bad.length} of ${ids.length} rows no longer exist.`
          : err instanceof ApiError
            ? err.detail
            : "Couldn't delete — try again.",
      );
    },
  });

  return (
    <>
      {banner ? (
        <div className="fixed bottom-[4.75rem] left-1/2 z-30 -translate-x-1/2 rounded-md bg-bar px-3 py-1.5 text-[12px] text-bar-foreground shadow-[0_18px_40px_-16px_rgb(0_0_0/0.45)]">
          {banner}
        </div>
      ) : null}

      <div
        role="toolbar"
        aria-label="Bulk actions"
        className="fixed bottom-6 left-1/2 z-30 flex h-11 -translate-x-1/2 items-center gap-1 rounded-xl bg-bar pl-1 pr-1.5 text-bar-foreground shadow-[0_18px_40px_-16px_rgb(0_0_0/0.45)]"
      >
        <div
          className="flex h-8 items-center gap-1.5 rounded-lg px-2.5"
          style={{
            background:
              "linear-gradient(180deg, color-mix(in oklab, var(--primary) 25%, transparent) 0%, color-mix(in oklab, var(--primary) 14%, transparent) 100%)",
            boxShadow:
              "inset 0 0 0 1px color-mix(in oklab, var(--primary) 38%, transparent)",
          }}
        >
          <span className="grid size-4 min-w-4 place-items-center rounded bg-primary px-0.5 text-[10px] font-semibold text-primary-foreground">
            {ids.length}
          </span>
          <span
            className="text-[12px] font-medium text-bar-foreground/90"
            style={{ letterSpacing: "-0.005em" }}
          >
            selected
          </span>
        </div>

        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <SelectionAction
              disabled={categorize.isPending || overCap}
              title={
                overCap
                  ? `Select at most ${BULK_MAX} rows to categorise`
                  : undefined
              }
            >
              {categorize.isPending ? "Categorizing…" : "Categorize"}
            </SelectionAction>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" className="max-h-72 w-56 overflow-y-auto">
            {buildCategoryTree(visibleCategories).map((parent) => (
              <div key={parent.id} className="py-0.5">
                <DropdownMenuItem
                  onSelect={() => categorize.mutate(parent.id)}
                  className="flex items-center gap-2 font-medium"
                >
                  <CategoryDot
                    categoryId={parent.id}
                    color={parent.display_color}
                  />
                  <span className="truncate">{parent.name}</span>
                </DropdownMenuItem>
                {parent.subcategories.map((sub) => (
                  <DropdownMenuItem
                    key={sub.id}
                    onSelect={() => categorize.mutate(sub.id)}
                    className="ml-3 flex items-center gap-2 border-l border-border/60 pl-3 text-[12px]"
                  >
                    <CategoryDot
                      categoryId={sub.id}
                      color={sub.display_color}
                      className="size-1.5"
                    />
                    <span className="truncate">{sub.name}</span>
                  </DropdownMenuItem>
                ))}
              </div>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>

        <span aria-hidden className="mx-0.5 h-5 w-px bg-bar-foreground/15" />

        <SelectionAction
          variant="danger"
          onClick={() => setConfirmOpen(true)}
          disabled={remove.isPending || overCap}
          title={overCap ? `Select at most ${BULK_MAX} rows to delete` : undefined}
        >
          Delete
        </SelectionAction>

        <button
          type="button"
          onClick={onClear}
          className="ml-0.5 grid size-7 place-items-center rounded-lg text-bar-foreground/60 transition-colors hover:bg-white/10 hover:text-bar-foreground"
          aria-label="Clear selection"
        >
          <IconX className="size-3.5" />
        </button>
      </div>

      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle>
              Delete {ids.length}{" "}
              {ids.length === 1 ? "transaction" : "transactions"}?
            </DialogTitle>
            <DialogDescription>
              This permanently removes {ids.length === 1 ? "it" : "them"} — it
              can’t be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="ghost"
              className="h-8 px-3 text-[12.5px]"
              onClick={() => setConfirmOpen(false)}
              disabled={remove.isPending}
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              className="h-8 px-3 text-[12.5px]"
              onClick={() => remove.mutate()}
              disabled={remove.isPending}
            >
              {remove.isPending ? "Deleting…" : "Delete"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

function SelectionAction({
  children,
  variant,
  ...props
}: React.ComponentProps<"button"> & { variant?: "danger" }) {
  return (
    <button
      type="button"
      className={cn(
        "flex h-8 items-center rounded-lg px-2.5 text-[12px] font-medium transition-colors disabled:opacity-50",
        variant === "danger"
          ? "text-[oklch(78%_0.16_25)]"
          : "text-bar-foreground/85",
      )}
      {...props}
    >
      <span className="rounded-md px-1.5 py-1 transition-colors hover:bg-white/10">
        {children}
      </span>
    </button>
  );
}
