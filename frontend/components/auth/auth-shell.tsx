import type { ReactNode } from "react";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { LogoMark } from "@/components/shell/logo-mark";

/**
 * Centered, chrome-less page frame shared by the login and register screens
 * (both rendered outside the app shell by the route guard). Brand mark + a card
 * with the form; an optional footer line for the cross-link.
 */
export function AuthShell({
  title,
  subtitle,
  children,
  footer,
}: {
  title: string;
  subtitle?: string;
  children: ReactNode;
  footer?: ReactNode;
}) {
  return (
    <div className="grid min-h-screen place-items-center bg-background px-4">
      <div className="w-full max-w-sm">
        <div className="mb-6 flex items-center justify-center gap-2">
          <LogoMark />
          <span className="text-[15px] font-semibold tracking-[-0.012em] text-foreground">
            fin
            <span className="font-normal text-muted-foreground">·</span>
            tracker
          </span>
        </div>
        <Card>
          <CardHeader>
            <CardTitle as="h1">{title}</CardTitle>
            {subtitle ? <CardDescription>{subtitle}</CardDescription> : null}
          </CardHeader>
          <CardContent>{children}</CardContent>
        </Card>
        {footer ? (
          <p className="mt-4 text-center text-[12.5px] text-muted-foreground">
            {footer}
          </p>
        ) : null}
      </div>
    </div>
  );
}
