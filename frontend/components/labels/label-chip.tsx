/**
 * A single transaction label rendered as a chip (PRD §F3a — user "Tags").
 *
 * Read-only on the board (no `onRemove`) — the row opens its dialog to edit.
 * In an editor (`onRemove` passed), the chip grows a nested `<button>` with an
 * `x`, mirroring the review-queue discard control (hover → `text-neg`, focus
 * ring). The `#` is display-only (`labelDisplay`); the stored name has none.
 */
import { Badge } from "@/components/ui/badge";
import { IconX } from "@/components/icons";
import { labelDisplay } from "@/lib/labels";
import { cn } from "@/lib/utils";

export function LabelChip({
  name,
  onRemove,
  className,
}: {
  name: string;
  onRemove?: () => void;
  className?: string;
}) {
  const display = labelDisplay(name);
  return (
    <Badge className={cn("max-w-full", onRemove && "pr-0.5", className)}>
      <span className="truncate">{display}</span>
      {onRemove ? (
        <button
          type="button"
          onClick={onRemove}
          aria-label={`Remove ${display}`}
          className="grid size-3.5 shrink-0 place-items-center rounded-[3px] text-muted-foreground transition-colors hover:text-neg focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
        >
          <IconX className="size-2.5" />
        </button>
      ) : null}
    </Badge>
  );
}
