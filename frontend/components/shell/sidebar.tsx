"use client";

import { usePathname } from "next/navigation";
import { useQuery } from "@tanstack/react-query";

import { listPendingImports } from "@/lib/api/client";
import { NAV_GROUPS, activeHref } from "./nav-config";
import { SidebarItem } from "./sidebar-item";

/**
 * Persistent left rail — renders every nav group, with the active item derived
 * from the current pathname. Lives once in the app shell (see `AppShell`), not
 * per-page.
 */
export function Sidebar() {
  const pathname = usePathname();
  const active = activeHref(pathname);

  // Live pending-review count → badge on the /imports/review item. Shares the
  // `["imports","pending"]` cache with the top-bar bell, so this adds a
  // subscriber, not a request; undefined badge when nothing is pending.
  const { data: pending } = useQuery({
    queryKey: ["imports", "pending"],
    queryFn: listPendingImports,
  });
  const pendingTotal = (pending ?? []).reduce((n, b) => n + b.pending_count, 0);

  return (
    <aside className="fixed left-0 top-[64px] z-20 hidden h-[calc(100vh-64px)] w-[200px] flex-col gap-4 overflow-y-auto border-r border-border bg-background px-3 py-5 md:flex">
      {NAV_GROUPS.map((group) => (
        <div key={group.label}>
          <div
            className="px-2 pb-1.5 text-[10px] font-medium uppercase text-muted-foreground"
            style={{ letterSpacing: "0.13em" }}
          >
            {group.label}
          </div>
          <nav className="flex flex-col gap-0.5">
            {group.items.map((item) => (
              <SidebarItem
                key={item.href}
                href={item.href}
                label={item.label}
                icon={item.icon}
                badge={
                  item.href === "/imports/review"
                    ? pendingTotal > 0
                      ? pendingTotal
                      : undefined
                    : item.badge
                }
                active={item.href === active}
                disabled={item.disabled ?? false}
              />
            ))}
          </nav>
        </div>
      ))}
    </aside>
  );
}
