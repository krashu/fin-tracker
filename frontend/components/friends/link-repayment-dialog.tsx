"use client";

/**
 * Link as Friend Repayment Dialog (ADR-0017 Rule 6, PRD §F3b).
 *
 * Links an incoming bank or UPI credit to a peer contact as a settlement credit
 * (direction = 'you_got', is_settlement = True).
 * Enforces transaction allocation guard (repayment amount <= incoming magnitude).
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
import { IconCheck, IconPlus } from "@/components/icons";
import { Sensitive } from "@/components/balance-visibility";
import { formatINR, paiseToRupees, rupeesToPaise } from "@/lib/format";
import {
  ApiError,
  createPeerContact,
  createPeerEntry,
  listPeerContacts,
  type TransactionRead,
} from "@/lib/api/client";
import { invalidateFriends } from "@/lib/queries/invalidate";

interface LinkRepaymentDialogProps {
  transaction: TransactionRead;
  open: boolean;
  onClose: () => void;
}

export function LinkRepaymentDialog({
  transaction,
  open,
  onClose,
}: LinkRepaymentDialogProps) {
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

  const [amountRupees, setAmountRupees] = useState(() =>
    paiseToRupees(maxPaise),
  );
  const [notes, setNotes] = useState("");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const selectedContact = contacts.find((c) => c.id === selectedContactId);

  const repaymentMutation = useMutation({
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
        throw new Error("Please select or add a friend");
      }

      const allocatedPaise = rupeesToPaise(amountRupees);
      if (allocatedPaise <= 0) {
        throw new Error("Repayment amount must be greater than zero");
      }
      if (allocatedPaise > maxPaise) {
        throw new Error(
          `Repayment amount cannot exceed incoming credit (${formatINR(maxPaise)})`,
        );
      }

      return createPeerEntry({
        contact_id: contactId,
        transaction_id: transaction.id,
        date: transaction.date,
        amount_paise: allocatedPaise,
        direction: "you_got",
        is_settlement: true,
        notes: notes.trim() || null,
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
            : "Failed to link repayment",
      );
    },
  });

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
            <IconCheck className="size-4 text-emerald-600 dark:text-emerald-400" />
            Link as Friend Repayment
          </DialogTitle>
          <DialogDescription>
            Record this incoming credit as a friend&apos;s repayment towards
            their outstanding balance.
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-3.5 py-1">
          {/* Transaction banner */}
          <div className="flex items-center justify-between rounded-md border border-border bg-muted/40 px-3 py-2 text-[12px]">
            <span className="text-muted-foreground">
              {transaction.merchant_raw || "Incoming Credit"}
            </span>
            <span className="font-semibold text-emerald-600 dark:text-emerald-400 font-mono">
              +<Sensitive>{formatINR(maxPaise)}</Sensitive>
            </span>
          </div>

          {/* Contact Picker */}
          <Field label="Friend / Contact *">
            <PickerButton
              label={contactPickerLabel}
              disabled={repaymentMutation.isPending}
            >
              <DropdownMenuItem
                onSelect={() => {
                  setIsAddingNewContact(true);
                  setSelectedContactId(null);
                }}
                className="gap-2 font-medium text-primary"
              >
                <IconPlus className="size-3.5" />
                Add New Friend
              </DropdownMenuItem>
              {contacts.map((c) => (
                <DropdownMenuItem
                  key={c.id}
                  onSelect={() => {
                    setIsAddingNewContact(false);
                    setSelectedContactId(c.id);
                  }}
                  className="flex items-center justify-between gap-2"
                >
                  <span className="truncate">{c.name}</span>
                  <span className="font-mono text-[11px] text-muted-foreground shrink-0">
                    {c.balance_paise > 0 ? (
                      <span className="text-emerald-600 dark:text-emerald-400">
                        owes <Sensitive>{formatINR(c.balance_paise)}</Sensitive>
                      </span>
                    ) : c.balance_paise < 0 ? (
                      <span className="text-amber-600 dark:text-amber-400">
                        you owe{" "}
                        <Sensitive>
                          {formatINR(Math.abs(c.balance_paise))}
                        </Sensitive>
                      </span>
                    ) : (
                      "settled"
                    )}
                  </span>
                </DropdownMenuItem>
              ))}
            </PickerButton>
          </Field>

          {/* New contact inline fields */}
          {isAddingNewContact ? (
            <div className="grid gap-2.5 rounded-md border border-border/80 bg-muted/20 p-3">
              <span className="text-[11.5px] font-semibold text-foreground">
                New Friend Details
              </span>
              <TextInput
                value={newContactName}
                onChange={setNewContactName}
                placeholder="Friend's Name *"
                autoFocus
              />
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
          ) : null}

          {/* Repayment Amount input */}
          <Field label="Repayment Amount (₹) *">
            <TextInput
              type="number"
              step="0.01"
              min="0"
              max={paiseToRupees(maxPaise)}
              value={amountRupees}
              onChange={setAmountRupees}
              placeholder="0.00"
            />
          </Field>

          {/* Notes input */}
          <Field label="Notes (optional)">
            <TextInput
              value={notes}
              onChange={setNotes}
              placeholder="e.g. Dinner repayment via UPI"
            />
          </Field>

          {errorMsg ? <p className="text-[12px] text-neg">{errorMsg}</p> : null}
        </div>

        <DialogFooter className="gap-2 sm:gap-0 pt-2">
          <Button
            type="button"
            variant="ghost"
            onClick={onClose}
            disabled={repaymentMutation.isPending}
            className="h-8 text-[12.5px]"
          >
            Cancel
          </Button>
          <Button
            type="button"
            onClick={() => {
              setErrorMsg(null);
              repaymentMutation.mutate();
            }}
            disabled={
              repaymentMutation.isPending ||
              (!selectedContactId && !newContactName.trim()) ||
              !amountRupees ||
              rupeesToPaise(amountRupees) <= 0 ||
              rupeesToPaise(amountRupees) > maxPaise
            }
            className="h-8 text-[12.5px]"
          >
            {repaymentMutation.isPending ? "Linking…" : "Confirm Repayment"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
