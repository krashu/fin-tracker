"use client";

/**
 * Bulk-action bar for /expenses — floats when rows are selected. Owns its
 * mutations (board owns the ids). Both ops are N parallel calls via
 * Promise.allSettled (no bulk endpoint), invalidating the ["transactions"]
 * prefix on completion (PRD §F9). Mirrors the review-queue commit bar: triggers
 * disable while in flight; full success clears the selection (unmounts the bar),
 * a partial failure keeps it open with an inline count.
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
  deleteTransaction,
  patchTransaction,
  type CategoryKind,
  type CategoryRead,
} from "@/lib/api/client";
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

  // Shared completion handler: resync the board, then either finish (full
  // success → clear selection) or surface a partial-failure count. A delete
  // that 404s is the desired end state, so it counts as success.
  function settle(
    results: PromiseSettledResult<unknown>[],
    verb: string,
    treat404AsSuccess: boolean,
  ) {
    queryClient.invalidateQueries({ queryKey: ["transactions"] });
    // The SummaryStrip's month total depends on these rows (PRD §F9). A mounted
    // dashboards query won't refetch on staleTime alone, so invalidate it here.
    queryClient.invalidateQueries({ queryKey: ["dashboards"] });
    // Bulk categorize hits the SAME PATCH endpoint as the single-row dialog, and
    // transactions.py runs learn_merchant_memory on every one of the N calls — so
    // ["rules"] and ["candidates"] go stale exactly as they would for one row. This
    // was the one board-write site of four missing it, against the contract stated in
    // invalidate.ts. Called unconditionally to match the other three: on delete it
    // costs a refetch of two small queries and keeps the sites identical.
    invalidateRules(queryClient);
    const failed = results.filter((r) => {
      if (r.status === "fulfilled") return false;
      if (
        treat404AsSuccess &&
        r.reason instanceof ApiError &&
        r.reason.status === 404
      )
        return false;
      return true;
    }).length;
    if (failed === 0) {
      setBanner(null);
      onDone();
    } else {
      setBanner(
        `${ids.length - failed} of ${ids.length} ${verb}, ${failed} failed.`,
      );
    }
  }

  const categorize = useMutation({
    mutationFn: (categoryId: number) =>
      Promise.allSettled(
        ids.map((id) => patchTransaction(id, { category_id: categoryId })),
      ),
    onSuccess: (results) => settle(results, "updated", false),
  });

  const remove = useMutation({
    mutationFn: () =>
      Promise.allSettled(ids.map((id) => deleteTransaction(id))),
    onSuccess: (results) => {
      setConfirmOpen(false);
      settle(results, "deleted", true);
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
            <SelectionAction disabled={categorize.isPending}>
              {categorize.isPending ? "Categorizing…" : "Categorize"}
            </SelectionAction>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" className="max-h-72 w-48">
            {visibleCategories.map((c) => (
              <DropdownMenuItem
                key={c.id}
                onSelect={() => categorize.mutate(c.id)}
              >
                {c.name}
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>

        <span aria-hidden className="mx-0.5 h-5 w-px bg-bar-foreground/15" />

        <SelectionAction
          variant="danger"
          onClick={() => setConfirmOpen(true)}
          disabled={remove.isPending}
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
