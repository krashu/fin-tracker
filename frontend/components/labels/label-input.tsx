"use client";

/**
 * Tag (label) type-ahead (PRD §F3a) — the shared editor for the transaction
 * dialog, add dialog, and review-queue row.
 *
 * Model (shadcn Combobox): removable `LabelChip`s + a trigger that opens a
 * `Popover` whose content is a `Command` (cmdk) — search, suggestions from the
 * ["labels"] catalog, and a "Create #x" item when the typed value is new. cmdk
 * owns the combobox/listbox a11y (roles, aria-activedescendant, arrow-key nav,
 * Enter selects the highlighted item); we add comma-to-commit and
 * Backspace-on-empty-to-remove-last.
 *
 * Two presentations off one core (the shared logic + `commandBlock`):
 *  - default (dialogs): a field box showing removable chips + an "Add" trigger.
 *  - `compact` (review-queue row): a single-line summary trigger; the removable
 *    chips move into the popover so the dense 56px row keeps its height.
 *
 * Operates on a LOCAL `string[]` of names — unknown names are allowed; the DB
 * get-or-create happens on save (txn POST/PATCH). Names are normalized
 * client-side (mirroring the backend) for dedupe/exact-match/the Create gate, so
 * `#Travel` and `travel` never both appear. The popover stays open across adds
 * (rapid multi-tag); Escape closes it before any enclosing Dialog (Radix
 * layering). `onCommit` (optional) fires with the current value when the popover
 * closes — the review queue uses it to PATCH once per editing session.
 */
