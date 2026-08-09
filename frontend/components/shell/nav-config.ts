// Single source of truth for app nav. Imported by `Sidebar`.
//
// One persistent left rail (Monarch / Lunch Money pattern): every primary
// destination is grouped and always visible. Routes not built yet carry
// `disabled: true` — the rail renders them muted and non-interactive. Drop the
// flag from an item once its page lands.
//
// Carries a leading `icon` per item (rendered by `SidebarItem` + `MobileNav`).
// This makes the module React-bearing; `command-palette.tsx` also imports
// `NAV_GROUPS` but reads only href/label/disabled and ignores `icon`.

import type { ComponentType, SVGProps } from "react";

import {
  IconArchive,
  IconChart,
  IconCheckAll,
  IconCloud,
  IconDoc,
  IconExchange,
  IconGrid,
  IconHash,
  IconList,
  IconRules,
  IconShield,
  IconStack,
  IconTag,
  IconTrend,
  IconUpload,
  IconWallet,
} from "@/components/icons";

export type NavItem = {
  href: string;
  label: string;
  icon: ComponentType<SVGProps<SVGSVGElement>>;
  badge?: string | number;
  disabled?: boolean;
};

export type NavGroup = {
  label: string;
  items: NavItem[];
};

export const NAV_GROUPS: NavGroup[] = [
  {
    label: "Overview",
    items: [{ href: "/dashboard", label: "Overview", icon: IconGrid }],
  },
  {
    label: "Spending",
    items: [
      { href: "/expenses", label: "Expenses", icon: IconList },
      { href: "/spending", label: "Analysis", icon: IconChart },
      // Date-range reports + month-wise pivot — Part 3.
      { href: "/reports", label: "Reports", icon: IconDoc, disabled: true },
    ],
  },
  {
    label: "Investments",
    items: [
      { href: "/holdings", label: "Holdings", icon: IconStack },
      { href: "/investments", label: "Transactions", icon: IconExchange },
      { href: "/portfolio", label: "Portfolio", icon: IconTrend },
    ],
  },
  {
    label: "Data",
    items: [
      // The import inbox that Statements/Investments feed. The live pending
      // count is injected as a badge in `Sidebar` (kept out of this static
      // config); the page lists open batches to review.
      { href: "/imports/review", label: "Review", icon: IconCheckAll },
      { href: "/imports/statements", label: "Statements", icon: IconDoc },
      { href: "/imports/investments", label: "Investments", icon: IconUpload },
      // Download / load a CSV backup of spend transactions (PRD §F10).
      { href: "/settings/backup", label: "Backup", icon: IconArchive },
    ],
  },
  {
    label: "Settings",
    items: [
      { href: "/settings/accounts", label: "Accounts", icon: IconWallet },
      { href: "/settings/categories", label: "Categories", icon: IconTag },
      // Freeform transaction tags (PRD §F3a). "Tags" in the UI; `label` in code
      // (the URL is /settings/labels) to avoid colliding with F3's merchant→
      // category "tag" domain.
      { href: "/settings/labels", label: "Tags", icon: IconHash },
      { href: "/settings/security", label: "Security", icon: IconShield },
      {
        href: "/settings/rules",
        label: "Rules",
        icon: IconRules,
      },
      {
        href: "/settings/drive",
        label: "Drive sync",
        icon: IconCloud,
        disabled: true,
      },
    ],
  },
];

/**
 * The nav item that should render active for a given pathname. Exact match
 * first, then longest-prefix (so a nested route like `/settings/accounts/...`
 * still lights up its parent). Disabled items never win. Returns the matching
 * href, or `null` when nothing matches (e.g. the redirect-only review page).
 */
export function activeHref(pathname: string): string | null {
  const items = NAV_GROUPS.flatMap((g) => g.items).filter((i) => !i.disabled);

  const exact = items.find((i) => i.href === pathname);
  if (exact) return exact.href;

  const prefix = items
    .filter((i) => pathname.startsWith(`${i.href}/`))
    .sort((a, b) => b.href.length - a.href.length)[0];

  return prefix?.href ?? null;
}
