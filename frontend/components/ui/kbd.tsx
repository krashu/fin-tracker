import * as React from "react";
import { cn } from "@/lib/utils";

function Kbd({ className, ...props }: React.ComponentProps<"kbd">) {
  return (
    <kbd
      data-slot="kbd"
      className={cn(
        "inline-flex items-center gap-0.5 rounded-sm border border-border bg-background px-1.5 py-[1px] font-mono text-[10px] font-medium tracking-[0.02em] text-muted-foreground",
        className,
      )}
      {...props}
    />
  );
}

export { Kbd };
