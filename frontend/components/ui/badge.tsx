import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { Slot } from "radix-ui";

import { cn } from "@/lib/utils";

// shadcn Badge (https://ui.shadcn.com/docs/components/base/badge), themed to the
// app's tokens like button.tsx / pill.tsx. A `<span>` (not a button) so it can
// host a nested remove `<button>` — the label-chip's whole affordance. `asChild`
// routes through Radix `Slot` for the rare case a badge wraps a link.
const badgeVariants = cva(
  "inline-flex items-center justify-center gap-1 rounded-md border text-[11px] font-medium leading-none whitespace-nowrap transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring [&_svg]:pointer-events-none [&_svg:not([class*='size-'])]:size-3",
  {
    variants: {
      variant: {
        default: "border-border bg-muted/60 text-foreground/80",
        secondary: "border-transparent bg-secondary text-secondary-foreground",
        outline: "border-border text-muted-foreground",
      },
      size: {
        default: "h-5 px-1.5",
        sm: "h-[18px] px-1.5",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  },
);

function Badge({
  className,
  variant,
  size,
  asChild = false,
  ...props
}: React.ComponentProps<"span"> &
  VariantProps<typeof badgeVariants> & {
    asChild?: boolean;
  }) {
  const Comp = asChild ? Slot.Root : "span";

  return (
    <Comp
      data-slot="badge"
      className={cn(badgeVariants({ variant, size, className }))}
      {...props}
    />
  );
}

export { Badge, badgeVariants };
