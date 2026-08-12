import { cn } from "@/lib/utils";
import { IconChevronDown, IconPlus, IconQuestion } from "@/components/icons";

// "seeded" (ADR-0011 merchant-alias layer, Phase A3): the prefilled category
// comes from a dictionary entry this user has never confirmed (hit_count ===
// 0), distinct from "none" (no rule at all). Must move together with
// client.ts's `ConfidenceLabel` — structurally, not by a shared import (see
// review-queue.tsx's TagPicker usage).
export type TagConfidence = "confident" | "uncertain" | "seeded" | "none";

export type TagPickerProps = {
  confidence: TagConfidence;
  /** Resolved category name, or null when the row has no category yet. */
  categoryName: string | null;
  /**
   * True when this untagged row is staged and will commit under a default category
   * (e.g. spend -> Other; a refund is a positive-amount spend, so it lands there too).
   */
  defaultsToOther?: boolean;
  defaultCategoryName?: string;
  /**
   * True for a staged, still-uncategorized card-bill-payment row
   * (`cc_payment_candidate`, PRD §F4a-1) — distinct from `defaultsToOther`
   * because nothing is actually applied here: the row stays `category_id`
   * NULL either way, becoming a `transfer` if a matching bank debit is
   * found at commit, or uncategorized `income` if not.
   */
  cardPayment?: boolean;
  priorMatches: number;
  pinned?: boolean;
} & React.ComponentProps<"button">;

export function TagPicker({
  confidence,
  categoryName,
  defaultsToOther = false,
  defaultCategoryName = "Other",
  cardPayment = false,
  priorMatches,
  pinned = false,
  className,
  ...props
}: TagPickerProps) {
  if (categoryName == null) {
    // Checked before `defaultsToOther` — mutually exclusive in practice (a
    // card-payment merchant never also matches the cashback regex), but this
    // is the more specific signal.
    if (cardPayment) {
      return (
        <button
          type="button"
          className={cn(
            "flex h-8 w-full min-w-0 items-center gap-2 rounded-[4px] border border-border bg-transparent px-2.5 text-[12px] font-medium text-foreground transition-colors duration-100 hover:bg-muted",
            className,
          )}
          {...props}
        >
          <span className="min-w-0 truncate">Card payment</span>
          <span
            className="shrink-0 rounded-sm bg-accent px-1 py-px text-[10px] font-medium uppercase tracking-wide text-accent-foreground"
            title="Links to your bank debit at commit — no category is applied"
          >
            auto
          </span>
          <IconChevronDown className="ml-auto size-3 shrink-0 text-muted-foreground" />
        </button>
      );
    }
    if (defaultsToOther) {
      return (
        <button
          type="button"
          className={cn(
            "flex h-8 w-full min-w-0 items-center gap-2 rounded-[4px] border border-border bg-transparent px-2.5 text-[12px] font-medium text-foreground transition-colors duration-100 hover:bg-muted",
            className,
          )}
          {...props}
        >
          <span className="min-w-0 truncate">{defaultCategoryName}</span>
          <span
            className="shrink-0 rounded-sm bg-accent px-1 py-px text-[10px] font-medium uppercase tracking-wide text-accent-foreground"
            title="Applied at commit — not learned as a rule"
          >
            auto
          </span>
          <IconChevronDown className="ml-auto size-3 shrink-0 text-muted-foreground" />
        </button>
      );
    }
    return (
      <button
        type="button"
        className={cn(
          "flex h-8 w-full min-w-0 items-center gap-2 rounded-[4px] border border-dashed border-muted-foreground/50 bg-transparent px-2.5 text-[12px] font-medium text-muted-foreground transition-colors duration-100 hover:bg-muted",
          className,
        )}
        {...props}
      >
        <IconPlus className="size-3 shrink-0" />
        <span className="min-w-0 truncate">Pick category</span>
        <IconChevronDown className="ml-auto size-3 shrink-0 text-muted-foreground" />
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
          "flex h-8 w-full min-w-0 items-center gap-2 rounded-[4px] border border-ring/10 bg-transparent px-2.5 text-[12px] font-medium text-foreground transition-colors duration-100 hover:bg-accent",
          className,
        )}
        {...props}
      >
        <span aria-hidden className="size-1.5 rounded-full bg-primary" />
        <span className="min-w-0 truncate">{categoryName}</span>
        <span className="shrink-0 rounded-sm bg-accent px-1 py-px text-[10px] font-medium uppercase tracking-wide text-accent-foreground">
          pinned
        </span>
        <IconChevronDown className="ml-auto size-3 shrink-0 text-muted-foreground" />
      </button>
    );
  }

  // "seeded": the suggestion comes from the merchant-alias dictionary
  // (ADR-0011), never confirmed by this user (hit_count === 0) — distinct
  // from both "pinned" (user-authored) and the plain "none"/"confident" pill
  // below. Dashed border + a "dictionary" marker, no "· N prior" text since
  // priorMatches is always 0 here.
  if (confidence === "seeded") {
    return (
      <button
        type="button"
        className={cn(
          "flex h-8 w-full min-w-0 items-center gap-2 rounded-[4px] border border-dashed border-ring/20 bg-transparent px-2.5 text-[12px] font-medium text-foreground transition-colors duration-100 hover:bg-accent",
          className,
        )}
        {...props}
      >
        <span aria-hidden className="size-1.5 rounded-full bg-muted-foreground/50" />
        <span className="min-w-0 truncate">{categoryName}</span>
        <span
          className="shrink-0 rounded-sm bg-accent px-1 py-px text-[10px] font-medium uppercase tracking-wide text-accent-foreground"
          title="Suggested from the merchant dictionary — not yet confirmed"
        >
          dictionary
        </span>
        <IconChevronDown className="ml-auto size-3 shrink-0 text-muted-foreground" />
      </button>
    );
  }

  if (confidence === "uncertain") {
    return (
      <button
        type="button"
        className={cn(
          "flex h-8 w-full min-w-0 items-center gap-2 rounded-[4px] border px-2.5 text-[12px] font-medium text-foreground transition-colors duration-100",
          className,
        )}
        style={{
          borderColor: "var(--warn)",
          background: "color-mix(in oklab, var(--warn) 8%, var(--card))",
        }}
        {...props}
      >
        <IconQuestion className="size-3.5 shrink-0 text-[color:var(--warn)]" />
        <span className="min-w-0 truncate">{categoryName}</span>
        <span className="shrink-0 text-[12px] font-normal tabular-nums text-muted-foreground/70">
          · only {priorMatches} prior
        </span>
        <IconChevronDown className="ml-auto size-3 shrink-0 text-muted-foreground" />
      </button>
    );
  }

  // "confident" or "none" with a category set — solid pill. Dot is primary when
  // there's prior history, muted for a just-tagged merchant with none.
  return (
    <button
      type="button"
      className={cn(
        "flex h-8 w-full min-w-0 items-center gap-2 rounded-[4px] border border-ring/10 bg-transparent px-2.5 text-[12px] font-medium text-foreground transition-colors duration-100 hover:bg-accent",
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
      <span className="min-w-0 truncate">{categoryName}</span>
      {priorMatches > 0 ? (
        <span className="shrink-0 text-[12px] font-normal tabular-nums text-muted-foreground/70">
          · {priorMatches} prior
        </span>
      ) : null}
      <IconChevronDown className="ml-auto size-3 shrink-0 text-muted-foreground" />
    </button>
  );
}
