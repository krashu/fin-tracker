import { cn } from "@/lib/utils";

/**
 * Brand mark — a filled brand-color rounded-square badge with the fin-tracker
 * "F" glyph reversed out. Shared by the top bar and the auth (login/register)
 * pages. Auto-themes via the `--primary` / `--primary-foreground` token pair
 * (light: indigo bg + near-white glyph; dark: light-indigo bg + dark glyph), so
 * no `dark:` overrides are needed.
 */
export function LogoMark({ className }: { className?: string }) {
  return (
    <span
      aria-hidden
      className={cn(
        "grid size-7 shrink-0 place-items-center rounded-[7px] bg-primary text-primary-foreground",
        className,
      )}
    >
      <svg
        width="17"
        height="17"
        viewBox="0 0 20 20"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.9"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <path d="M6.5 15V7.5A1.5 1.5 0 0 1 8 6h5.5" />
        <path d="M6 10.5h6" />
      </svg>
    </span>
  );
}
