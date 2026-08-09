"use client";

/**
 * Existing-tag picker for the rules page — a Popover + Command (cmdk) list of the
 * user's labels (the shared ["labels"] catalog). Deliberately NOT the
 * get-or-create `LabelInput`: authoring a rule must never spawn a dangling tag
 * (the backend rejects an unknown `label_id`), so this offers only tags that
 * already exist; new tags are created in Settings → Tags. Emits `onPick(label)`
 * per selection and stays open for rapid multi-pick; `exclude` hides ids already
 * chosen or already on the merchant.
 */
import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";

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
import { listLabels, type LabelRead } from "@/lib/api/client";
import { labelDisplay, normalizeLabelName } from "@/lib/labels";

export function ExistingLabelPicker({
  trigger,
  exclude,
  onPick,
}: {
  trigger: React.ReactNode;
  exclude?: Set<number>;
  onPick: (label: LabelRead) => void;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");

  const labelsQuery = useQuery({
    queryKey: ["labels"],
    queryFn: listLabels,
    enabled: open,
  });
  const catalog = labelsQuery.data ?? [];

  const nq = normalizeLabelName(query);
  const suggestions = useMemo(
    () =>
      catalog
        .filter((l) => !exclude?.has(l.id))
        .filter((l) => (nq ? l.name.includes(nq) : true)),
    [catalog, exclude, nq],
  );

  function handleOpenChange(next: boolean) {
    setOpen(next);
    if (!next) setQuery("");
  }

  return (
    <Popover open={open} onOpenChange={handleOpenChange}>
      <PopoverTrigger asChild>{trigger}</PopoverTrigger>
      <PopoverContent align="start" className="w-64 p-0">
        <Command shouldFilter={false}>
          <CommandInput
            value={query}
            onValueChange={setQuery}
            placeholder="Search tags…"
          />
          <CommandList>
            {suggestions.length === 0 ? (
              <div className="py-4 text-center text-[12px] text-muted-foreground">
                {catalog.length === 0
                  ? "No tags yet — create one in Settings → Tags."
                  : "No matching tags."}
              </div>
            ) : null}
            <CommandGroup>
              {suggestions.map((l) => (
                <CommandItem
                  key={l.id}
                  value={l.name}
                  onSelect={() => onPick(l)}
                >
                  {labelDisplay(l.name)}
                </CommandItem>
              ))}
            </CommandGroup>
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}
