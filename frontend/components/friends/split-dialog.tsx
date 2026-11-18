"use client";

/**
 * Split with Friend / Fronted for Friend Dialog (PRD §F3b, ADR-0017).
 *
 * Allows allocating a transaction (in full or in part) to a peer contact.
 * Enforces transaction allocation guard (allocated amount <= transaction amount).
 * Supports upfront cashback / discount offset inputs (original - discount).
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
import { Field, PickerButton, TextInput } from "@/components/form/fields";
import { IconCheck, IconPlus, IconUsers } from "@/components/icons";
import { Sensitive } from "@/components/balance-visibility";
import { formatINR, paiseToRupees, rupeesToPaise } from "@/lib/format";
import {
  ApiError,
  createPeerContact,
  createPeerEntry,
  listPeerContacts,
  type PeerDirection,
  type TransactionRead,
} from "@/lib/api/client";
import { invalidateFriends } from "@/lib/queries/invalidate";
import { cn } from "@/lib/utils";

interface SplitDialogProps {
  transaction: TransactionRead;
  open: boolean;
  onClose: () => void;
}

export function SplitDialog({ transaction, open, onClose }: SplitDialogProps) {
  const queryClient = useQueryClient();

  // Active contacts list
  const contactsQuery = useQuery({
    queryKey: ["friends", "contacts"],
    queryFn: () => listPeerContacts(false),
  });
  const contacts = contactsQuery.data ?? [];

  const maxPaise = Math.abs(transaction.amount_paise);

  // Form state
  const [selectedContactId, setSelectedContactId] = useState<number | null>(
    null,
  );
  const [isAddingNewContact, setIsAddingNewContact] = useState(false);
  const [newContactName, setNewContactName] = useState("");
  const [newContactPhone, setNewContactPhone] = useState("");
  const [newContactUpi, setNewContactUpi] = useState("");

  const defaultDirection: PeerDirection =
    transaction.transaction_type === "income" ? "you_got" : "you_gave";
  const [direction, setDirection] = useState<PeerDirection>(defaultDirection);

  const [amountRupees, setAmountRupees] = useState(() =>
    paiseToRupees(maxPaise),
  );
  const [notes, setNotes] = useState("");

  // Discount / cashback offset state
  const [hasDiscount, setHasDiscount] = useState(false);
  const [originalRupees, setOriginalRupees] = useState(() =>
    paiseToRupees(maxPaise),
  );
  const [discountRupees, setDiscountRupees] = useState("0.00");

  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  // Recalculate amount if discount fields change
  const handleOriginalChange = (val: string) => {
    setOriginalRupees(val);
    const origPaise = rupeesToPaise(val);
    const discPaise = rupeesToPaise(discountRupees);
    const net = Math.max(0, origPaise - discPaise);
    setAmountRupees(paiseToRupees(net));
  };

  const handleDiscountChange = (val: string) => {
    setDiscountRupees(val);
    const origPaise = rupeesToPaise(originalRupees);
    const discPaise = rupeesToPaise(val);
    const net = Math.max(0, origPaise - discPaise);
    setAmountRupees(paiseToRupees(net));
  };

  const splitMutation = useMutation({
    mutationFn: async () => {
      let contactId = selectedContactId;

      if (isAddingNewContact) {
        if (!newContactName.trim()) {
          throw new Error("Contact name is required");
        }
        const created = await createPeerContact({
          name: newContactName.trim(),
          phone: newContactPhone.trim() || null,
          upi_id: newContactUpi.trim() || null,
        });
        contactId = created.id;
      }

      if (!contactId) {
        throw new Error("Please select or add a contact");
      }

      const allocatedPaise = rupeesToPaise(amountRupees);
      if (allocatedPaise <= 0) {
        throw new Error("Allocated amount must be greater than zero");
      }
      if (allocatedPaise > maxPaise) {
        throw new Error(
          `Allocated amount cannot exceed transaction total (${formatINR(maxPaise)})`,
        );
      }

      let originalPaise: number | undefined;
      let discountPaise: number | undefined;
      if (hasDiscount) {
        originalPaise = rupeesToPaise(originalRupees);
        discountPaise = rupeesToPaise(discountRupees);
        if (originalPaise - discountPaise !== allocatedPaise) {
          throw new Error("Allocated amount must equal original less discount");
        }
      }

      return createPeerEntry({
        contact_id: contactId,
        transaction_id: transaction.id,
        date: transaction.date,
        amount_paise: allocatedPaise,
        direction,
        is_settlement: false,
        notes: notes.trim() || null,
        original_paise: originalPaise,
        discount_paise: discountPaise,
      });
    },
    onSuccess: () => {
      invalidateFriends(queryClient);
      queryClient.invalidateQueries({ queryKey: ["transactions"] });
      queryClient.invalidateQueries({ queryKey: ["dashboards"] });
      onClose();
    },
    onError: (err) => {
      setErrorMsg(
        err instanceof ApiError
          ? err.detail
          : err instanceof Error
            ? err.message
            : "Failed to record split",
      );
    },
  });

  const selectedContact = contacts.find((c) => c.id === selectedContactId);
  const contactPickerLabel = isAddingNewContact
    ? "+ New Contact"
    : selectedContact
      ? selectedContact.name
      : "Select friend / contact";

  return (
    <Dialog open={open} onOpenChange={(isOpen) => !isOpen && onClose()}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <IconUsers className="size-4 text-primary" />
            Split with Friend / Khata
          </DialogTitle>
          <DialogDescription>
            Allocate this transaction to a friend or record debt against your
            peer ledger.
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-3.5 py-1">
          {/* Transaction context banner */}
          <div className="flex items-center justify-between rounded-md border border-border bg-muted/40 px-3 py-2 text-[12px]">
            <span className="text-muted-foreground">
              {transaction.merchant_raw || "Transaction Total"}
            </span>
            <span className="font-semibold text-foreground font-mono">
              <Sensitive>{formatINR(maxPaise)}</Sensitive>
            </span>
          </div>

          {/* Direction toggle */}
          <div className="flex flex-col gap-1.5">
            <span
              className="text-[10.5px] font-medium uppercase text-muted-foreground"
              style={{ letterSpacing: "0.08em" }}
            >
              Direction
            </span>
            <div className="grid grid-cols-2 gap-2">
              <Button
                type="button"
                variant={direction === "you_gave" ? "default" : "outline"}
                className={cn(
                  "h-9 text-[12px] font-medium justify-center",
                  direction === "you_gave" && "bg-emerald-600 hover:bg-emerald-700 text-white",
                )}
                onClick={() => setDirection("you_gave")}
              >
                You Gave (They owe you)
              </Button>
              <Button
                type="button"
                variant={direction === "you_got" ? "default" : "outline"}
                className={cn(
                  "h-9 text-[12px] font-medium justify-center",
                  direction === "you_got" && "bg-amber-600 hover:bg-amber-700 text-white",
                )}
                onClick={() => setDirection("you_got")}
              >
                You Got (You owe them)
              </Button>
            </div>
          </div>

          {/* Contact Picker */}
          <Field label="Friend / Contact">
            <PickerButton label={contactPickerLabel}>
              <DropdownMenuItem
                onSelect={() => {
                  setIsAddingNewContact(true);
                  setSelectedContactId(null);
                }}
              >
                <IconPlus className="size-3 mr-1 text-primary" />
                <span className="font-medium text-primary">Add new contact</span>
              </DropdownMenuItem>
              {contacts.map((c) => (
                <DropdownMenuItem
                  key={c.id}
                  onSelect={() => {
                    setIsAddingNewContact(false);
                    setSelectedContactId(c.id);
                  }}
                >
                  <div className="flex items-center justify-between w-full">
                    <span>{c.name}</span>
                    {selectedContactId === c.id ? (
                      <IconCheck className="size-3.5 text-primary ml-2" />
                    ) : null}
                  </div>
                </DropdownMenuItem>
              ))}
            </PickerButton>
          </Field>

          {/* Inline quick-add if adding new contact */}
          {isAddingNewContact ? (
            <div className="grid gap-2 rounded-md border border-border bg-muted/20 p-2.5">
              <span className="text-[11px] font-medium text-foreground">
                New contact details
              </span>
              <TextInput
                value={newContactName}
                onChange={setNewContactName}
                placeholder="Full Name *"
                autoFocus
              />
              <div className="grid grid-cols-2 gap-2">
                <TextInput
                  value={newContactPhone}
                  onChange={setNewContactPhone}
                  placeholder="Phone (optional)"
                />
                <TextInput
                  value={newContactUpi}
                  onChange={setNewContactUpi}
                  placeholder="UPI ID (optional)"
                />
              </div>
            </div>
          ) : null}

          {/* Upfront discount / cashback toggle */}
          <div className="flex items-center justify-between pt-1">
            <label className="flex items-center gap-2 cursor-pointer text-[12px] text-foreground">
              <input
                type="checkbox"
                checked={hasDiscount}
                onChange={(e) => {
                  setHasDiscount(e.target.checked);
                  if (!e.target.checked) {
                    setAmountRupees(paiseToRupees(maxPaise));
                  }
                }}
                className="rounded border-border text-primary focus:ring-ring"
              />
              <span>Apply card cashback / discount</span>
            </label>
          </div>

          {/* Upfront discount inputs */}
          {hasDiscount ? (
            <div className="grid grid-cols-2 gap-2 rounded-md border border-border bg-muted/20 p-2.5">
              <Field label="Original Amount (₹)">
                <TextInput
                  id="original-amount"
                  type="number"
                  step="0.01"
                  min="0"
                  value={originalRupees}
                  onChange={handleOriginalChange}
                />
              </Field>
              <Field label="Discount Deduction (₹)">
                <TextInput
                  id="discount-deduction"
                  type="number"
                  step="0.01"
                  min="0"
                  value={discountRupees}
                  onChange={handleDiscountChange}
                />
              </Field>
            </div>
          ) : null}

          {/* Allocated Amount */}
          <div className="flex flex-col gap-1.5">
            <div className="flex items-center justify-between">
              <label
                htmlFor="allocated-amount"
                className="text-[10.5px] font-medium uppercase text-muted-foreground"
                style={{ letterSpacing: "0.08em" }}
              >
                Allocated Amount (₹)
              </label>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="h-5 px-1.5 text-[11px] text-primary"
                onClick={() => {
                  setAmountRupees(paiseToRupees(maxPaise));
                  setOriginalRupees(paiseToRupees(maxPaise));
                  setDiscountRupees("0.00");
                }}
              >
                Max ({formatINR(maxPaise)})
              </Button>
            </div>
            <TextInput
              id="allocated-amount"
              type="number"
              step="0.01"
              min="0"
              value={amountRupees}
              onChange={(val) => {
                setAmountRupees(val);
                if (hasDiscount) {
                  setHasDiscount(false);
                }
              }}
            />
          </div>

          {/* Notes */}
          <Field label="Notes (optional)">
            <TextInput
              value={notes}
              onChange={setNotes}
              placeholder="e.g. Dinner split, Amazon order"
            />
          </Field>

          {/* Error display */}
          {errorMsg ? (
            <p className="text-[12px] text-neg">{errorMsg}</p>
          ) : null}
        </div>

        <DialogFooter className="gap-2 sm:gap-0">
          <Button
            type="button"
            variant="ghost"
            onClick={onClose}
            disabled={splitMutation.isPending}
            className="h-8 text-[12.5px]"
          >
            Cancel
          </Button>
          <Button
            type="button"
            onClick={() => {
              setErrorMsg(null);
              splitMutation.mutate();
            }}
            disabled={
              splitMutation.isPending ||
              (!selectedContactId && !isAddingNewContact) ||
              (isAddingNewContact && !newContactName.trim())
            }
            className="h-8 text-[12.5px]"
          >
            {splitMutation.isPending ? "Recording…" : "Record Split"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
