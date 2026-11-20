"use client";

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandSeparator,
} from "@/components/ui/command";
import { CategoryDot } from "@/components/category-dot";
import { IconCheck, IconChevronDown } from "@/components/icons";
import {
  listCategories,
  type CategoryKind,
  type CategoryRead,
} from "@/lib/api/client";
import {
  buildCategoryTree,
  categoryDisplayName,
} from "@/lib/categories";
import { cn } from "@/lib/utils";

export type CategorySelectorProps = {
  value: number | null;
  onChange: (categoryId: number | null) => void;
  categories?: readonly CategoryRead[];
  kind?: CategoryKind | "all";
  optional?: boolean;
  disabled?: boolean;
  placeholder?: string;
  className?: string;
  triggerClassName?: string;
};

export function CategorySelector({
  value,
  onChange,
  categories: providedCategories,
  kind = "spend",
  optional = true,
  disabled = false,
  placeholder = "Uncategorized",
  className,
  triggerClassName,
}: CategorySelectorProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");

  const categoriesQuery = useQuery({
    queryKey: ["categories"],
    queryFn: () => listCategories(),
    enabled: !providedCategories,
  });

  const allCategories = providedCategories ?? categoriesQuery.data ?? [];
  const categoriesById = useMemo(
    () => new Map<number, CategoryRead>(allCategories.map((c) => [c.id, c])),
    [allCategories],
  );

  const visibleCategories = useMemo(() => {
    return allCategories.filter((c) => {
      if (c.archived_at != null) return false;
      if (kind === "all") return true;
      return c.kind === kind;
    });
  }, [allCategories, kind]);

  const tree = useMemo(
    () => buildCategoryTree(visibleCategories),
    [visibleCategories],
  );

  const selectedCategory = value != null ? categoriesById.get(value) : null;
  const selectedColor = selectedCategory?.display_color ?? null;

  function handleSelect(categoryId: number | null) {
    onChange(categoryId);
    setOpen(false);
    setQuery("");
  }

  // Search ranking when query is present
  const q = query.trim().toLowerCase();
  const searchResults = useMemo(() => {
    if (!q) return [];

    type ScoredItem = {
      category: CategoryRead;
      parent: CategoryRead | null;
      score: number;
    };

    const results: ScoredItem[] = [];

    for (const c of visibleCategories) {
      const nameLower = c.name.toLowerCase();
      const parent =
        c.parent_id != null ? (categoriesById.get(c.parent_id) ?? null) : null;
      const parentNameLower = parent?.name.toLowerCase() ?? "";

      let score = 99;
      if (nameLower === q) {
        // Exact match
        score = 1;
      } else if (nameLower.startsWith(q)) {
        // Prefix match
        score = 2;
      } else if (nameLower.split(/[^\p{L}\p{N}]+/u).some((w) => w.startsWith(q))) {
        // Word boundary startsWith match (e.g. "Food" in "Fast Food")
        score = 3;
      } else if (nameLower.includes(q)) {
        // Substring match
        score = 4;
      } else if (parentNameLower.includes(q)) {
        // Parent matches query
        score = 5;
      }

      if (score < 99) {
        results.push({ category: c, parent, score });
      }
    }

    results.sort((a, b) => {
      if (a.score !== b.score) return a.score - b.score;
      if (a.category.name.length !== b.category.name.length) {
        return a.category.name.length - b.category.name.length;
      }
      return a.category.name.localeCompare(b.category.name);
    });

    return results;
  }, [visibleCategories, categoriesById, q]);

  const displayText = selectedCategory
    ? categoryDisplayName(selectedCategory, categoriesById)
    : placeholder;

  return (
    <div className={cn("w-full", className)}>
      <Popover
        open={open}
        onOpenChange={(o) => {
          if (!disabled) {
            setOpen(o);
            if (!o) setQuery("");
          }
        }}
      >
        <PopoverTrigger asChild>
          <Button
            type="button"
            variant="outline"
            disabled={disabled}
            className={cn(
              "h-9 w-full justify-between px-2.5 text-[12.5px] font-normal transition-colors",
              selectedCategory == null && "text-muted-foreground",
              disabled && "opacity-60 cursor-not-allowed",
              triggerClassName,
            )}
          >
            <span className="flex min-w-0 items-center gap-2 truncate">
              {selectedCategory ? (
                <CategoryDot
                  categoryId={selectedCategory.id}
                  color={selectedColor}
                />
              ) : null}
              <span className="truncate">{displayText}</span>
            </span>
            <IconChevronDown className="ml-2 size-3 shrink-0 text-muted-foreground" />
          </Button>
        </PopoverTrigger>
        <PopoverContent
          align="start"
          className="w-[--radix-popover-trigger-width] min-w-[240px] max-w-[calc(100vw-2rem)] p-0"
        >
          <Command shouldFilter={false}>
            <CommandInput
              value={query}
              onValueChange={setQuery}
              placeholder="Search categories…"
              className="h-9 text-[12.5px]"
            />
            <CommandList
              className="max-h-72 overflow-y-auto"
              onWheelCapture={(e) => e.stopPropagation()}
            >
              {q ? (
                // Search results (ranked by match priority: exact -> prefix -> word -> substring)
                searchResults.length === 0 ? (
                  <CommandEmpty>No matching categories.</CommandEmpty>
                ) : (
                  <CommandGroup className="p-1">
                    {searchResults.map(({ category, parent }) => {
                      const isSelected = value === category.id;
                      const isSub = category.parent_id !== null;

                      return (
                        <CommandItem
                          key={category.id}
                          value={`cat-${category.id}-${category.name}`}
                          onSelect={() => handleSelect(category.id)}
                          className={cn(
                            "flex items-center gap-2 rounded-[5px] px-2 py-1.5 text-[12.5px]",
                            isSelected &&
                              "bg-accent font-medium text-accent-foreground",
                          )}
                        >
                          <CategoryDot
                            categoryId={category.id}
                            color={category.display_color}
                            className={isSub ? "size-1.5" : "size-2"}
                          />
                          <div className="flex flex-1 items-center gap-1.5 min-w-0">
                            <span className="font-medium truncate">{category.name}</span>
                            {isSub && parent ? (
                              <span className="shrink-0 text-[11px] text-muted-foreground/70">
                                · {parent.name}
                              </span>
                            ) : category.role === "investment_transfer" ? (
                              <span className="shrink-0 rounded bg-muted/60 px-1 py-0.5 text-[10px] text-muted-foreground/70 font-normal">
                                system
                              </span>
                            ) : !isSub ? (
                              <span className="shrink-0 rounded bg-muted/60 px-1 py-0.5 text-[10px] text-muted-foreground/70 font-normal">
                                parent
                              </span>
                            ) : null}
                          </div>
                          {isSelected ? (
                            <IconCheck className="ml-auto size-3.5 text-primary shrink-0" />
                          ) : null}
                        </CommandItem>
                      );
                    })}
                  </CommandGroup>
                )
              ) : (
                // Default Tree browsing view (no search query)
                <>
                  {visibleCategories.length === 0 ? (
                    <CommandEmpty>No categories available.</CommandEmpty>
                  ) : null}

                  {optional ? (
                    <>
                      <CommandGroup>
                        <CommandItem
                          value="__uncategorized__"
                          onSelect={() => handleSelect(null)}
                          className="text-[12.5px]"
                        >
                          <CategoryDot categoryId={null} />
                          <span className="flex-1 text-muted-foreground">
                            {placeholder}
                          </span>
                          {value === null ? (
                            <IconCheck className="size-3.5 text-primary" />
                          ) : null}
                        </CommandItem>
                      </CommandGroup>
                      <CommandSeparator />
                    </>
                  ) : null}

                  {tree.map((parent) => {
                    const parentColor = parent.display_color;
                    const isParentSelected = value === parent.id;

                    return (
                      <CommandGroup key={parent.id} className="p-1">
                        {/* Top-level Parent Category Item */}
                        <CommandItem
                          value={`parent-${parent.id}-${parent.name}`}
                          onSelect={() => handleSelect(parent.id)}
                          className={cn(
                            "flex items-center gap-2 rounded-[5px] px-2 py-1.5 text-[12.5px] font-medium",
                            isParentSelected && "bg-accent text-accent-foreground",
                          )}
                        >
                          <CategoryDot
                            categoryId={parent.id}
                            color={parentColor}
                          />
                          <span className="flex-1 truncate">{parent.name}</span>
                          {parent.role === "investment_transfer" ? (
                            <span className="shrink-0 rounded bg-muted/60 px-1 py-0.5 text-[10px] text-muted-foreground/70 font-normal">
                              system
                            </span>
                          ) : null}
                          {isParentSelected ? (
                            <IconCheck className="size-3.5 text-primary" />
                          ) : parent.subcategories.length > 0 ? (
                            <span className="text-[10px] text-muted-foreground/70">
                              {parent.subcategories.length} sub
                            </span>
                          ) : null}
                        </CommandItem>

                        {/* Subcategories (indented) */}
                        {parent.subcategories.map((sub) => {
                          const subColor = sub.display_color;
                          const isSubSelected = value === sub.id;

                          return (
                            <CommandItem
                              key={sub.id}
                              value={`sub-${sub.id}-${sub.name}-${parent.name}`}
                              onSelect={() => handleSelect(sub.id)}
                              className={cn(
                                "ml-3 flex items-center gap-2 border-l border-border/60 py-1.5 pl-3 pr-2 text-[12px]",
                                isSubSelected &&
                                  "bg-accent font-medium text-accent-foreground",
                              )}
                            >
                              <CategoryDot
                                categoryId={sub.id}
                                color={subColor}
                                className="size-1.5"
                              />
                              <span className="flex-1 truncate">{sub.name}</span>
                              {isSubSelected ? (
                                <IconCheck className="size-3.5 text-primary" />
                              ) : null}
                            </CommandItem>
                          );
                        })}
                      </CommandGroup>
                    );
                  })}
                </>
              )}
            </CommandList>
          </Command>
        </PopoverContent>
      </Popover>
    </div>
  );
}
