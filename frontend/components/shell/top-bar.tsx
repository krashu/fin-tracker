"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";

import { useAuth } from "@/components/auth/auth-provider";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Kbd } from "@/components/ui/kbd";
import {
  IconBell,
  IconChevronDown,
  IconList,
  IconSearch,
} from "@/components/icons";
import { listPendingImports, type AuthUser } from "@/lib/api/client";
import { pendingBatchLabel } from "@/lib/accounts";

import { NAV_GROUPS } from "./nav-config";
import { LogoMark } from "./logo-mark";
import { ThemeToggle } from "./theme-toggle";
import { BalanceToggle } from "./balance-toggle";
import { CommandPalette } from "./command-palette";

/** Up-to-two-letter avatar initials from the display name (word initials) or,
 * failing that, the email local-part. Falls back to "?" for a nameless user. */
function initialsFor(user: AuthUser | null): string {
  const name = user?.display_name?.trim();
  if (name) {
    const parts = name.split(/\s+/);
    const letters = parts
      .slice(0, 2)
      .map((p) => p[0])
      .join("");
    return letters.toUpperCase();
  }
  const email = user?.email?.trim();
  if (email) return email.slice(0, 2).toUpperCase();
  return "?";
}

function UserMenu() {
  const router = useRouter();
  const { user, logout } = useAuth();

  async function handleSignOut() {
    await logout();
    router.replace("/login");
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          className="h-8 gap-1.5 px-1"
          aria-label="Account menu"
        >
          <span className="grid size-7 place-items-center rounded-[4px] bg-accent text-accent-foreground text-[11px] font-semibold ring-1 ring-ring/10">
            {initialsFor(user)}
          </span>
          <IconChevronDown className="size-3 text-muted-foreground" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-56">
        <DropdownMenuLabel className="truncate font-normal text-muted-foreground">
          {user?.display_name?.trim() || user?.email || "Signed in"}
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={handleSignOut}>Sign out</DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/**
 * Thin global header: brand on the left (aligned to the sidebar rail width),
 * utility controls on the right. Primary navigation now lives in the persistent
 * `Sidebar`, so this no longer renders section tabs.
 */
/**
 * Mobile-only navigation: the persistent Sidebar is hidden below `md`, so this
 * hamburger menu exposes the same nav destinations in a dropdown. Reuses
 * NAV_GROUPS (single source of truth) and skips routes still flagged disabled.
 */
function MobileNav() {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className="ml-2 self-center md:hidden"
          aria-label="Open navigation menu"
        >
          <IconList className="size-4" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-56">
        {NAV_GROUPS.map((group, gi) => {
          const items = group.items.filter((i) => !i.disabled);
          if (items.length === 0) return null;
          return (
            <div key={group.label}>
              {gi > 0 ? <DropdownMenuSeparator /> : null}
              <div className="px-2 py-1 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
                {group.label}
              </div>
              {items.map((item) => (
                <DropdownMenuItem key={item.href} asChild>
                  <Link href={item.href} className="flex items-center gap-2">
                    <item.icon className="size-4 shrink-0 text-muted-foreground" />
                    {item.label}
                  </Link>
                </DropdownMenuItem>
              ))}
            </div>
          );
        })}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/**
 * Notification bell — a real pending-review indicator. Counts uncommitted import
 * rows via `GET /imports/pending`; the dot shows only when something is pending,
 * and the dropdown links back into each batch's review queue (the only way back
 * after navigating away from a fresh upload). The `["imports","pending"]` key is
 * invalidated by the upload / commit / cancel flows so the count stays live.
 */
function PendingBell() {
  const { data } = useQuery({
    queryKey: ["imports", "pending"],
    queryFn: listPendingImports,
  });
  const batches = data ?? [];
  const total = batches.reduce((n, b) => n + b.pending_count, 0);
  const hasPending = total > 0;
  const label = hasPending
    ? `${total} transaction${total === 1 ? "" : "s"} pending review`
    : "No pending reviews";

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          type="button"
          variant="outline"
          size="icon"
          className="relative"
          aria-label={label}
          title={label}
        >
          <IconBell className="size-3.5" />
          {hasPending ? (
            <span
              aria-hidden
              className="absolute right-1 top-1 size-1.5 rounded-full bg-primary ring-2 ring-card"
            />
          ) : null}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-64">
        <DropdownMenuLabel className="font-normal text-muted-foreground">
          Pending review
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        {batches.length === 0 ? (
          <DropdownMenuItem disabled>You’re all caught up</DropdownMenuItem>
        ) : (
          batches.map((b) => (
            <DropdownMenuItem key={b.batch_id} asChild>
              <Link
                href={`/imports/review/${b.batch_id}`}
                className="flex items-center justify-between gap-3"
              >
                <span className="truncate">
                  {pendingBatchLabel(b.account_name, b.account_last4)}
                </span>
                <span className="shrink-0 tabular-nums text-muted-foreground">
                  {b.pending_count} pending
                </span>
              </Link>
            </DropdownMenuItem>
          ))
        )}
        <DropdownMenuSeparator />
        <DropdownMenuItem asChild>
          <Link href="/imports/review" className="text-muted-foreground">
            See all pending
          </Link>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export function TopBar({ isDark }: { isDark: boolean }) {
  const [paletteOpen, setPaletteOpen] = useState(false);

  // ⌘K / Ctrl+K opens the palette. Skip when the focus is in an editable
  // target (a form field, the palette's own input, a contentEditable) so we
  // don't hijack the keystroke mid-typing.
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        const t = e.target as HTMLElement | null;
        if (
          t &&
          (t.tagName === "INPUT" ||
            t.tagName === "TEXTAREA" ||
            t.isContentEditable)
        ) {
          return;
        }
        e.preventDefault();
        setPaletteOpen(true);
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  return (
    <header className="fixed inset-x-0 top-0 z-40 flex h-16 items-stretch border-b border-border bg-background">
      <CommandPalette open={paletteOpen} onOpenChange={setPaletteOpen} />
      <MobileNav />
      <div className="flex w-[200px] items-center gap-2.5 pl-5 pr-7 md:pl-6">
        <LogoMark />
        <span className="text-[14px] font-semibold tracking-[-0.012em] text-foreground">
          fin
          <span className="font-normal text-muted-foreground">·</span>
          tracker
        </span>
      </div>

      <div className="ml-auto flex items-center gap-2 pr-5">
        <Button
          type="button"
          variant="outline"
          onClick={() => setPaletteOpen(true)}
          className="hidden h-8 min-w-[240px] justify-start gap-2 px-2.5 text-[12px] font-normal text-muted-foreground hover:text-muted-foreground md:flex"
        >
          <IconSearch className="size-3.5" />
          <span className="flex-1 text-left">
            Search pages, accounts, categories…
          </span>
          <Kbd className="ml-auto">⌘K</Kbd>
        </Button>

        <PendingBell />

        <BalanceToggle />

        <ThemeToggle isDark={isDark} />

        <UserMenu />
      </div>
    </header>
  );
}
