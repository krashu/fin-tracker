import { cn } from "@/lib/utils";
import { IconChevronDown, IconPlus, IconQuestion } from "@/components/icons";

export type TagConfidence = "confident" | "uncertain" | "none";

export type TagPickerProps = {
  confidence: TagConfidence;
  /** Resolved category name, or null when the row has no category yet. */
  categoryName: string | null;
  /**
   * True when this untagged row is staged and will commit under a default category
   * (e.g. spend -> Other, refund -> Refund).
   */
  defaultsToOther?: boolean;
  defaultCategoryName?: string;
  priorMatches: number;
  pinned?: boolean;
} & React.ComponentProps<"button">;

export function TagPicker({
  confidence,
  categoryName,
  defaultsToOther = false,
  defaultCategoryName = "Other",
  priorMatches,
  pinned = false,
  className,
  ...props
}: TagPickerProps) {
  if (categoryName == null) {
    if (defaultsToOther) {
      return (
        <button
          type="button"
          className={cn(
            "flex h-8 items-center gap-2 rounded-[4px] border border-border bg-transparent px-2.5 text-[12px] font-medium text-muted-foreground transition-colors duration-100 hover:bg-muted",
            className,
          )}
          {...props}
        >
          <span
            aria-hidden
            className="size-1.5 rounded-full bg-muted-foreground/40"
          />
          <span>{defaultCategoryName}</span>
          <span className="text-[11px] font-normal text-muted-foreground/70">
            · default
          </span>
          <IconChevronDown className="ml-auto size-3 text-muted-foreground" />
        </button>
      );
    }
    return (
      <button
        type="button"
        className={cn(
          "flex h-8 items-center gap-2 rounded-[4px] border border-dashed border-muted-foreground/50 bg-transparent px-2.5 text-[12px] font-medium text-muted-foreground transition-colors duration-100 hover:bg-muted",
          "animate-[deskPulse_2.4s_ease-in-out_infinite]",
          className,
        )}
        {...props}
      >
        <IconPlus className="size-3" />
        <span>Pick category</span>
        {priorMatches === 0 ? (
          <span className="text-[11px] font-normal text-muted-foreground/70">
            · no history
          </span>
        ) : null}
        <IconChevronDown className="ml-auto size-3 text-muted-foreground" />
      </button>
    );
  }

  // User-authored (pinned): the user set this category as always-wins, so it
  // outranks the hit-count confidence tint (a fresh pin sits at prior_matches=1,
  // which would otherwise render the "only 1 prior" warn state). Solid pill with
  // an explicit "pinned" marker.
  if (pinned) {
    return (
      <button
        type="button"
        className={cn(
          "flex h-8 items-center gap-2 rounded-[4px] border border-ring/10 bg-transparent px-2.5 text-[12px] font-medium text-foreground transition-colors duration-100 hover:bg-accent",
          className,
        )}
        {...props}
      >
        <span aria-hidden className="size-1.5 rounded-full bg-primary" />
        <span>{categoryName}</span>
        <span className="rounded-sm bg-accent px-1 py-px text-[10px] font-medium uppercase tracking-wide text-accent-foreground">
          pinned
        </span>
        <IconChevronDown className="ml-auto size-3 text-muted-foreground" />
      </button>
    );
  }

  if (confidence === "uncertain") {
    return (
      <button
        type="button"
        className={cn(
          "flex h-8 items-center gap-2 rounded-[4px] border px-2.5 text-[12px] font-medium text-foreground transition-colors duration-100",
          className,
        )}
        style={{
          borderColor: "var(--warn)",
          background: "color-mix(in oklab, var(--warn) 8%, var(--card))",
        }}
        {...props}
      >
        <IconQuestion className="size-3.5 text-[color:var(--warn)]" />
        <span>{categoryName}</span>
        <span className="text-[12px] font-normal tabular-nums text-muted-foreground/70">
          · only {priorMatches} prior
        </span>
        <IconChevronDown className="ml-auto size-3 text-muted-foreground" />
      </button>
    );
  }

  // "confident" or "none" with a category set — solid pill. Dot is primary when
  // there's prior history, muted for a just-tagged merchant with none.
  return (
    <button
      type="button"
      className={cn(
        "flex h-8 items-center gap-2 rounded-[4px] border border-ring/10 bg-transparent px-2.5 text-[12px] font-medium text-foreground transition-colors duration-100 hover:bg-accent",
        className,
      )}
      {...props}
    >
      <span
        aria-hidden
        className={cn(
          "size-1.5 rounded-full",
          confidence === "confident" ? "bg-primary" : "bg-muted-foreground/50",
        )}
      />
      <span>{categoryName}</span>
      <span className="text-[12px] font-normal tabular-nums text-muted-foreground/70">
        {priorMatches > 0 ? `· ${priorMatches} prior` : "· no history"}
      </span>
      <IconChevronDown className="ml-auto size-3 text-muted-foreground" />
    </button>
  );
}
