import { categoryColorVar } from "@/lib/categories";
import { cn } from "@/lib/utils";
import type { CategoryColor } from "@/lib/api/client";

/**
 * A small colored dot keying a category by its server-resolved `display_color`; `null`
 * (uncategorised, or an id with no colour in hand) renders a neutral muted dot. Never
 * derived from the id. Decorative — `aria-hidden`, the category name carries the meaning.
 *
 * Used wherever a category name appears so the same colour recurs on the spend-by-category
 * bar, the board row, and the settings list. NOT used in the import review TagPicker, whose
 * dot encodes F3 confidence (a different signal).
 */
export function CategoryDot({
  categoryId,
  color,
  className,
}: {
  categoryId: number | null;
  color?: CategoryColor | null;
  className?: string;
}) {
  return (
    <span
      aria-hidden
      className={cn("inline-block size-2 shrink-0 rounded-full", className)}
      style={{ backgroundColor: categoryColorVar(categoryId, color) }}
    />
  );
}
