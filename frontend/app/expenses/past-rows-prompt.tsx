"use client";

/**
 * "Also move N recent <merchant> rows to <category>?" (ADR-0004 rules 2b and 4): the offer
 * that follows a category correction on the board. A non-modal card at the bottom-right,
 * so the board stays usable behind it.
 *
 * It is a SNAPSHOT of the moment of correction. The query key sits outside the
 * ["transactions"] prefix, with an infinite stale time and no refetch on focus or
 * reconnect, so a later board write (a bulk categorise, another edit) can never refetch it
 * and resurrect an offer the first answer did not make. `correctionSeq` plus `gcTime: 0`
 * make a second correction of the same row start a fresh snapshot. The card unmounts, via
 * `onDone`, when the first answer is empty or errors, and on Apply or Not now. Rows that
 * changed in between are caught by bulk categorise's all-or-nothing 422.
 */
import { useEffect } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import {
  ApiError,
  bulkCategorise,
  getPastRows,
  invalidIdsFromError,
  type CategoryRead,
} from "@/lib/api/client";
import { categoryLabel } from "@/lib/categories";
import { invalidateRules } from "@/lib/queries/invalidate";

export function PastRowsPrompt({
  sourceId,
  correctionSeq,
  categories,
  onDone,
}: {
  sourceId: number;
  correctionSeq: number;
  categories: CategoryRead[];
  onDone: () => void;
}) {
  const queryClient = useQueryClient();
  const pastRows = useQuery({
    queryKey: ["past-rows", sourceId, correctionSeq],
    queryFn: () => getPastRows(sourceId),
    staleTime: Infinity,
    gcTime: 0,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });

  const data = pastRows.data;
  const nothingToOffer =
    data != null && (data.transaction_ids.length === 0 || data.category_id == null);
  const gone = nothingToOffer || pastRows.isError;
  useEffect(() => {
    if (gone) onDone();
  }, [gone, onDone]);

  const apply = useMutation({
    mutationFn: () => {
      // The button only renders once `data` has a category.
      return bulkCategorise({
        transaction_ids: data!.transaction_ids,
        category_id: data!.category_id!,
      });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["transactions"] });
      queryClient.invalidateQueries({ queryKey: ["dashboards"] });
      invalidateRules(queryClient);
      onDone();
    },
  });

  if (data == null || gone) return null;

  const n = data.transaction_ids.length;
  const error = apply.error;
  return (
    <section
      role="region"
      aria-label="Apply to past rows"
      className="fixed bottom-6 right-6 z-30 w-80 rounded-xl border border-border bg-card p-3 text-[12.5px] shadow-lg"
    >
      <p className="text-foreground">
        Also move {n} recent {data.merchant} {n === 1 ? "row" : "rows"} to{" "}
        {categoryLabel(data.category_id, categories)}?
      </p>
      {error ? (
        <p className="mt-1.5 text-[12px] text-neg">
          {invalidIdsFromError(error).length > 0
            ? "Some of those rows changed. Nothing was moved."
            : error instanceof ApiError
              ? error.detail
              : "Couldn’t apply — try again."}
        </p>
      ) : null}
      <div className="mt-2 flex justify-end gap-2">
        <Button
          type="button"
          variant="ghost"
          className="h-8 px-3 text-[12.5px]"
          onClick={onDone}
          disabled={apply.isPending}
        >
          Not now
        </Button>
        <Button
          type="button"
          className="h-8 px-3 text-[12.5px]"
          onClick={() => apply.mutate()}
          disabled={apply.isPending}
        >
          {apply.isPending ? "Applying…" : "Apply"}
        </Button>
      </div>
    </section>
  );
}
