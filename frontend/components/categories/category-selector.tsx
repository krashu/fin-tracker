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
  resolveCategoryColor,
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
  const selectedColor = resolveCategoryColor(selectedCategory, categoriesById);

  function handleSelect(categoryId: number | null) {
    onChange(categoryId);
    setOpen(false);
    setQuery("");
  }

  // Filter tree when query is present
  const q = query.trim().toLowerCase();
  const filteredTree = useMemo(() => {
    if (!q) return tree;
    return tree
      .map((parent) => {
        const parentMatches = parent.name.toLowerCase().includes(q);
        const matchingSubcategories = parent.subcategories.filter((sub) =>
          sub.name.toLowerCase().includes(q),
        );
        if (parentMatches || matchingSubcategories.length > 0) {
          return {
            ...parent,
            // If parent matches, show all its subcategories; otherwise show matching subcategories
            subcategories: parentMatches
              ? parent.subcategories
              : matchingSubcategories,
          };
        }
        return null;
      })
      .filter((node): node is NonNullable<typeof node> => node !== null);
  }, [tree, q]);

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
              {filteredTree.length === 0 ? (
                <CommandEmpty>No matching categories.</CommandEmpty>
              ) : null}

              {optional && !q ? (
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

              {filteredTree.map((parent) => {
                const parentColor = resolveCategoryColor(
                  parent,
                  categoriesById,
                );
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
                      const subColor = resolveCategoryColor(
                        sub,
                        categoriesById,
                      );
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
            </CommandList>
          </Command>
        </PopoverContent>
      </Popover>
    </div>
  );
}
