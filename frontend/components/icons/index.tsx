import type { SVGProps } from "react";

// Consolidated app icons. Inline SVG, single source of truth.
// strokeWidth 1.4 is the default tone for most icons; the few that need
// emphasis (IconPlus, IconCheck) override.
//
// Every icon defaults to `aria-hidden="true"` (decorative): icon-only controls
// carry their own `aria-label`, so the glyphs must not surface to AT. It sits
// before `{...props}`, so a caller can still expose one via `aria-hidden={false}`
// if an icon ever needs to convey meaning on its own.

export function IconSearch(props: SVGProps<SVGSVGElement>) {
  return (
    <svg
      aria-hidden="true"
      {...props}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <circle cx="7" cy="7" r="4.5" />
      <path d="m13.5 13.5-2.7-2.7" />
    </svg>
  );
}

export function IconChevronDown(props: SVGProps<SVGSVGElement>) {
  return (
    <svg
      aria-hidden="true"
      {...props}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="m4 6 4 4 4-4" />
    </svg>
  );
}

export function IconChevronLeft(props: SVGProps<SVGSVGElement>) {
  return (
    <svg
      aria-hidden="true"
      {...props}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="m10 4-4 4 4 4" />
    </svg>
  );
}

export function IconChevronRight(props: SVGProps<SVGSVGElement>) {
  return (
    <svg
      aria-hidden="true"
      {...props}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="m6 4 4 4-4 4" />
    </svg>
  );
}

export function IconPlus(props: SVGProps<SVGSVGElement>) {
  return (
    <svg
      aria-hidden="true"
      {...props}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
    >
      <path d="M8 3v10M3 8h10" />
    </svg>
  );
}

export function IconBell(props: SVGProps<SVGSVGElement>) {
  return (
    <svg
      aria-hidden="true"
      {...props}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M4 7a4 4 0 0 1 8 0v2.3l1 1.7H3l1-1.7V7Z" />
      <path d="M6.5 12.5a1.5 1.5 0 0 0 3 0" />
    </svg>
  );
}

export function IconUpload(props: SVGProps<SVGSVGElement>) {
  return (
    <svg
      aria-hidden="true"
      {...props}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M8 11V3M4.5 6.5 8 3l3.5 3.5M3 13h10" />
    </svg>
  );
}

export function IconX(props: SVGProps<SVGSVGElement>) {
  return (
    <svg
      aria-hidden="true"
      {...props}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
    >
      <path d="M4 4l8 8M12 4l-8 8" />
    </svg>
  );
}

export function IconSun(props: SVGProps<SVGSVGElement>) {
  return (
    <svg
      aria-hidden="true"
      {...props}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinecap="round"
    >
      <circle cx="8" cy="8" r="2.8" />
      <path d="M8 1.5v1.6M8 12.9v1.6M14.5 8h-1.6M3.1 8H1.5M12.6 3.4l-1.1 1.1M4.5 11.5l-1.1 1.1M12.6 12.6l-1.1-1.1M4.5 4.5 3.4 3.4" />
    </svg>
  );
}

export function IconMoon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg
      aria-hidden="true"
      {...props}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M13.5 9.6A5.5 5.5 0 1 1 6.4 2.5a4.4 4.4 0 0 0 7.1 7.1Z" />
    </svg>
  );
}

export function IconList(props: SVGProps<SVGSVGElement>) {
  return (
    <svg
      aria-hidden="true"
      {...props}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinecap="round"
    >
      <path d="M3 4.5h10M3 8h10M3 11.5h6" />
    </svg>
  );
}

export function IconChart(props: SVGProps<SVGSVGElement>) {
  return (
    <svg
      aria-hidden="true"
      {...props}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M2.5 13h11" />
      <path d="M5 11V7M8 11V4.5M11 11V8.5" />
    </svg>
  );
}

