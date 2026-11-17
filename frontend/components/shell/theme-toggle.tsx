"use client";

import { Button } from "@/components/ui/button";
import { IconMoon, IconSun } from "@/components/icons";
import { useTheme } from "@/components/theme-provider";

export function ThemeToggle() {
  const { isDark, toggleTheme } = useTheme();

  return (
    <Button
      type="button"
      variant="outline"
      size="icon"
      onClick={toggleTheme}
      aria-label={isDark ? "Switch to light theme" : "Switch to dark theme"}
      title={isDark ? "Switch to light" : "Switch to dark"}
    >
      {isDark ? (
        <IconSun className="size-3.5" />
      ) : (
        <IconMoon className="size-3.5" />
      )}
    </Button>
  );
}
