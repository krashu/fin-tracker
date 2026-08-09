"use client";

/**
 * Shared form primitives for the app's dialogs (expenses entry/transfer,
 * settings account CRUD, statement upload). These are app-composed, not shadcn
 * copies, so they live under components/form/ rather than components/ui/.
 *
 * Not used by transaction-dialog.tsx — its picker/label intentionally differ
 * (shorter trigger, no width-match), so it keeps its own primitives.
 */
import { cloneElement, createContext, isValidElement, useContext, useId } from "react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { IconChevronDown } from "@/components/icons";
import { cn } from "@/lib/utils";

/**
 * The id of the caption a non-associable Field rendered, for a control that has
 * to name itself with `aria-labelledby` because `for` can't reach it. Published
 * through context rather than `cloneElement` because the consuming trigger is
 * not always Field's direct child.
 */
const CaptionIdContext = createContext<string | undefined>(undefined);

export function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  const id = useId();
  // Associate the caption with its control ONLY when the single child is one
  // that will actually receive the cloned id — a native input/select/textarea or
  // our TextInput (which spreads props onto its <input>). A dropdown trigger
  // can't be a `for` target and a file input is wrapped in its own <label>, so
  // for those the caption is a <span> instead: a <label> with neither `for` nor
  // a wrapped control is invalid HTML and names nothing.
  const target = isValidElement(children) ? children : null;
  const associable =
    target !== null &&
    (target.type === TextInput ||
      (typeof target.type === "string" &&
        ["input", "select", "textarea"].includes(target.type)));
  const captionClass =
    "text-[10.5px] font-medium uppercase text-muted-foreground";
  const captionStyle = { letterSpacing: "0.08em" };
  return (
    <div className="flex flex-col gap-1.5">
      {associable ? (
        <label htmlFor={id} className={captionClass} style={captionStyle}>
          {label}
        </label>
      ) : (
        <span id={`${id}caption`} className={captionClass} style={captionStyle}>
          {label}
        </span>
      )}
      {associable ? (
        cloneElement(target as React.ReactElement<{ id?: string }>, { id })
      ) : (
        <CaptionIdContext.Provider value={`${id}caption`}>
          {children}
        </CaptionIdContext.Provider>
      )}
    </div>
  );
}

/**
 * `aria-labelledby` for a dropdown trigger sitting inside a Field: the Field's
 * caption plus the element holding the current value. Both tokens are needed —
 * `aria-labelledby` REPLACES content-derived naming rather than prepending to
 * it, so the caption alone would announce "Type, menu button" and lose the
 * value the trigger's text was carrying. Two tokens give "Type, Credit card".
 *
 * The value element is referenced, not the button, because Radix's
 * DropdownMenuTrigger already owns the button's `id` and its menu points
 * `aria-labelledby` back at it.
 *
 * Returns undefined outside a Field — nothing to name the control with, so the
 * attribute is omitted rather than left dangling.
 */
export function usePickerLabelledBy(valueId: string): string | undefined {
  const captionId = useContext(CaptionIdContext);
  return captionId ? `${captionId} ${valueId}` : undefined;
}

export function TextInput({
  value,
  onChange,
  ...props
}: Omit<React.ComponentProps<"input">, "onChange" | "value"> & {
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <input
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="w-full rounded-md border border-border bg-background px-2.5 py-2 text-[12.5px] text-foreground placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      {...props}
    />
  );
}

export function PickerButton({
  label,
  muted,
  disabled,
  children,
}: {
  label: string;
  muted?: boolean;
  /** Renders the current value but refuses to open. Used where the server
   * forbids the edit — e.g. a transaction linked as a transfer leg, whose
   * identity fields and type are frozen until it is unlinked (ADR-0007 rule 7). */
  disabled?: boolean;
  children: React.ReactNode;
}) {
  const valueId = useId();
  const labelledBy = usePickerLabelledBy(valueId);
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          type="button"
          variant="outline"
          aria-labelledby={labelledBy}
          disabled={disabled}
          className="h-9 w-full justify-between px-2.5 text-[12.5px] font-normal"
        >
          <span id={valueId} className={cn(muted && "text-muted-foreground")}>
            {label}
          </span>
          <IconChevronDown className="size-3 text-muted-foreground" />
        </Button>
      </DropdownMenuTrigger>
      {/* max-h-72 caps long lists with a scroll. A no-op for short pickers
          (e.g. account types/currencies, ≤4 items) but kept here so any
          consumer with a long list gets the cap for free. */}
      <DropdownMenuContent
        align="start"
        className="max-h-72 w-[--radix-dropdown-menu-trigger-width]"
      >
        {children}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
