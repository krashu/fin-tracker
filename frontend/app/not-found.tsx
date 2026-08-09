/**
 * Root not-found boundary — catches every unmatched route in the app. Renders
 * inside the root layout, so `RouteGuard` still applies: an authenticated user
 * gets this inside `AppShell`, an unauthenticated one is bounced to /login
 * before it paints (a bogus URL is not a public route).
 */
import Link from "next/link";

import { PageHeader } from "@/components/shell/page-header";
import { Button } from "@/components/ui/button";
import { IconArrowRight } from "@/components/icons";

export default function NotFound() {
  return (
    <div className="mx-auto max-w-[1240px] px-4 pb-10 sm:px-6 lg:px-10">
      <PageHeader
        title="Page not found"
        description="That URL doesn’t exist. It may have been renamed, or the link that brought you here is out of date."
      />
      <Button asChild className="h-7 gap-1.5 px-2.5 text-[12px] font-medium">
        <Link href="/dashboard">
          Go to dashboard
          <IconArrowRight className="size-3" />
        </Link>
      </Button>
    </div>
  );
}
