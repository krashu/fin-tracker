import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const pillVariants = cva(
  "inline-flex items-center gap-1.5 rounded-md border whitespace-nowrap text-[12px] transition-colors duration-100 outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50",
  {
    variants: {
      size: {
        default: "h-7 pl-2.5 pr-2",
        sm: "h-6 px-2 text-[11px]",
      },
      tone: {
        default:
          "border-border bg-card text-muted-foreground hover:bg-muted hover:text-foreground",
        accent:
          "border-ring/10 bg-accent text-accent-foreground hover:bg-accent/80",
      },
    },
    defaultVariants: {
      size: "default",
      tone: "default",
    },
  },
);

type PillProps = React.ComponentProps<"button"> &
  VariantProps<typeof pillVariants> & {
    active?: boolean;
  };

function Pill({ className, size, tone, active, ...props }: PillProps) {
  return (
    <button
      type="button"
      data-slot="pill"
      data-active={active ? "" : undefined}
      className={cn(
        pillVariants({ size, tone: active ? "accent" : tone }),
        className,
      )}
      {...props}
    />
  );
}

export { Pill, pillVariants };
