"use client";

/**
 * Manual investment-transaction entry (PRD §F7) — the Add controls in the
 * /investments SubNav trailing slot plus the entry dialog. Curated to
 * buy/sip/sell/dividend/bonus; split + switch_* are CAS-era (the backend rejects
 * them on manual entry).
 *
 * The Type control's axis is an `EntryMode`, not an `InvestmentTransactionType`: one
 * extra mode, "IDCW reinvestment", posts to a different endpoint and writes a linked
 * `dividend` + `buy` pair in one atomic call. It is the only mode needing units AND
 * price AND amount at once, and the only one that forbids a fee.
 *
 * units / per-unit price are entered as decimals and sent verbatim as strings
 * (precision-preserving — never round-tripped through a JS number). Amount and
 * fees are native-currency money (the selected instrument's currency: ₹ for INR,
 * $ for USD) entered as a major-unit magnitude and converted to native minor
 * units — ×100 is correct for both paise and cents. fx_rate is not sent; the
 * backend stamps it from the cached rate for the instrument's currency.
 */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

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
import { IconCheck, IconPlus } from "@/components/icons";
import {
  ApiError,
  createInvestmentTransaction,
  createReinvestment,
  listInstruments,
  type InvestmentTransactionCreate,
  type ReinvestmentCreate,
} from "@/lib/api/client";
import { Field, PickerButton, TextInput } from "@/components/form/fields";
import { rupeesToPaise } from "@/lib/format";
import { toLocalYMD } from "@/lib/dates";
import {
  ENTRY_MODES,
  ENTRY_MODE_LABELS,
  instrumentLabel,
  type EntryMode,
} from "@/lib/investments";
import { AddInstrumentDialog } from "./add-instrument";

export function InvestmentAddControls() {
  const [txnOpen, setTxnOpen] = useState(false);
  const [instrumentOpen, setInstrumentOpen] = useState(false);

  return (
    <div className="flex items-center gap-2">
      <Button
        type="button"
        variant="outline"
        onClick={() => setTxnOpen(true)}
        className="h-7 gap-1.5 px-2.5 text-[12px] font-medium"
      >
        <IconPlus className="size-3" />
        Add transaction
      </Button>
      <Button
        type="button"
        variant="outline"
        onClick={() => setInstrumentOpen(true)}
        className="h-7 gap-1.5 px-2.5 text-[12px] font-medium"
      >
        <IconPlus className="size-3" />
        Add instrument
      </Button>

      {txnOpen ? <EntryDialog onClose={() => setTxnOpen(false)} /> : null}
      {instrumentOpen ? (
        <AddInstrumentDialog onClose={() => setInstrumentOpen(false)} />
      ) : null}
    </div>
  );
}

