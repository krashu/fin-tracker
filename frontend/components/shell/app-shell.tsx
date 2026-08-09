import type { ReactNode } from "react";

import { Sidebar } from "./sidebar";
import { TopBar } from "./top-bar";

/**
 * The single global app chrome: a fixed top bar + a persistent left sidebar +
 * the rail-offset main scroll area. Rendered once in the root layout so every
 * page is just its own content (no per-page TopBar / SubNav / Sidebar wiring).
 *
 * `isDark` is read from the theme cookie in the server layout and threaded to
 * the (client) TopBar for the theme toggle.
 */
export function AppShell({
  isDark,
  children,
}: {
  isDark: boolean;
  children: ReactNode;
}) {
  return (
    <>
      {/* WCAG 2.4.1: the top bar + sidebar are ~15 tab stops ahead of the page's
          own content on every route. `fixed` (not `absolute`) because AppShell
          establishes no positioned ancestor, and z-50 to clear the z-40 header. */}
      <a
        href="#main-content"
        className="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-50 focus:rounded-md focus:border focus:border-border focus:bg-card focus:px-3 focus:py-2 focus:text-[12.5px] focus:font-medium focus:text-foreground"
      >
        Skip to content
      </a>
      <TopBar isDark={isDark} />
      <Sidebar />
      {/* tabIndex={-1} is what makes the skip link actually move focus: without
          it several browsers move only the scroll position, so the next Tab
          restarts from the top bar. outline-none because a focus ring on a
          full-page container is noise — the element is never tabbed to. */}
      <main
        id="main-content"
        tabIndex={-1}
        className="ml-0 min-h-[calc(100vh-64px)] pt-[64px] outline-none md:ml-[200px]"
        style={{ minWidth: 0 }}
      >
        {children}
      </main>
    </>
  );
}