export function IconTag(props: SVGProps<SVGSVGElement>) {
  return (
    <svg
      aria-hidden="true"
      {...props}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M2.5 8.5 8.5 2.5h4v4l-6 6-4-4Z" />
      <circle cx="10" cy="6" r="0.8" />
    </svg>
  );
}

// Hash glyph for the Settings → Tags nav item (labels render a text `#`, not
// this icon — this is nav-only).
export function IconHash(props: SVGProps<SVGSVGElement>) {
  return (
    <svg
      aria-hidden="true"
      {...props}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M6.3 2.5 4.8 13.5M11.2 2.5 9.7 13.5M3 6.2h10.3M2.7 9.8H13" />
    </svg>
  );
}

export function IconStack(props: SVGProps<SVGSVGElement>) {
  return (
    <svg
      aria-hidden="true"
      {...props}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinejoin="round"
    >
      <path d="M8 2.5 2.5 5 8 7.5 13.5 5 8 2.5Z" />
      <path d="M2.5 8 8 10.5 13.5 8" />
      <path d="M2.5 11 8 13.5 13.5 11" />
    </svg>
  );
}

export function IconExchange(props: SVGProps<SVGSVGElement>) {
  return (
    <svg
      aria-hidden="true"
      {...props}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M3 6h9l-2-2M13 10H4l2 2" />
    </svg>
  );
}

export function IconTrend(props: SVGProps<SVGSVGElement>) {
  return (
    <svg
      aria-hidden="true"
      {...props}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M3 11.5 6.5 8l2.5 2 4-5" />
      <path d="M13 5.5V8.5M13 5.5H10" />
    </svg>
  );
}

export function IconDoc(props: SVGProps<SVGSVGElement>) {
  return (
    <svg
      aria-hidden="true"
      {...props}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinejoin="round"
    >
      <path d="M4 2.5h5l3 3v8h-8v-11Z" />
      <path d="M9 2.5v3h3" />
    </svg>
  );
}

export function IconArchive(props: SVGProps<SVGSVGElement>) {
  return (
    <svg
      aria-hidden="true"
      {...props}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinejoin="round"
      strokeLinecap="round"
    >
      <rect x="2.5" y="3" width="11" height="3" rx="0.7" />
      <path d="M3.5 6v7.5h9V6" />
      <path d="M6.5 9h3" />
    </svg>
  );
}

export function IconClock(props: SVGProps<SVGSVGElement>) {
  return (
    <svg
      aria-hidden="true"
      {...props}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <circle cx="8" cy="8" r="5.5" />
      <path d="M8 5v3l2 1.2" />
    </svg>
  );
}

export function IconWallet(props: SVGProps<SVGSVGElement>) {
  return (
    <svg
      aria-hidden="true"
      {...props}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinejoin="round"
    >
      <rect x="2.5" y="4" width="11" height="9" rx="1.5" />
      <path d="M10 8.5h2" />
    </svg>
  );
}

export function IconRules(props: SVGProps<SVGSVGElement>) {
  return (
    <svg
      aria-hidden="true"
      {...props}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M3 4.5h6M3 8h10M3 11.5h4" />
      <circle cx="11.5" cy="4.5" r="1.3" />
      <circle cx="9.5" cy="11.5" r="1.3" />
    </svg>
  );
}

export function IconGrid(props: SVGProps<SVGSVGElement>) {
  return (
    <svg
      aria-hidden="true"
      {...props}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinejoin="round"
    >
      <rect x="2.5" y="2.5" width="4.5" height="4.5" rx="1" />
      <rect x="9" y="2.5" width="4.5" height="4.5" rx="1" />
      <rect x="2.5" y="9" width="4.5" height="4.5" rx="1" />
      <rect x="9" y="9" width="4.5" height="4.5" rx="1" />
    </svg>
  );
}

