"use client";

/**
 * ⌘K command palette — the working half of the top-bar search button. A thin
 * client island: it fuzzy-filters (plain substring, no fuzzy lib) over three
 * sources and navigates on select. No backend search endpoint exists in v1, so
 * this is navigation-only, not a transaction search.
 *
 * Sources:
 * - Nav destinations — `NAV_GROUPS` (single source of truth, disabled items dropped).
 * - Accounts — `["accounts"]` query; selecting deep-links to `/expenses?account=<id>`.
 * - Categories — `["categories"]`, **spend kind only**: the expenses board defaults
 *   to the Spending view (every `spend` row, either sign), so an income-category
 *   deep-link would land on a board that never queries it (empty table). Income
 *   categories are out of scope here.
 *
 * The account/category queries are `enabled: open` — the palette is mounted
 * globally in the top-bar, so without the gate it would fetch on every page that
 * never needs those lists (dashboard/holdings/portfolio). Gating shares the cache
 * where it exists and fetches lazily otherwise.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";
import { IconArrowRight, IconSearch } from "@/components/icons";
import { CategoryDot } from "@/components/category-dot";
import { listAccounts, listCategories } from "@/lib/api/client";
import { accountLabel } from "@/lib/accounts";
import { categoryDisplayName, resolveCategoryColor } from "@/lib/categories";
import { cn } from "@/lib/utils";

import { NAV_GROUPS } from "./nav-config";

// Cap the entity groups so a long list never buries the nav section (and the
// empty-query view stays scannable). Nav is uncapped — it's a small fixed set.
const ENTITY_CAP = 6;

type PaletteItem =
  | { kind: "nav"; key: string; label: string; href: string }
  | { kind: "account"; key: string; label: string; href: string }
  | {
      kind: "category";
      key: string;
      label: string;
      href: string;
      categoryId: number;
      color: string | null;
    };

type PaletteGroup = { heading: string; items: PaletteItem[] };

export function CommandPalette({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const router = useRouter();
  const [query, setQuery] = useState("");
  const [highlight, setHighlight] = useState(0);
  const activeRef = useRef<HTMLButtonElement | null>(null);

  const accountsQuery = useQuery({
    queryKey: ["accounts"],
    queryFn: listAccounts,
    enabled: open,
  });
  const categoriesQuery = useQuery({
    queryKey: ["categories"],
    queryFn: listCategories,
    enabled: open,
  });

  // Fresh query + highlight on every open.
  useEffect(() => {
    if (open) {
      setQuery("");
      setHighlight(0);
    }
  }, [open]);

  const navItems = useMemo<PaletteItem[]>(
    () =>
      NAV_GROUPS.flatMap((g) => g.items)
        .filter((i) => !i.disabled)
        .map((i) => ({
          kind: "nav",
          key: `nav:${i.href}`,
          label: i.label,
          href: i.href,
        })),
    [],
  );

  const accountItems = useMemo<PaletteItem[]>(
    () =>
      (accountsQuery.data ?? [])
        .filter((a) => a.archived_at == null)
        .map((a) => ({
          kind: "account",
          key: `account:${a.id}`,
          label: accountLabel(a),
          href: `/expenses?account=${a.id}`,
        })),
    [accountsQuery.data],
  );

  const categoryItems = useMemo<PaletteItem[]>(() => {
    const raw = categoriesQuery.data ?? [];
    return raw
      .filter((c) => c.kind === "spend" && c.archived_at == null)
      .map((c) => ({
        kind: "category",
        key: `category:${c.id}`,
        label: categoryDisplayName(c, raw),
        href: `/expenses?category=${c.id}`,
        categoryId: c.id,
        color: resolveCategoryColor(c, raw),
      }));
  }, [categoriesQuery.data]);

  const q = query.trim().toLowerCase();
  const groups = useMemo<PaletteGroup[]>(() => {
    const match = (label: string) => label.toLowerCase().includes(q);
    const scope = (items: PaletteItem[], cap?: number) => {
      const filtered = q ? items.filter((i) => match(i.label)) : items;
      return cap != null ? filtered.slice(0, cap) : filtered;
    };
    return [
      { heading: "Go to", items: scope(navItems) },
      { heading: "Accounts", items: scope(accountItems, ENTITY_CAP) },
      { heading: "Categories", items: scope(categoryItems, ENTITY_CAP) },
    ].filter((g) => g.items.length > 0);
  }, [q, navItems, accountItems, categoryItems]);

  const flat = useMemo(() => groups.flatMap((g) => g.items), [groups]);

  // Clamp on shrink (a keystroke can drop the list under the stored index).
  const hi = flat.length ? Math.min(highlight, flat.length - 1) : 0;

  // Reset the highlight to the top whenever the result set changes.
  useEffect(() => {
    setHighlight(0);
  }, [q]);

  // Keep the highlighted row in view under arrow navigation.
  useEffect(() => {
    activeRef.current?.scrollIntoView({ block: "nearest" });
  }, [hi]);

  function select(item: PaletteItem) {
    onOpenChange(false);
    router.push(item.href);
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setHighlight((h) => Math.min(h + 1, flat.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setHighlight((h) => Math.max(h - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      const item = flat[hi];
      if (item) select(item);
    }
  }

  // Running index across all groups → maps each row to its position in `flat`
  // for the highlight comparison. Deterministic per render (recomputed below).
  let running = -1;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="top-[12vh] max-w-xl translate-y-0 gap-0 overflow-hidden p-0">
        <DialogTitle className="sr-only">Search</DialogTitle>
        <DialogDescription className="sr-only">
          Jump to a page, account, or category.
        </DialogDescription>

        <div className="flex items-center gap-2.5 border-b border-border px-3.5 pr-11">
          <IconSearch className="size-4 shrink-0 text-muted-foreground" />
          <input
            autoFocus
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder="Jump to a page, account, or category…"
            aria-label="Search"
            className="h-12 w-full bg-transparent text-[13px] text-foreground outline-none placeholder:text-muted-foreground"
          />
        </div>

        <div className="max-h-[min(60vh,380px)] overflow-y-auto py-1">
          {flat.length === 0 ? (
            <p className="px-3.5 py-6 text-center text-[12.5px] text-muted-foreground">
              No results.
            </p>
          ) : (
            groups.map((group) => (
              <div key={group.heading}>
                <div className="px-3.5 pb-1 pt-2.5 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
                  {group.heading}
                </div>
                {group.items.map((item) => {
                  const idx = ++running;
                  const active = idx === hi;
                  return (
                    <button
                      key={item.key}
                      ref={active ? activeRef : undefined}
                      type="button"
                      onClick={() => select(item)}
                      onMouseMove={() => setHighlight(idx)}
                      className={cn(
                        "flex w-full items-center gap-2.5 px-3.5 py-2 text-left text-[13px] text-foreground",
                        active ? "bg-muted" : "",
                      )}
                    >
                      {item.kind === "category" ? (
                        <CategoryDot
                          categoryId={item.categoryId}
                          color={item.color}
                        />
                      ) : (
                        <span className="size-2 shrink-0" aria-hidden />
                      )}
                      <span className="flex-1 truncate">{item.label}</span>
                      <IconArrowRight
                        className={cn(
                          "size-3 shrink-0 text-muted-foreground",
                          active ? "opacity-100" : "opacity-0",
                        )}
                      />
                    </button>
                  );
                })}
              </div>
            ))
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
