"use client";

import { Button } from "@/components/ui/button";
import { IconEye, IconEyeOff } from "@/components/icons";
import { useBalanceHidden } from "@/components/balance-visibility";

/** Top-bar toggle for hiding all money amounts/balances (over-the-shoulder
 * privacy). Pure client state via {@link useBalanceHidden} — no server round
 * trip, so flicking it is instant. */
export function BalanceToggle() {
  const { hidden, toggle } = useBalanceHidden();
  return (
    <Button
      type="button"
      variant="outline"
      size="icon"
      onClick={toggle}
      aria-pressed={hidden}
      aria-label={hidden ? "Show amounts" : "Hide amounts"}
      title={hidden ? "Show amounts" : "Hide amounts"}
    >
      {hidden ? (
        <IconEyeOff className="size-3.5" />
      ) : (
        <IconEye className="size-3.5" />
      )}
    </Button>
  );
}
