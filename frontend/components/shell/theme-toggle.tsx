"use client";

import { useRouter } from "next/navigation";
import { useTransition } from "react";
import { Button } from "@/components/ui/button";
import { IconMoon, IconSun } from "@/components/icons";

export function ThemeToggle({ isDark }: { isDark: boolean }) {
  const router = useRouter();
  const [isPending, startTransition] = useTransition();

  const handleClick = () => {
    const next = isDark ? "light" : "dark";
    document.cookie = `theme=${next}; path=/; max-age=31536000; samesite=lax`;
    startTransition(() => router.refresh());
  };

  return (
    <Button
      type="button"
      variant="outline"
      size="icon"
      onClick={handleClick}
      aria-label={isDark ? "Switch to light theme" : "Switch to dark theme"}
      title={isDark ? "Switch to light" : "Switch to dark"}
      disabled={isPending}
    >
      {isDark ? (
        <IconSun className="size-3.5" />
      ) : (
        <IconMoon className="size-3.5" />
      )}
    </Button>
  );
}
