import type { ComponentType, SVGProps } from "react";
import Link from "next/link";
import { cn } from "@/lib/utils";

export function SidebarItem({
  href,
  label,
  icon: Icon,
  badge,
  active,
  disabled,
}: {
  href: string;
  label: string;
  icon: ComponentType<SVGProps<SVGSVGElement>>;
  badge?: string | number;
  active?: boolean;
  disabled?: boolean;
}) {
  // Route not built yet — show it muted and non-interactive so the rail stays
  // complete without 404-ing on click.
  if (disabled) {
    return (
      <span
        title="Coming soon"
        aria-disabled
        className="flex h-9 cursor-not-allowed items-center gap-2.5 rounded-[4px] px-2 text-[12.5px] text-muted-foreground/50"
        style={{ letterSpacing: "-0.003em" }}
      >
        <Icon className="size-4 shrink-0" />
        <span>{label}</span>
      </span>
    );
  }

  return (
    <Link
      href={href}
      className={cn(
        "group relative flex h-9 items-center gap-2.5 rounded-[4px] px-2 text-[12.5px] transition-colors duration-100",
        active
          ? "font-medium text-primary"
          : "text-muted-foreground hover:bg-muted hover:text-foreground",
      )}
      style={{ letterSpacing: "-0.003em" }}
    >
      {active && (
        <span
          aria-hidden
          className="absolute -left-3 top-1/2 h-5 w-[2px] -translate-y-1/2 rounded-full bg-primary"
        />
      )}
      <Icon className="size-4 shrink-0" />
      <span>{label}</span>
      {badge !== undefined && (
        <span
          className={cn(
            "ml-auto inline-flex h-4 min-w-4 items-center justify-center rounded-[3px] px-1 text-[10px] font-semibold tabular-nums",
            active
              ? "bg-primary text-primary-foreground"
              : "bg-muted text-muted-foreground",
          )}
        >
          {badge}
        </span>
      )}
    </Link>
  );
}
