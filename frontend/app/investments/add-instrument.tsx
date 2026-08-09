"use client";

/**
 * Add-instrument dialog (PRD §F7). Registers a scheme/ticker the user holds.
 * Currency isn't a field — it's derived from the asset class: US classes
 * (`us_equity` / `us_etf`) are USD (priced in USD via Yahoo), everything else is
 * INR. That mirrors the backend rule, which *requires* USD for US classes and
 * would 422 an INR stamp on them.
 *
 * Two fields exist to keep this form a first-class registration path rather than a
 * second-class one:
 *  • ISIN — the ONLY key AMFI NAVAll is matched on. A fund registered without one can
 *    never be auto-priced, and it is write-once server-side, so the moment to supply it
 *    is here. It used to be settable only by the CSV importer.
 *  • NAV as of — the date the price is valid for, not the moment it was typed. Shown only
 *    once a NAV is entered, because the server rejects one without the other. `max` is
 *    today, mirroring the server's future-date 422 so the browser catches it first.
 */
import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { DropdownMenuItem } from "@/components/ui/dropdown-menu";
import {
  ApiError,
  createInstrument,
  type AssetClass,
  type Exchange,
  type InstrumentCreate,
} from "@/lib/api/client";
import { Field, PickerButton, TextInput } from "@/components/form/fields";
import { toLocalYMD } from "@/lib/dates";
import { ASSET_CLASS_LABELS, EXCHANGE_LABELS } from "@/lib/investments";

const ASSET_CLASSES = Object.keys(ASSET_CLASS_LABELS) as AssetClass[];
const EXCHANGES = Object.keys(EXCHANGE_LABELS) as Exchange[];

export function AddInstrumentDialog({ onClose }: { onClose: () => void }) {
  const queryClient = useQueryClient();
  const [symbol, setSymbol] = useState("");
  const [name, setName] = useState("");
  const [assetClass, setAssetClass] = useState<AssetClass>("indian_mf");
  const [exchange, setExchange] = useState<Exchange>("MFCentral");
  const [isin, setIsin] = useState("");
  const [nav, setNav] = useState("");
  const today = toLocalYMD(new Date());
  const [navAsOf, setNavAsOf] = useState(today);

  // Currency follows the asset class — the backend requires USD for US classes.
  const currency =
    assetClass === "us_equity" || assetClass === "us_etf" ? "USD" : "INR";
  const navSymbol = currency === "USD" ? "$" : "₹";
  const hasNav = nav.trim() !== "";

  const mutation = useMutation({
    mutationFn: () => {
      const body: InstrumentCreate = {
        symbol: symbol.trim(),
        name: name.trim(),
        asset_class: assetClass,
        currency,
        exchange,
        // Server-normalised (trim + upper); omit when blank.
        isin: isin.trim() || null,
        // Sent verbatim as a decimal string (precision-preserving); omit when blank.
        current_nav: nav.trim() || null,
        // Only travels with a price — a date for no price is a 422.
        nav_as_of: hasNav ? navAsOf : null,
      };
      return createInstrument(body);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["instruments"] });
      onClose();
    },
  });

  const canSubmit =
    symbol.trim() !== "" && name.trim() !== "" && !mutation.isPending;

  return (
    <Dialog open onOpenChange={(open) => (open ? null : onClose())}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Add instrument</DialogTitle>
          <DialogDescription className="sr-only">
            Register a mutual fund or stock you hold.
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-3">
          <div className="grid grid-cols-2 gap-3">
            <Field label="Symbol">
              {/* A scheme handle or ticker — NOT an ISIN. The old placeholder was an
                  ISIN, which steered the value into the wrong field and left the fund
                  unmatchable to AMFI. */}
              <TextInput
                value={symbol}
                onChange={setSymbol}
                placeholder="HDFCNIFTY / AAPL"
                maxLength={64}
              />
            </Field>
            <Field label="Asset class">
              <PickerButton label={ASSET_CLASS_LABELS[assetClass]}>
                {ASSET_CLASSES.map((a) => (
                  <DropdownMenuItem key={a} onSelect={() => setAssetClass(a)}>
                    {ASSET_CLASS_LABELS[a]}
                  </DropdownMenuItem>
                ))}
              </PickerButton>
            </Field>
          </div>

          <Field label="Name">
            <TextInput
              value={name}
              onChange={setName}
              placeholder="e.g. Nifty 50 Index Fund Direct Growth"
              maxLength={256}
            />
          </Field>

          <div className="flex flex-col gap-1.5">
            <Field label="ISIN (optional)">
              <TextInput
                value={isin}
                onChange={setIsin}
                placeholder="INF209KA12Z1"
                maxLength={12}
                autoCapitalize="characters"
                spellCheck={false}
              />
            </Field>
            <p className="text-[11px] leading-snug text-muted-foreground">
              12 characters, from the AMC or broker statement. It’s the only key
              Indian-MF NAVs are matched on — without it this fund stays hand-priced,
              and it can’t be changed after saving.
            </p>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <Field label="Exchange">
              <PickerButton label={EXCHANGE_LABELS[exchange]}>
                {EXCHANGES.map((e) => (
                  <DropdownMenuItem key={e} onSelect={() => setExchange(e)}>
                    {EXCHANGE_LABELS[e]}
                  </DropdownMenuItem>
                ))}
              </PickerButton>
            </Field>
            <Field label={`Current NAV (${navSymbol}, optional)`}>
              <TextInput
                value={nav}
                onChange={setNav}
                placeholder="0.0000"
                inputMode="decimal"
                type="number"
                step="0.0001"
                min="0"
              />
            </Field>
          </div>

          {/* Only once there's a price to date. `max` mirrors the server's future-date
              422 so the picker refuses it before the round-trip. */}
          {hasNav ? (
            <Field label="NAV as of">
              <input
                type="date"
                value={navAsOf}
                max={today}
                onChange={(e) => setNavAsOf(e.target.value)}
                className="w-full rounded-md border border-border bg-background px-2.5 py-2 text-[12.5px] text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              />
            </Field>
          ) : null}
        </div>

        {mutation.isError ? (
          <p className="text-[12px] text-neg">
            {mutation.error instanceof ApiError
              ? mutation.error.detail
              : "Couldn’t save — try again."}
          </p>
        ) : null}

        <DialogFooter>
          <Button
            type="button"
            variant="ghost"
            className="h-8 px-3 text-[12.5px]"
            onClick={onClose}
            disabled={mutation.isPending}
          >
            Cancel
          </Button>
          <Button
            type="button"
            className="h-8 px-3 text-[12.5px]"
            onClick={() => mutation.mutate()}
            disabled={!canSubmit}
          >
            {mutation.isPending ? "Adding…" : "Add"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
