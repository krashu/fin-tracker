"use client";

/**
 * "New rule" dialog for /settings/rules — author (pin) a merchant→category and/or
 * merchant→tag rule (PRD §F3 / §F3a). A pinned rule always wins the prefill
 * regardless of hit_count, so it can't be silently overturned by future imports.
 *
 * Merchant entry is a combobox over the user's *observed* merchants
 * (`GET /rules/merchants`, already server-normalized) with free entry allowed —
 * so the common "pin a merchant I've seen" case is a correct pick, and a novel
 * merchant is still typeable. We never normalize client-side (the backend owns
 * `normalize_merchant`, which will gain regex stripping later); the create
 * response echoes the stored key, which we surface in the success line. This is a
 * deliberate asymmetry with labels, which ARE mirrored client-side — see the
 * policy note in `lib/labels.ts` for why (stable label spec vs. ADR-pending
 * merchant regex stripping).
 *
 * Category is a single spend-category pick; tags are existing-only (see
 * `ExistingLabelPicker`). At least one of {category, ≥1 tag} is required. Submit
 * fans out to POST /rules/categories + one POST /rules/labels per tag.
 */
import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { DropdownMenuItem } from "@/components/ui/dropdown-menu";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import {
  Command,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { Field, PickerButton } from "@/components/form/fields";
import { LabelChip } from "@/components/labels/label-chip";
import { IconCheck, IconChevronDown, IconPlus } from "@/components/icons";
import {
  ApiError,
  createCategoryRule,
  createLabelRule,
  listCategories,
  listRuleMerchants,
  type LabelRead,
  type RuleWriteResult,
} from "@/lib/api/client";
import { invalidateRules } from "@/lib/queries/invalidate";
import { cn } from "@/lib/utils";
import { ExistingLabelPicker } from "./existing-label-picker";

function MerchantCombobox({
  value,
  onChange,
}: {
  value: string;
  onChange: (next: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");

  const merchantsQuery = useQuery({
    queryKey: ["rules", "merchants"],
    queryFn: listRuleMerchants,
    enabled: open,
  });
  const observed = merchantsQuery.data ?? [];

  const q = query.trim().toLowerCase();
  const suggestions = useMemo(
    () => (q ? observed.filter((m) => m.includes(q)) : observed).slice(0, 50),
    [observed, q],
  );
  // Offer the free-typed value when it isn't an exact observed match.
  const canUseTyped = q.length > 0 && !observed.includes(q);

  function commit(next: string) {
    onChange(next.trim());
    setQuery("");
    setOpen(false);
  }

  return (
    <Popover
      open={open}
      onOpenChange={(o) => {
        setOpen(o);
        if (!o) setQuery("");
      }}
    >
      <PopoverTrigger asChild>
        <Button
          type="button"
          variant="outline"
          className="h-9 w-full justify-between px-2.5 text-[12.5px] font-normal"
        >
          <span className={cn(!value && "text-muted-foreground")}>
            {value || "Search or type a merchant…"}
          </span>
          <IconChevronDown className="size-3 text-muted-foreground" />
        </Button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        className="w-[--radix-popover-trigger-width] p-0"
      >
        <Command shouldFilter={false}>
          <CommandInput
            value={query}
            onValueChange={setQuery}
            placeholder="e.g. swiggy"
            maxLength={512}
          />
          <CommandList>
            {suggestions.length === 0 && !canUseTyped ? (
              <div className="py-4 text-center text-[12px] text-muted-foreground">
                {observed.length === 0
                  ? "No merchants seen yet — type one."
                  : "No matches — type to use a new merchant."}
              </div>
            ) : null}
            <CommandGroup>
              {canUseTyped ? (
                <CommandItem
                  value={`__use__:${q}`}
                  onSelect={() => commit(query)}
                >
                  <IconPlus className="size-3" />
                  Use “{query.trim()}”
                </CommandItem>
              ) : null}
              {suggestions.map((m) => (
                <CommandItem key={m} value={m} onSelect={() => commit(m)}>
                  {m}
                </CommandItem>
              ))}
            </CommandGroup>
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}

export function NewRuleDialog({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();

  const [merchant, setMerchant] = useState("");
  const [categoryId, setCategoryId] = useState<number | null>(null);
  const [labels, setLabels] = useState<LabelRead[]>([]);
  const [justAdded, setJustAdded] = useState<string | null>(null);

  const categoriesQuery = useQuery({
    queryKey: ["categories"],
    queryFn: listCategories,
    enabled: open,
  });
  const spendCategories = (categoriesQuery.data ?? []).filter(
    (c) => c.kind === "spend" && c.archived_at === null,
  );
  const selectedCategory =
    spendCategories.find((c) => c.id === categoryId) ?? null;

  const merchantTrimmed = merchant.trim();
  const canSubmit =
    merchantTrimmed.length > 0 && (categoryId != null || labels.length > 0);

  function reset() {
    setMerchant("");
    setCategoryId(null);
    setLabels([]);
  }

  const mutation = useMutation({
    mutationFn: async (): Promise<RuleWriteResult> => {
      let last: RuleWriteResult | null = null;
      if (categoryId != null) {
        last = await createCategoryRule({
          merchant: merchantTrimmed,
          category_id: categoryId,
        });
      }
      for (const label of labels) {
        last = await createLabelRule({
          merchant: merchantTrimmed,
          label_id: label.id,
        });
      }
      // canSubmit guarantees at least one write ran, so `last` is set.
      return last as RuleWriteResult;
    },
    onSuccess: (result) => {
      setJustAdded(result.merchant_normalized);
      reset();
    },
    // Invalidate on *settled*, not just success: the fan-out (category POST +
    // one POST per tag) can partially succeed — category pinned, then a tag POST
    // fails — and each pin is its own commit. Refreshing either way keeps the
    // list reflecting what actually persisted, not just the all-success case.
    onSettled: () => invalidateRules(queryClient),
  });

  function handleOpenChange(next: boolean) {
    if (next) return;
    reset();
    setJustAdded(null);
    mutation.reset();
    onClose();
  }

  const excludeLabelIds = useMemo(
    () => new Set(labels.map((l) => l.id)),
    [labels],
  );

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>New rule</DialogTitle>
          <DialogDescription>
            Pin a category and/or tags to a merchant. A pinned rule always wins,
            so future imports can’t override it.
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-3">
          <Field label="Merchant">
            <MerchantCombobox value={merchant} onChange={setMerchant} />
          </Field>

          <Field label="Category (optional)">
            <PickerButton
              label={selectedCategory ? selectedCategory.name : "No category"}
              muted={selectedCategory == null}
            >
              <DropdownMenuItem onSelect={() => setCategoryId(null)}>
                <span className="text-muted-foreground">No category</span>
              </DropdownMenuItem>
              {spendCategories.map((c) => (
                <DropdownMenuItem
                  key={c.id}
                  onSelect={() => setCategoryId(c.id)}
                >
                  {c.name}
                </DropdownMenuItem>
              ))}
            </PickerButton>
          </Field>

          <div className="flex flex-col gap-1.5">
            <span
              className="text-[10.5px] font-medium uppercase text-muted-foreground"
              style={{ letterSpacing: "0.08em" }}
            >
              Tags (optional)
            </span>
            <div className="flex min-h-9 flex-wrap items-center gap-1 rounded-md border border-border bg-background px-1.5 py-1">
              {labels.map((l) => (
                <LabelChip
                  key={l.id}
                  name={l.name}
                  onRemove={() =>
                    setLabels((prev) => prev.filter((x) => x.id !== l.id))
                  }
                />
              ))}
              <ExistingLabelPicker
                exclude={excludeLabelIds}
                onPick={(l) =>
                  setLabels((prev) =>
                    prev.some((x) => x.id === l.id) ? prev : [...prev, l],
                  )
                }
                trigger={
                  <button
                    type="button"
                    className="flex min-w-[88px] flex-1 items-center gap-1 rounded-sm px-1 py-0.5 text-left text-[12.5px] text-muted-foreground outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    {labels.length === 0 ? (
                      "Add tags…"
                    ) : (
                      <>
                        <IconPlus className="size-3" />
                        Add
                      </>
                    )}
                  </button>
                }
              />
            </div>
          </div>
        </div>

        {mutation.isError ? (
          <p className="text-[12px] text-neg">
            {mutation.error instanceof ApiError
              ? mutation.error.detail
              : "Couldn’t save — try again."}
          </p>
        ) : justAdded ? (
          <p className="inline-flex items-center gap-1.5 text-[12px] text-pos">
            <IconCheck className="size-3.5" />
            Pinned rule for “{justAdded}”. Add another, or Done.
          </p>
        ) : null}

        <DialogFooter>
          <Button
            variant="ghost"
            className="h-8 px-3 text-[12.5px]"
            onClick={() => handleOpenChange(false)}
            disabled={mutation.isPending}
          >
            Done
          </Button>
          <Button
            className="h-8 px-3 text-[12.5px]"
            onClick={() => mutation.mutate()}
            disabled={!canSubmit || mutation.isPending}
          >
            {mutation.isPending ? "Saving…" : "Pin rule"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
