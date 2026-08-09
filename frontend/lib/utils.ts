import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/** Max characters for a single transaction label (PRD §F3a — user "Tags").
 * Matches `Label.name`'s `String(64)` column + the backend's `normalize_label_name`
 * cap, so a chip can never be entered longer than it can be stored. Enforced at
 * entry by native `maxLength` on the label input. */
export const LABEL_MAX_CHARS = 64;
