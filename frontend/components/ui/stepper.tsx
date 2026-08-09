import { cn } from "@/lib/utils";

/**
 * Import-flow progress indicator. The honest pipeline is Upload → Review & tag
 * → Commit (issuer-specific parsers have nothing to "configure", and the review
 * queue IS the preview), so it's three steps — not the four a generic
 * column-mapping importer would show. Presentational + static per page: each
 * import page renders it with a fixed `current`; it's not a live wizard (the two
 * pages are separate routes). "Commit" is the terminal action inside the review
 * queue, not a third route.
 */
const STEPS = [
  { key: "upload", label: "Upload" },
  { key: "review", label: "Review & tag" },
  { key: "commit", label: "Commit" },
] as const;

export type ImportStep = (typeof STEPS)[number]["key"];

export function ImportStepper({ current }: { current: ImportStep }) {
  const currentIdx = STEPS.findIndex((s) => s.key === current);
  return (
    <ol className="flex items-center gap-2.5" aria-label="Import progress">
      {STEPS.map((step, i) => {
        const state = i < currentIdx ? "done" : i === currentIdx ? "active" : "todo";
        return (
          <li key={step.key} className="flex items-center gap-2.5">
            <span className="flex items-center gap-2">
              <span
                aria-current={state === "active" ? "step" : undefined}
                className={cn(
                  "flex size-5 items-center justify-center rounded-full text-[10.5px] font-semibold tabular-nums",
                  state === "todo"
                    ? "bg-muted text-muted-foreground"
                    : "bg-primary text-primary-foreground",
                  state === "active" && "ring-2 ring-primary/25",
                )}
              >
                {i + 1}
              </span>
              <span
                className={cn(
                  "text-[12px]",
                  state === "todo"
                    ? "text-muted-foreground"
                    : "font-medium text-foreground",
                )}
              >
                {step.label}
              </span>
            </span>
            {i < STEPS.length - 1 ? (
              <span
                aria-hidden
                className={cn(
                  "h-px w-8",
                  state === "done" ? "bg-primary/40" : "bg-border",
                )}
              />
            ) : null}
          </li>
        );
      })}
    </ol>
  );
}