import { useId, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import {
  Popover,
  PopoverAnchor,
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
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { LabelChip } from "@/components/labels/label-chip";
import { IconPlus } from "@/components/icons";
import { listLabels } from "@/lib/api/client";
import {
  LABEL_INPUT_MAX_CHARS,
  labelDisplay,
  normalizeLabelName,
} from "@/lib/labels";
import { cn } from "@/lib/utils";

const COMPACT_SUMMARY_MAX = 2;

export function LabelInput({
  value,
  onChange,
  onCommit,
  label,
  id,
  compact = false,
  disabled,
}: {
  value: string[];
  onChange: (next: string[]) => void;
  /** Fired with the current value when the popover closes (review queue → PATCH). */
  onCommit?: (value: string[]) => void;
  /** Renders a self-owned `<label htmlFor>` caption (this composite can't use
   * `Field`, which clones its id onto a single native child). Default mode only. */
  label?: string;
  id?: string;
  /** Dense single-line trigger; removable chips move into the popover. */
  compact?: boolean;
  disabled?: boolean;
}) {
  const generatedId = useId();
  const inputId = id ?? generatedId;
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");

  // Suggestions come from the shared catalog; gated to `open` so a field that's
  // never touched doesn't fetch (the cache is reused if the board/settings
  // already loaded it).
  const labelsQuery = useQuery({
    queryKey: ["labels"],
    queryFn: listLabels,
    enabled: open,
  });
  const catalog = labelsQuery.data ?? [];

  // Already-added (normalized) names — hidden from suggestions + blocks re-add.
  const addedSet = useMemo(
    () => new Set(value.map((n) => normalizeLabelName(n)).filter(Boolean)),
    [value],
  );

  const normalizedQuery = normalizeLabelName(query);

  // cmdk filtering is disabled (shouldFilter={false}); we filter here so the
  // "Create" item can always show regardless of the query.
  const suggestions = useMemo(
    () =>
      catalog
        .map((l) => l.name)
        .filter((name) => !addedSet.has(name))
        .filter((name) =>
          normalizedQuery ? name.includes(normalizedQuery) : true,
        ),
    [catalog, addedSet, normalizedQuery],
  );

  // Offer Create when the typed value is new: non-empty, not already added, and
  // not an exact existing suggestion (that one's already in the list).
  const canCreate =
    normalizedQuery.length > 0 &&
    !addedSet.has(normalizedQuery) &&
    !suggestions.includes(normalizedQuery);

  function addLabel(raw: string) {
    setQuery("");
    if (disabled) return;
    const name = normalizeLabelName(raw);
    if (!name || addedSet.has(name)) return;
    onChange([...value, name]);
  }

  function removeLabel(name: string) {
    if (disabled) return;
    onChange(value.filter((n) => n !== name));
  }

  function handleOpenChange(next: boolean) {
    setOpen(next);
    if (!next) {
      setQuery("");
      onCommit?.(value);
    }
  }

  // The search + suggestion list — identical in both presentations.
  const commandBlock = (
    <Command shouldFilter={false}>
      <CommandInput
        value={query}
        onValueChange={setQuery}
        placeholder="Search or create a tag…"
        maxLength={LABEL_INPUT_MAX_CHARS}
        onKeyDown={(e) => {
          // Enter / arrows stay cmdk's (this runs on the input, then bubbles to
          // cmdk's Command root — don't stopPropagation).
          if (e.key === ",") {
            e.preventDefault();
            addLabel(query);
          } else if (
            e.key === "Backspace" &&
            query === "" &&
            value.length > 0
          ) {
            removeLabel(value[value.length - 1]);
          }
        }}
      />
      <CommandList>
        {suggestions.length === 0 && !canCreate ? (
          <div className="py-4 text-center text-[12px] text-muted-foreground">
            {catalog.length === 0
              ? "No tags yet — type to create one."
              : "No matching tags."}
          </div>
        ) : null}
        <CommandGroup>
          {/* Create is rendered FIRST so cmdk (which auto-highlights the first
              item) makes Enter create the exact typed name. An exact existing
              match sets canCreate=false, so the exact suggestion is then first
              and Enter selects it — only genuinely-new text highlights Create,
              never a mere substring match (e.g. typing "online" with
              "online-shopping" present). */}
          {canCreate ? (
            <CommandItem
              value={`__create__:${normalizedQuery}`}
              onSelect={() => addLabel(query)}
            >
              <IconPlus className="size-3" />
              Create {labelDisplay(normalizedQuery)}
            </CommandItem>
          ) : null}
          {suggestions.map((name) => (
            <CommandItem
              key={name}
              value={name}
              onSelect={() => addLabel(name)}
            >
              {labelDisplay(name)}
            </CommandItem>
          ))}
        </CommandGroup>
      </CommandList>
    </Command>
  );

  if (compact) {
    const shown = value.slice(0, COMPACT_SUMMARY_MAX);
    const extra = value.length - shown.length;
    const trigger = (
      <PopoverTrigger asChild>
        <button
          type="button"
          disabled={disabled}
          aria-label="Edit tags"
          className="flex min-w-0 items-center gap-1 rounded-sm text-left outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50"
        >
          {value.length === 0 ? (
            // Faint at rest so it doesn't compete with the merchant as a 4th
            // per-row target; lifts to full on the row's hover/focus. Keyed off
            // the enclosing `group` (the review <li>) — NOT `opacity-0`
            // (invisible tap target on touch) and NOT `focus-within` on the
            // span (never fires: the span wraps no focusable node — the trigger
            // button is its ancestor). Compact branch is review-queue-only.
            <span className="text-[11px] text-muted-foreground/70 opacity-70 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100">
              Add tags…
            </span>
          ) : (
            <span className="flex min-w-0 items-center gap-1 overflow-hidden">
              {shown.map((name) => (
                <LabelChip key={name} name={name} />
              ))}
              {extra > 0 ? (
                <span className="shrink-0 text-[11px] tabular-nums text-muted-foreground/70">
                  +{extra}
                </span>
              ) : null}
            </span>
          )}
        </button>
      </PopoverTrigger>
    );
    return (
      <Popover open={open} onOpenChange={handleOpenChange}>
        {/* When the summary hides tags behind "+N", surface the full set on hover
            via the styled tooltip (click still opens the editor popover). */}
        {extra > 0 ? (
          <Tooltip>
            <TooltipTrigger asChild>{trigger}</TooltipTrigger>
            <TooltipContent className="max-w-xs">
              {value.map((name) => labelDisplay(name)).join("  ")}
            </TooltipContent>
          </Tooltip>
        ) : (
          trigger
        )}
        <PopoverContent align="start" className="w-64 p-0">
          {value.length > 0 ? (
            <div className="flex flex-wrap gap-1 border-b border-border p-2">
              {value.map((name) => (
                <LabelChip
                  key={name}
                  name={name}
                  onRemove={() => removeLabel(name)}
                />
              ))}
            </div>
          ) : null}
          {commandBlock}
        </PopoverContent>
      </Popover>
    );
  }

  return (
    <div className="flex flex-col gap-1.5">
      {label ? (
        <label
          htmlFor={inputId}
          className="text-[10.5px] font-medium uppercase text-muted-foreground"
          style={{ letterSpacing: "0.08em" }}
        >
          {label}
        </label>
      ) : null}

      <Popover open={open} onOpenChange={handleOpenChange}>
        <PopoverAnchor asChild>
          <div
            className={cn(
              "flex min-h-9 flex-wrap items-center gap-1 rounded-md border border-border bg-background px-1.5 py-1 focus-within:ring-2 focus-within:ring-ring",
              disabled && "pointer-events-none opacity-50",
            )}
          >
            {value.map((name) => (
              <LabelChip
                key={name}
                name={name}
                onRemove={() => removeLabel(name)}
              />
            ))}
            <PopoverTrigger asChild>
              <button
                type="button"
                id={inputId}
                disabled={disabled}
                className="flex min-w-[88px] flex-1 items-center gap-1 rounded-sm px-1 py-0.5 text-left text-[12.5px] text-muted-foreground outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
              >
                {value.length === 0 ? (
                  "Add tags…"
                ) : (
                  <>
                    <IconPlus className="size-3" />
                    Add
                  </>
                )}
              </button>
            </PopoverTrigger>
          </div>
        </PopoverAnchor>

        <PopoverContent align="start" className="w-64 p-0">
          {commandBlock}
        </PopoverContent>
      </Popover>
    </div>
  );
}