export function IconShield(props: SVGProps<SVGSVGElement>) {
  return (
    <svg
      aria-hidden="true"
      {...props}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M8 2 3 4v4c0 3 2.2 5 5 6 2.8-1 5-3 5-6V4L8 2Z" />
      <path d="m6 8 1.4 1.5L10 6.5" />
    </svg>
  );
}

export function IconCloud(props: SVGProps<SVGSVGElement>) {
  return (
    <svg
      aria-hidden="true"
      {...props}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinejoin="round"
    >
      <path d="M5 11.5a2.5 2.5 0 0 1-.4-4.97 3.5 3.5 0 0 1 6.85.97A2.5 2.5 0 0 1 11.5 11.5H5Z" />
    </svg>
  );
}

export function IconCheck(props: SVGProps<SVGSVGElement>) {
  return (
    <svg
      aria-hidden="true"
      {...props}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="m3.5 8.2 3 3L13 4.5" />
    </svg>
  );
}

export function IconMinus(props: SVGProps<SVGSVGElement>) {
  return (
    <svg
      aria-hidden="true"
      {...props}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M4 8h8" />
    </svg>
  );
}

export function IconCheckAll(props: SVGProps<SVGSVGElement>) {
  return (
    <svg
      aria-hidden="true"
      {...props}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="m1.5 8.5 3 3L9 7" />
      <path d="m6.5 11.5 1 1L14.5 5" />
    </svg>
  );
}

export function IconQuestion(props: SVGProps<SVGSVGElement>) {
  return (
    <svg
      aria-hidden="true"
      {...props}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M5.5 6.2A2.5 2.5 0 1 1 8 8.5v1.2" />
      <circle cx="8" cy="12" r="0.6" fill="currentColor" stroke="none" />
    </svg>
  );
}

export function IconAlert(props: SVGProps<SVGSVGElement>) {
  return (
    <svg
      aria-hidden="true"
      {...props}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M8 2 1.5 13h13L8 2Z" />
      <path d="M8 6.5v3" />
      <circle cx="8" cy="11.5" r="0.5" fill="currentColor" stroke="none" />
    </svg>
  );
}

export function IconArrowRight(props: SVGProps<SVGSVGElement>) {
  return (
    <svg
      aria-hidden="true"
      {...props}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M3 8h10M9 4l4 4-4 4" />
    </svg>
  );
}

export function IconEye(props: SVGProps<SVGSVGElement>) {
  return (
    <svg
      aria-hidden="true"
      {...props}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M1.5 8s2.4-4.5 6.5-4.5S14.5 8 14.5 8s-2.4 4.5-6.5 4.5S1.5 8 1.5 8Z" />
      <circle cx="8" cy="8" r="1.9" />
    </svg>
  );
}

export function IconEyeOff(props: SVGProps<SVGSVGElement>) {
  return (
    <svg
      aria-hidden="true"
      {...props}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M6.3 3.7A6.6 6.6 0 0 1 8 3.5c4.1 0 6.5 4.5 6.5 4.5a11 11 0 0 1-1.9 2.4M9.7 9.7a1.9 1.9 0 0 1-2.7-2.7" />
      <path d="M4.2 4.9A11 11 0 0 0 1.5 8s2.4 4.5 6.5 4.5a6.6 6.6 0 0 0 2.1-.33" />
      <path d="m2 2 12 12" />
    </svg>
  );
}

// Two-arrow circular "sync" glyph (the refresh-cw shape), spun via animate-spin
// while a price refresh is in flight.
export function IconRefresh(props: SVGProps<SVGSVGElement>) {
  return (
    <svg
      aria-hidden="true"
      {...props}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M2 8a6 6 0 0 1 6-6 6.5 6.5 0 0 1 4.49 1.83L14 5.33" />
      <path d="M14 2v3.33h-3.33" />
      <path d="M14 8a6 6 0 0 1-6 6 6.5 6.5 0 0 1-4.49-1.83L2 10.67" />
      <path d="M2 14v-3.33h3.33" />
    </svg>
  );
}