function EntryDialog({ onClose }: { onClose: () => void }) {
  const queryClient = useQueryClient();
  const instrumentsQuery = useQuery({
    queryKey: ["instruments"],
    queryFn: listInstruments,
  });
  const instruments = instrumentsQuery.data ?? [];

  const [instrumentId, setInstrumentId] = useState<number | null>(null);
  const [type, setType] = useState<EntryMode>("buy");
  const [date, setDate] = useState(toLocalYMD(new Date()));
  const [units, setUnits] = useState("");
  const [price, setPrice] = useState("");
  const [amount, setAmount] = useState("");
  const [fees, setFees] = useState("");
  const [note, setNote] = useState("");
  const [justAdded, setJustAdded] = useState<string | null>(null);

  const selectedInstrument =
    instruments.find((i) => i.id === instrumentId) ?? null;
  // Money labels follow the instrument's currency (₹ INR / $ USD). The wire
  // value is native minor units either way (rupeesToPaise ×100 = paise or cents).
  const moneySymbol = selectedInstrument?.currency === "USD" ? "$" : "₹";

  // A reinvestment is the one mode that is not a single wire type — it posts a pair.
  const isReinvest = type === "reinvestment";

  // Per-mode field visibility (PRD §F7): dividend is a cash payout (no units, no
  // price); bonus is free units (no price, no cashflow); buy/sip/sell are full. A
  // reinvestment needs all three of units/price/amount — the dividend became units at
  // that date's NAV — but no fee: it carries no brokerage and the backend body forbids
  // one, so `showFees` can't just track `showPrice` here.
  const showUnits = type !== "dividend";
  const showPrice =
    type === "buy" || type === "sip" || type === "sell" || isReinvest;
  const showAmount = type !== "bonus";
  const showFees = showPrice && !isReinvest;

  const mutation = useMutation({
    // Returns void, not the created row(s): the two endpoints have different response
    // shapes (one row vs a named pair) and this form consumes neither — the success
    // banner reads the selected instrument and the board refetches via invalidation.
    mutationFn: async (): Promise<void> => {
      if (isReinvest) {
        const body: ReinvestmentCreate = {
          date,
          instrument_id: instrumentId!,
          amount_native_paise: rupeesToPaise(amount),
          units: units.trim(),
          price_per_unit_native: price.trim(),
          note: note.trim() || null,
        };
        await createReinvestment(body);
        return;
      }
      const body: InvestmentTransactionCreate = {
        date,
        instrument_id: instrumentId!,
        transaction_type: type,
        units: showUnits ? units.trim() : "0",
        price_per_unit_native: showPrice ? price.trim() : null,
        amount_native_paise: showAmount ? rupeesToPaise(amount) : 0,
        fees_native_paise: showFees ? rupeesToPaise(fees) : 0,
        note: note.trim() || null,
      };
      await createInvestmentTransaction(body);
    },
    onMutate: () => setJustAdded(null),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["investment-transactions"] });
      queryClient.invalidateQueries({ queryKey: ["holdings"] });
      // An investment txn changes portfolio value → net worth on /dashboard
      // (PRD §F9). A mounted dashboards query won't refetch on staleTime alone,
      // so nudge it explicitly — mirrors the expenses board's dashboards nudge.
      queryClient.invalidateQueries({ queryKey: ["dashboards"] });
      // …and the /portfolio tiles + allocation + per-holding XIRR (PRD §F9).
      queryClient.invalidateQueries({ queryKey: ["portfolio"] });
      // Stay open for rapid multi-entry: reset the per-row fields, keep
      // instrument / type / date for the next one.
      setJustAdded(selectedInstrument?.symbol ?? "");
      setUnits("");
      setPrice("");
      setAmount("");
      setFees("");
      setNote("");
    },
  });

  // Validation mirrors the backend per-type rules (display-only parses gate the
  // submit; the exact decimal strings are what get sent).
  const unitsOk = parseFloat(units) > 0;
  const priceOk = parseFloat(price) > 0;
  const amountOk = rupeesToPaise(amount) > 0;
  let typeOk = false;
  if (showPrice)
    typeOk = unitsOk && priceOk && amountOk; // buy/sip/sell + reinvestment
  else if (type === "dividend") typeOk = amountOk;
  else if (type === "bonus") typeOk = unitsOk;

  const canSubmit =
    instrumentId != null && date !== "" && typeOk && !mutation.isPending;

  return (
    <Dialog open onOpenChange={(open) => (open ? null : onClose())}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Add investment transaction</DialogTitle>
          <DialogDescription className="sr-only">
            Record a buy, SIP, sell, dividend, bonus, or IDCW reinvestment
            against one of your instruments.
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-3">
          <Field label="Instrument">
            <PickerButton
              label={
                selectedInstrument
                  ? instrumentLabel(selectedInstrument)
                  : "Select an instrument"
              }
              muted={selectedInstrument == null}
            >
              {instruments.length === 0 ? (
                <DropdownMenuItem disabled>
                  <span className="text-muted-foreground">
                    No instruments yet — add one first
                  </span>
                </DropdownMenuItem>
              ) : (
                instruments.map((i) => (
                  <DropdownMenuItem
                    key={i.id}
                    onSelect={() => setInstrumentId(i.id)}
                  >
                    {instrumentLabel(i)}
                  </DropdownMenuItem>
                ))
              )}
            </PickerButton>
          </Field>

          <div className="grid grid-cols-2 gap-3">
            <Field label="Type">
              <PickerButton label={ENTRY_MODE_LABELS[type]}>
                {ENTRY_MODES.map((t) => (
                  <DropdownMenuItem key={t} onSelect={() => setType(t)}>
                    {ENTRY_MODE_LABELS[t]}
                  </DropdownMenuItem>
                ))}
              </PickerButton>
            </Field>
            <Field label="Date">
              <input
                type="date"
                value={date}
                onChange={(e) => setDate(e.target.value)}
                className="w-full rounded-md border border-border bg-background px-2.5 py-2 text-[12.5px] text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              />
            </Field>
          </div>

          <div className="grid grid-cols-2 gap-3">
            {showUnits ? (
              <Field label="Units">
                <TextInput
                  value={units}
                  onChange={setUnits}
                  placeholder="0.0000"
                  inputMode="decimal"
                  type="number"
                  step="0.00000001"
                  min="0"
                />
              </Field>
            ) : null}
            {showPrice ? (
              <Field label={`Price / unit (${moneySymbol})`}>
                <TextInput
                  value={price}
                  onChange={setPrice}
                  placeholder="0.0000"
                  inputMode="decimal"
                  type="number"
                  step="0.0001"
                  min="0"
                />
              </Field>
            ) : null}
            {showAmount ? (
              <Field label={`Amount (${moneySymbol})`}>
                <TextInput
                  value={amount}
                  onChange={setAmount}
                  placeholder="0.00"
                  inputMode="decimal"
                  type="number"
                  step="0.01"
                  min="0"
                />
              </Field>
            ) : null}
            {showFees ? (
              <Field label={`Fees (${moneySymbol}, optional)`}>
                <TextInput
                  value={fees}
                  onChange={setFees}
                  placeholder="0.00"
                  inputMode="decimal"
                  type="number"
                  step="0.01"
                  min="0"
                />
              </Field>
            ) : null}
          </div>

          {isReinvest ? (
            // Two rows land on the board for one entry — say so up front rather than
            // letting it read as a double-submit. Amount is authoritative for cost
            // basis; units × price need not tie to it exactly (AMC statements round
            // units to 3 dp), which is why the backend cross-checks neither.
            <p className="text-[11.5px] text-muted-foreground">
              Records two linked rows: a Dividend for the payout and a Buy for the
              units acquired at that date’s NAV, which opens its own lot. No fee.
            </p>
          ) : null}

          <Field label="Note (optional)">
            <textarea
              value={note}
              onChange={(e) => setNote(e.target.value)}
              maxLength={1024}
              rows={2}
              placeholder="Add a note…"
              className="w-full resize-none rounded-md border border-border bg-background px-2.5 py-2 text-[12.5px] text-foreground placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
          </Field>
        </div>

        {mutation.isError ? (
          <p className="text-[12px] text-neg">
            {mutation.error instanceof ApiError
              ? mutation.error.detail
              : "Couldn’t save — try again."}
          </p>
        ) : justAdded ? (
          <p className="inline-flex items-center gap-1.5 text-[12px] text-pos">
            <IconCheck className="size-3.5" />
            Added to “{justAdded}”. Add another, or Done.
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
            Done
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
