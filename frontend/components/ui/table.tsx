import type { CSSProperties, ReactNode } from "react";

import { cn } from "@/lib/utils";

/**
 * Shared low-level table primitives for the app's data boards (expenses,
 * holdings, investments, recent-activity, …). Extracted from the three boards
 * that previously each inlined an identical copy. Plain `<th>`/`<td>` cells —
 * not a full table component — so each board keeps its own `<table>`,
 * `<colgroup>`, and row markup.
 */

/** Monospace recipe for tabular amounts (JetBrains Mono + tabular-nums). */
export const MONO: CSSProperties = {
  fontFamily: "var(--font-jbmono), ui-monospace, monospace",
  fontVariantNumeric: "tabular-nums lining-nums",
  letterSpacing: "-0.012em",
};

/**
 * Header cell. Pass `stickyTop` (viewport px) to pin the header row at `lg`+
 * while the body scrolls — the expenses board sits below its sticky filter row,
 * so it passes 112. Sticky engages only at `lg`+: it relies on the consumer's
 * `lg:contents` wrapper to drop the horizontal-scroll box at that width, because
 * an `overflow-x` scroll container captures `position: sticky` and shoves the
 * header down by `top`px. Below `lg` (and when omitted) the header is a plain,
 * static row.
 */
export function Th({
  children,
  align,
  first,
  last,
  stickyTop,
}: {
  children?: ReactNode;
  align?: "right";
  first?: boolean;
  last?: boolean;
  stickyTop?: number;
}) {
  return (
    <th
      scope="col"
      className={cn(
        "h-8 bg-muted text-[11px] font-medium text-muted-foreground/80",
        stickyTop !== undefined && "lg:sticky lg:z-10",
      )}
      style={{
        top: stickyTop,
        textAlign: align === "right" ? "right" : "left",
        paddingLeft: first ? 16 : 12,
        paddingRight: last ? 16 : 12,
        // border-b detaches from a sticky cell under border-separate; an inset
        // shadow stays glued to the bottom edge while the header is pinned.
        boxShadow: "inset 0 -1px 0 var(--border)",
      }}
    >
      {children}
    </th>
  );
}

/** Body cell. `borderClass` lets a row paint its own bottom border (omitted on
 *  the last row). */
export function Td({
  children,
  align,
  first,
  last,
  borderClass,
}: {
  children?: ReactNode;
  align?: "right";
  first?: boolean;
  last?: boolean;
  borderClass?: string;
}) {
  return (
    <td
      className={cn("align-middle", borderClass)}
      style={{
        textAlign: align === "right" ? "right" : "left",
        paddingLeft: first ? 16 : 12,
        paddingRight: last ? 16 : 12,
        paddingTop: 8,
        paddingBottom: 8,
      }}
    >
      {children}
    </td>
  );
}

/** Full-width row for loading / empty / error states. `colSpan` must match the
 *  table's column count. */
export function StateRow({
  children,
  colSpan,
  tone,
}: {
  children: ReactNode;
  colSpan: number;
  tone?: "error";
}) {
  return (
    <tr>
      <td
        colSpan={colSpan}
        className={cn(
          "px-4 py-10 text-center text-[13px]",
          tone === "error" ? "text-neg" : "text-muted-foreground",
        )}
      >
        {children}
      </td>
    </tr>
  );
}
