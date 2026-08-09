import type { ElementType, ReactNode } from "react";

/**
 * Small uppercase section eyebrow (10.5px, +0.13em tracking) used above panels
 * and KPI tiles. Extracted verbatim from the dashboard overview so /portfolio
 * and the overview share one recipe instead of each inlining a copy.
 *
 * `as` renders the eyebrow as a heading element (e.g. "h2") so a section's
 * visible label is also its accessible heading, without changing the styling
 * (Tailwind preflight makes headings inherit size/weight). Defaults to "span".
 *
 * NOTE: the overview's `GroupLabel` (10px, text-muted-foreground/80, 0.11em) is
 * a deliberately distinct near-twin — it is NOT this and must stay separate.
 */
export function Eyebrow({
  as: As = "span",
  children,
}: {
  as?: ElementType;
  children: ReactNode;
}) {
  return (
    <As
      className="text-[10.5px] font-medium uppercase text-muted-foreground"
      style={{ letterSpacing: "0.13em" }}
    >
      {children}
    </As>
  );
}
