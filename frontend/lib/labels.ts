import { LABEL_MAX_CHARS } from "@/lib/utils";

/**
 * Client helpers for transaction labels (PRD §F3a — user-facing "Tags").
 *
 * Labels store as a plain lowercased word; the `#` is display-only. Two helpers:
 * `labelDisplay` prepends the `#` for rendering, and `normalizeLabelName` is a
 * faithful mirror of the backend `normalize_label_name`
 * (`services/transaction_labels.py`) — the same word must dedupe the same way on
 * both sides, so the chip input can decide "already added?" and "offer Create?"
 * before the server ever sees it. Keep the two in lockstep.
 *
 * Policy note — this is deliberately ASYMMETRIC with merchant normalization,
 * which the client does NOT mirror (the merchant combobox in
 * `settings/rules/new-rule-dialog.tsx` trusts the server echo instead). The
 * asymmetry is intentional, not an oversight: the label spec is small and stable
 * (strip/lowercase/cap), so mirroring it client-side is cheap and lets the
 * create-gate work offline; `normalize_merchant` is slated to gain regex
 * stripping (RRN/auth-code) under an ADR (see `services/merchant.py`), so the
 * client must NOT guess it — a stale mirror would mis-judge "already pinned?".
 */

/** Render a stored label name as a chip caption: `travel` → `#travel`. */
export function labelDisplay(name: string): string {
  return `#${name}`;
}

const WHITESPACE_RE = /\s+/g;

/**
 * Native `maxLength` for any input a label name is typed into.
 *
 * One more than the storage cap, because `normalizeLabelName` strips the leading `#`
 * FIRST and applies the cap LAST — so the `#` is a display-only glyph that must not
 * eat a character of the real name. Using the bare `LABEL_MAX_CHARS` here means
 * `#` + a 64-char name silently stores 63 chars, and the same intent typed into the
 * other input stores 64: two near-identical rows in the tag catalog for one tag, and
 * a rename dialog that can never type its way back to the 64-char name.
 *
 * Stated once here so the two inputs cannot disagree again.
 */
export const LABEL_INPUT_MAX_CHARS = LABEL_MAX_CHARS + 1;

/**
 * Canonicalize a user-typed label name, or `""` if it's empty after normalizing.
 *
 * Rules (mirror the backend exactly): strip, drop one leading `#`, remove `;`
 * (the backup-CSV delimiter), collapse internal whitespace to single spaces,
 * lowercase, cap at 64. So `#Travel`, `travel`, and `  travel  ` all resolve to
 * one label. The backend returns `None` for empty; here `""` is the falsy
 * sentinel callers gate on.
 */
export function normalizeLabelName(raw: string): string {
  let s = raw.trim();
  if (s.startsWith("#")) s = s.slice(1);
  s = s.replace(/;/g, "");
  s = s.replace(WHITESPACE_RE, " ").trim().toLowerCase();
  if (!s) return "";
  return s.slice(0, LABEL_MAX_CHARS);
}

/**
 * Order-independent set equality for label-name arrays.
 *
 * Assumes each array is already deduped and normalized (the label chip input
 * enforces both), so `length + every` is an exact set comparison. Used to decide
 * whether a label edit actually changed anything before firing a PATCH.
 */
export function sameLabelSet(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((x) => b.includes(x));
}
