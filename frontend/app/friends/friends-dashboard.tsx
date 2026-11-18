"use client";

/**
 * Friends & Khata Dashboard (PRD §F3b, ADR-0017).
 *
 * Provides peer debts & shared expenses overview, contact running balances,
 * chronological ledger timeline, full settlements, and upfront discount handling.
 */
import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Field, PickerButton, TextInput } from "@/components/form/fields";
import {
  IconCheck,
  IconCopy,
  IconDownload,
  IconPlus,
  IconQr,
  IconSearch,
  IconUsers,
  IconX,
} from "@/components/icons";
import { Sensitive } from "@/components/balance-visibility";
import { formatDate, formatINR, paiseToRupees, rupeesToPaise } from "@/lib/format";
import {
  ApiError,
  archivePeerContact,
  createPeerContact,
  createPeerEntry,
  deletePeerEntry,
  exportPeerContactStatement,
  getPeerContact,
  getPeerSummary,
  listPeerContacts,
  listPeerEntries,
  settlePeerContact,
  type PeerContactWithBalance,
  type PeerDirection,
  type PeerEntry,
} from "@/lib/api/client";
import { invalidateFriends } from "@/lib/queries/invalidate";
import { buildUpiUri, formatWhatsAppBreakdown } from "@/lib/upi";
import { QrCodeSvg } from "@/lib/qr";
import { cn } from "@/lib/utils";

export function FriendsDashboard() {
  const queryClient = useQueryClient();

  // Filters & State
  const [includeArchived, setIncludeArchived] = useState(false);
  const [search, setSearch] = useState("");
  const [addContactOpen, setAddContactOpen] = useState(false);
  const [selectedContactId, setSelectedContactId] = useState<number | null>(
    null,
  );

  // Queries (TanStack Query directly)
  const summaryQuery = useQuery({
    queryKey: ["friends", "summary"],
    queryFn: getPeerSummary,
  });

  const contactsQuery = useQuery({
    queryKey: ["friends", "contacts", { includeArchived }],
    queryFn: () => listPeerContacts(includeArchived),
  });

  const summary = summaryQuery.data;
  const contacts = contactsQuery.data ?? [];

  // Filter contacts by search
  const filteredContacts = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return contacts;
    return contacts.filter((c) => {
      const matchName = c.name.toLowerCase().includes(q);
      const matchPhone = c.phone?.toLowerCase().includes(q);
      const matchUpi = c.upi_id?.toLowerCase().includes(q);
      return matchName || matchPhone || matchUpi;
    });
  }, [contacts, search]);

  const activeContact = useMemo(() => {
    if (selectedContactId == null) return null;
    return contacts.find((c) => c.id === selectedContactId) ?? null;
  }, [contacts, selectedContactId]);

  return (
    <div className="space-y-6">
      {/* 1. Summary Banner: 3 Metrics */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
        {/* You will get */}
        <Card className="border-border/80">
          <CardHeader className="pb-2">
            <CardDescription className="text-[12px] font-medium text-emerald-600 dark:text-emerald-400">
              You will get
            </CardDescription>
            <CardTitle className="text-2xl font-bold font-mono tracking-tight text-emerald-600 dark:text-emerald-400">
              <Sensitive>
                {formatINR(summary?.total_receivable_paise ?? 0)}
              </Sensitive>
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-[11px] text-muted-foreground">
              Total friends owe you across active contacts
            </p>
          </CardContent>
        </Card>

        {/* You will give */}
        <Card className="border-border/80">
          <CardHeader className="pb-2">
            <CardDescription className="text-[12px] font-medium text-amber-600 dark:text-amber-400">
              You will give
            </CardDescription>
            <CardTitle className="text-2xl font-bold font-mono tracking-tight text-amber-600 dark:text-amber-400">
              <Sensitive>
                {formatINR(summary?.total_payable_paise ?? 0)}
              </Sensitive>
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-[11px] text-muted-foreground">
              Total you owe friends across active contacts
            </p>
          </CardContent>
        </Card>

        {/* Net Balance */}
        <Card className="border-border/80">
          <CardHeader className="pb-2">
            <CardDescription className="text-[12px] font-medium text-foreground">
              Net Balance
            </CardDescription>
            <CardTitle
              className={cn(
                "text-2xl font-bold font-mono tracking-tight",
                (summary?.net_balance_paise ?? 0) > 0
                  ? "text-emerald-600 dark:text-emerald-400"
                  : (summary?.net_balance_paise ?? 0) < 0
                    ? "text-amber-600 dark:text-amber-400"
                    : "text-foreground",
              )}
            >
              <Sensitive>
                {formatINR(summary?.net_balance_paise ?? 0)}
              </Sensitive>
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-[11px] text-muted-foreground">
              {(summary?.net_balance_paise ?? 0) > 0
                ? "Overall positive position"
                : (summary?.net_balance_paise ?? 0) < 0
                  ? "Overall debt position"
                  : "All settled up"}
            </p>
          </CardContent>
        </Card>
      </div>

      {/* 2. Action & Filter Bar */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex flex-1 items-center gap-2 max-w-sm">
          <div className="relative w-full">
            <IconSearch className="absolute left-2.5 top-1/2 -translate-y-1/2 size-3.5 text-muted-foreground pointer-events-none" />
            <input
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search contacts by name, phone, UPI…"
              className="w-full rounded-md border border-border bg-background pl-8 pr-3 py-1.5 text-[12.5px] text-foreground placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
          </div>
        </div>

        <div className="flex items-center gap-3">
          <label className="flex items-center gap-2 cursor-pointer text-[12px] text-muted-foreground select-none">
            <input
              type="checkbox"
              checked={includeArchived}
              onChange={(e) => setIncludeArchived(e.target.checked)}
              className="rounded border-border text-primary focus:ring-ring"
            />
            <span>Show archived</span>
          </label>

          <Button
            type="button"
            onClick={() => setAddContactOpen(true)}
            className="h-8 gap-1.5 px-3 text-[12px] font-medium"
          >
            <IconPlus className="size-3" />
            Add Friend / Contact
          </Button>
        </div>
      </div>

      {/* 3. Contact List */}
      {contactsQuery.isPending ? (
        <div className="py-12 text-center text-[13px] text-muted-foreground">
          Loading contacts…
        </div>
      ) : filteredContacts.length === 0 ? (
        <Card className="border-dashed">
          <CardContent className="flex flex-col items-center justify-center py-12 text-center">
            <IconUsers className="size-9 text-muted-foreground/60 mb-3" />
            <h3 className="text-[14px] font-medium text-foreground">
              {search ? "No matching contacts found" : "No peer contacts yet"}
            </h3>
            <p className="mt-1 text-[12px] text-muted-foreground max-w-sm">
              {search
                ? `No contact matches "${search}". Try adjusting your query.`
                : "Add friends to track shared expenses, card fronting, and borrow/lend Khata balances."}
            </p>
            {!search && (
              <Button
                type="button"
                onClick={() => setAddContactOpen(true)}
                className="mt-4 h-8 gap-1.5 text-[12px]"
              >
                <IconPlus className="size-3" />
                Add First Contact
              </Button>
            )}
          </CardContent>
        </Card>
      ) : (
        <div className="grid grid-cols-1 gap-3.5 sm:grid-cols-2 lg:grid-cols-3">
          {filteredContacts.map((contact) => (
            <ContactCard
              key={contact.id}
              contact={contact}
              onClick={() => setSelectedContactId(contact.id)}
            />
          ))}
        </div>
      )}

      {/* 4. Add Contact Modal */}
      {addContactOpen ? (
        <AddContactDialog
          open={addContactOpen}
          onClose={() => setAddContactOpen(false)}
        />
      ) : null}

      {/* 5. Contact Ledger Drawer / Dialog */}
      {activeContact ? (
        <ContactLedgerDialog
          contact={activeContact}
          open={activeContact !== null}
          onClose={() => setSelectedContactId(null)}
        />
      ) : null}
    </div>
  );
}

function ContactCard({
  contact,
  onClick,
}: {
  contact: PeerContactWithBalance;
  onClick: () => void;
}) {
  const [copiedUpi, setCopiedUpi] = useState(false);
  const isArchived = contact.archived_at != null;
  const owesUser = contact.balance_paise > 0;
  const userOwes = contact.balance_paise < 0;
  const settled = contact.balance_paise === 0;

  const upiUri =
    owesUser && contact.upi_id
      ? buildUpiUri({
          upiId: contact.upi_id,
          name: contact.name,
          amountPaise: contact.balance_paise,
        })
      : null;

  const handleCopyUpi = async (e: React.MouseEvent) => {
    e.stopPropagation();
    if (!upiUri) return;
    try {
      await navigator.clipboard.writeText(upiUri);
      setCopiedUpi(true);
      setTimeout(() => setCopiedUpi(false), 2000);
    } catch {
      // ignore
    }
  };

  return (
    <Card
      className={cn(
        "cursor-pointer transition-colors hover:border-foreground/30 hover:shadow-xs",
        isArchived && "opacity-60 bg-muted/20",
      )}
      onClick={onClick}
    >
      <CardHeader className="p-4 pb-2">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <CardTitle className="text-[14px] font-semibold text-foreground truncate">
              {contact.name}
            </CardTitle>
            {(contact.phone || contact.upi_id) && (
              <p className="text-[11px] text-muted-foreground truncate mt-0.5">
                {[contact.phone, contact.upi_id].filter(Boolean).join(" · ")}
              </p>
            )}
          </div>
          {isArchived ? (
            <Badge variant="outline" className="text-[10px] shrink-0">
              Archived
            </Badge>
          ) : (
            <Badge variant="default" className="text-[10px] shrink-0 font-normal">
              {contact.active_entries_count}{" "}
              {contact.active_entries_count === 1 ? "entry" : "entries"}
            </Badge>
          )}
        </div>
      </CardHeader>
      <CardContent className="p-4 pt-2">
        <div className="flex items-center justify-between border-t border-border/60 pt-2.5 mt-1">
          <span className="text-[11px] text-muted-foreground">
            {owesUser ? "Owes you" : userOwes ? "You owe" : "Status"}
          </span>
          <div className="flex items-center gap-2">
            <span
              className={cn(
                "font-mono font-medium text-[13px]",
                owesUser && "text-emerald-600 dark:text-emerald-400",
                userOwes && "text-amber-600 dark:text-amber-400",
                settled && "text-muted-foreground",
              )}
            >
              {settled ? (
                "Settled up"
              ) : (
                <Sensitive>{formatINR(Math.abs(contact.balance_paise))}</Sensitive>
              )}
            </span>
            {upiUri ? (
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="h-6 gap-1 px-2 text-[11px]"
                onClick={handleCopyUpi}
                title="Copy UPI Payment Link"
              >
                {copiedUpi ? (
                  <>
                    <IconCheck className="size-3 text-emerald-600 dark:text-emerald-400" />
                    <span>Copied</span>
                  </>
                ) : (
                  <>
                    <IconCopy className="size-3" />
                    <span>Copy UPI</span>
                  </>
                )}
              </Button>
            ) : null}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

function AddContactDialog({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [phone, setPhone] = useState("");
  const [upiId, setUpiId] = useState("");
  const [notes, setNotes] = useState("");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: () =>
      createPeerContact({
        name: name.trim(),
        phone: phone.trim() || null,
        upi_id: upiId.trim() || null,
        notes: notes.trim() || null,
      }),
    onSuccess: () => {
      invalidateFriends(queryClient);
      onClose();
    },
    onError: (err) => {
      if (err instanceof ApiError && err.status === 409) {
        setErrorMsg("An active contact with this name already exists.");
      } else {
        setErrorMsg(
          err instanceof ApiError
            ? err.detail
            : err instanceof Error
              ? err.message
              : "Failed to create contact",
        );
      }
    },
  });

  return (
    <Dialog open={open} onOpenChange={(isOpen) => !isOpen && onClose()}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Add Friend / Contact</DialogTitle>
          <DialogDescription>
            Add a contact to track shared bills, credit card fronting, or personal
            debts.
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (!name.trim()) return;
            setErrorMsg(null);
            mutation.mutate();
          }}
          className="grid gap-3 py-1"
        >
          <Field label="Name *">
            <TextInput
              value={name}
              onChange={setName}
              placeholder="Friend's Name"
              maxLength={100}
              autoFocus
              required
            />
          </Field>

          <Field label="Phone number (optional)">
            <TextInput
              value={phone}
              onChange={setPhone}
              placeholder="e.g. +91 98765 43210"
              maxLength={20}
            />
          </Field>

          <Field label="UPI ID (optional)">
            <TextInput
              value={upiId}
              onChange={setUpiId}
              placeholder="e.g. friend@okhdfcbank"
              maxLength={100}
            />
          </Field>

          <Field label="Notes (optional)">
            <TextInput
              value={notes}
              onChange={setNotes}
              placeholder="e.g. Flatmate, Colleague"
            />
          </Field>

          {errorMsg ? <p className="text-[12px] text-neg">{errorMsg}</p> : null}

          <DialogFooter className="gap-2 sm:gap-0 pt-2">
            <Button
              type="button"
              variant="ghost"
              onClick={onClose}
              disabled={mutation.isPending}
              className="h-8 text-[12.5px]"
            >
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={!name.trim() || mutation.isPending}
              className="h-8 text-[12.5px]"
            >
              {mutation.isPending ? "Adding…" : "Add Contact"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function ContactLedgerDialog({
  contact,
  open,
  onClose,
}: {
  contact: PeerContactWithBalance;
  open: boolean;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();

  const [settleOpen, setSettleOpen] = useState(false);
  const [addEntryOpen, setAddEntryOpen] = useState(false);
  const [archiveConfirmOpen, setArchiveConfirmOpen] = useState(false);
  const [entryToDelete, setEntryToDelete] = useState<PeerEntry | null>(null);

  const [copiedUpi, setCopiedUpi] = useState(false);
  const [copiedWhatsApp, setCopiedWhatsApp] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);

  // Ledger entries query
  const entriesQuery = useQuery({
    queryKey: ["friends", "contacts", contact.id, "entries"],
    queryFn: () => listPeerEntries(contact.id),
  });
  const entries = entriesQuery.data ?? [];

  // Fresh contact details
  const freshContactQuery = useQuery({
    queryKey: ["friends", "contacts", contact.id],
    queryFn: () => getPeerContact(contact.id),
    initialData: contact,
  });
  const currentContact = freshContactQuery.data ?? contact;

  const owesUser = currentContact.balance_paise > 0;
  const userOwes = currentContact.balance_paise < 0;
  const settled = currentContact.balance_paise === 0;

  const upiUri =
    owesUser && currentContact.upi_id
      ? buildUpiUri({
          upiId: currentContact.upi_id,
          name: currentContact.name,
          amountPaise: currentContact.balance_paise,
        })
      : null;

  const handleCopyUpi = async (uri: string) => {
    try {
      await navigator.clipboard.writeText(uri);
      setCopiedUpi(true);
      setTimeout(() => setCopiedUpi(false), 2000);
    } catch {
      // ignore
    }
  };

  const handleCopyWhatsApp = async () => {
    try {
      const text = formatWhatsAppBreakdown({
        contact: currentContact,
        entries,
      });
      await navigator.clipboard.writeText(text);
      setCopiedWhatsApp(true);
      setTimeout(() => setCopiedWhatsApp(false), 2000);
    } catch {
      // ignore
    }
  };

  const handleExportStatement = async () => {
    try {
      setExporting(true);
      setExportError(null);
      const { csvText, filename } = await exportPeerContactStatement(
        currentContact.id,
      );
      const blob = new Blob([csvText], { type: "text/csv;charset=utf-8;" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.setAttribute("download", filename);
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      URL.revokeObjectURL(url);
    } catch (err) {
      setExportError(
        err instanceof ApiError
          ? err.detail
          : err instanceof Error
            ? err.message
            : "Failed to export statement",
      );
    } finally {
      setExporting(false);
    }
  };

  // Archive mutation
  const archiveMutation = useMutation({
    mutationFn: (confirmWithBalance: boolean) =>
      archivePeerContact(currentContact.id, confirmWithBalance),
    onSuccess: () => {
      invalidateFriends(queryClient);
      setArchiveConfirmOpen(false);
      onClose();
    },
  });

  // Delete entry mutation
  const deleteEntryMutation = useMutation({
    mutationFn: (entryId: number) => deletePeerEntry(entryId),
    onSuccess: () => {
      invalidateFriends(queryClient);
      setEntryToDelete(null);
    },
  });

  return (
    <>
      <Dialog open={open} onOpenChange={(isOpen) => !isOpen && onClose()}>
        <DialogContent className="max-w-2xl max-h-[85vh] flex flex-col p-0 overflow-hidden">
          {/* Header */}
          <DialogHeader className="p-6 pb-4 border-b border-border">
            <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
              <div>
                <DialogTitle className="text-xl font-bold text-foreground">
                  {currentContact.name}
                </DialogTitle>
                <DialogDescription className="text-[12px] mt-0.5">
                  {[currentContact.phone, currentContact.upi_id]
                    .filter(Boolean)
                    .join(" · ") || "Peer Ledger"}
                </DialogDescription>
              </div>

              {/* Running Balance Pill */}
              <div className="flex items-center gap-2">
                <span className="text-[12px] text-muted-foreground">Balance:</span>
                <span
                  className={cn(
                    "font-mono font-semibold text-[15px]",
                    owesUser && "text-emerald-600 dark:text-emerald-400",
                    userOwes && "text-amber-600 dark:text-amber-400",
                    settled && "text-muted-foreground",
                  )}
                >
                  {settled ? (
                    "Settled (₹0)"
                  ) : owesUser ? (
                    <>
                      <span className="text-[12px] font-sans font-normal mr-1">
                        owes you
                      </span>
                      <Sensitive>
                        {formatINR(currentContact.balance_paise)}
                      </Sensitive>
                    </>
                  ) : (
                    <>
                      <span className="text-[12px] font-sans font-normal mr-1">
                        you owe
                      </span>
                      <Sensitive>
                        {formatINR(Math.abs(currentContact.balance_paise))}
                      </Sensitive>
                    </>
                  )}
                </span>
              </div>
            </div>

            {/* Actions Bar */}
            <div className="flex flex-wrap items-center justify-between gap-2 pt-3">
              <div className="flex items-center gap-2">
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => setAddEntryOpen(true)}
                  className="h-8 gap-1.5 text-[12px]"
                >
                  <IconPlus className="size-3" />
                  Add Entry
                </Button>

                <Button
                  type="button"
                  size="sm"
                  onClick={() => setSettleOpen(true)}
                  disabled={settled}
                  className="h-8 gap-1.5 text-[12px]"
                >
                  <IconCheck className="size-3.5" />
                  Settle Up
                </Button>
              </div>

              {!currentContact.archived_at ? (
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={() => setArchiveConfirmOpen(true)}
                  className="h-8 text-[12px] text-muted-foreground hover:text-neg"
                >
                  Archive Contact
                </Button>
              ) : (
                <Badge variant="outline">Archived</Badge>
              )}
            </div>
          </DialogHeader>

          {/* Timeline of Entries */}
          <div className="flex-1 overflow-y-auto p-6 space-y-4">
            {/* UPI QR & WhatsApp sharing card */}
            {owesUser && currentContact.upi_id && upiUri ? (
              <div className="flex flex-col sm:flex-row items-center gap-4 rounded-lg border border-border bg-muted/20 p-3.5">
                <div className="shrink-0 bg-white p-2 rounded-md shadow-xs border border-border">
                  <QrCodeSvg
                    value={upiUri}
                    size={110}
                    title={`UPI QR for ${currentContact.name}`}
                  />
                </div>
                <div className="flex flex-col gap-1 min-w-0 text-center sm:text-left flex-1">
                  <div className="flex items-center justify-center sm:justify-start gap-1.5">
                    <IconQr className="size-3.5 text-primary" />
                    <span className="text-[11px] font-semibold uppercase text-muted-foreground tracking-wider">
                      UPI Payment Request
                    </span>
                  </div>
                  <p className="text-[12.5px] font-medium text-foreground truncate">
                    {currentContact.upi_id}
                  </p>
                  <p className="text-[12px] text-muted-foreground font-mono">
                    Payable:{" "}
                    <Sensitive>
                      {formatINR(currentContact.balance_paise)}
                    </Sensitive>
                  </p>
                  <div className="flex flex-wrap items-center justify-center sm:justify-start gap-2 pt-1">
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={() => handleCopyUpi(upiUri)}
                      className="h-7 gap-1.5 px-2 text-[11.5px]"
                    >
                      {copiedUpi ? (
                        <>
                          <IconCheck className="size-3 text-emerald-600 dark:text-emerald-400" />
                          Copied UPI Link
                        </>
                      ) : (
                        <>
                          <IconCopy className="size-3" />
                          Copy UPI Link
                        </>
                      )}
                    </Button>

                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={handleCopyWhatsApp}
                      className="h-7 gap-1.5 px-2 text-[11.5px]"
                    >
                      {copiedWhatsApp ? (
                        <>
                          <IconCheck className="size-3 text-emerald-600 dark:text-emerald-400" />
                          Copied Breakdown
                        </>
                      ) : (
                        <>
                          <IconCopy className="size-3" />
                          Copy Breakdown for WhatsApp
                        </>
                      )}
                    </Button>
                  </div>
                </div>
              </div>
            ) : (
              <div className="flex items-center justify-between rounded-lg border border-border bg-muted/15 px-3 py-2">
                <span className="text-[11.5px] text-muted-foreground">
                  Share unsettled ledger breakdown with {currentContact.name}
                </span>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={handleCopyWhatsApp}
                  className="h-7 gap-1.5 px-2 text-[11.5px]"
                >
                  {copiedWhatsApp ? (
                    <>
                      <IconCheck className="size-3 text-emerald-600 dark:text-emerald-400" />
                      Copied Breakdown
                    </>
                  ) : (
                    <>
                      <IconCopy className="size-3" />
                      Copy Breakdown for WhatsApp
                    </>
                  )}
                </Button>
              </div>
            )}

            <h4 className="text-[12px] font-semibold text-muted-foreground uppercase tracking-wider pt-1">
              Ledger Timeline
            </h4>

            {entriesQuery.isPending ? (
              <p className="text-[12.5px] text-muted-foreground py-6 text-center">
                Loading ledger entries…
              </p>
            ) : entries.length === 0 ? (
              <div className="py-8 text-center text-muted-foreground text-[12.5px]">
                No entries recorded yet. Use &ldquo;Add Entry&rdquo; or split an
                expense to record debt.
              </div>
            ) : (
              <div className="space-y-2">
                {entries.map((entry) => (
                  <div
                    key={entry.id}
                    className="flex items-center justify-between rounded-lg border border-border p-3 hover:bg-muted/30 transition-colors"
                  >
                    <div className="flex flex-col gap-0.5 min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="text-[12px] font-medium text-foreground">
                          {formatDate(entry.date)}
                        </span>
                        {entry.is_settlement ? (
                          <Badge
                            variant="outline"
                            className="text-[10px] border-primary/40 text-primary bg-primary/5"
                          >
                            Settlement
                          </Badge>
                        ) : entry.direction === "you_gave" ? (
                          <Badge
                            variant="secondary"
                            className="text-[10px] bg-emerald-600/10 text-emerald-600 dark:text-emerald-400"
                          >
                            You gave
                          </Badge>
                        ) : (
                          <Badge
                            variant="secondary"
                            className="text-[10px] bg-amber-600/10 text-amber-600 dark:text-amber-400"
                          >
                            You got
                          </Badge>
                        )}
                      </div>
                      <p className="text-[11.5px] text-muted-foreground truncate">
                        {entry.notes || (entry.is_settlement ? "Settled balance" : "Ledger entry")}
                      </p>
                    </div>

                    <div className="flex items-center gap-3 shrink-0">
                      <span
                        className={cn(
                          "font-mono font-semibold text-[13.5px]",
                          entry.direction === "you_gave"
                            ? "text-emerald-600 dark:text-emerald-400"
                            : "text-amber-600 dark:text-amber-400",
                        )}
                      >
                        <Sensitive>{formatINR(entry.amount_paise)}</Sensitive>
                      </span>

                      <Button
                        type="button"
                        variant="ghost"
                        size="icon"
                        onClick={() => setEntryToDelete(entry)}
                        className="size-7 text-muted-foreground hover:text-neg"
                        title="Delete entry"
                        aria-label="Delete entry"
                      >
                        <IconX className="size-3.5" />
                      </Button>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>

          <DialogFooter className="flex flex-row items-center justify-between p-4 border-t border-border bg-muted/10">
            <div className="flex items-center gap-2">
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={handleExportStatement}
                disabled={exporting}
                className="h-8 gap-1.5 text-[12px]"
              >
                <IconDownload className="size-3.5" />
                {exporting ? "Exporting…" : "Export Statement"}
              </Button>
              {exportError ? (
                <span className="text-[11px] text-neg">{exportError}</span>
              ) : null}
            </div>
            <Button
              type="button"
              variant="outline"
              onClick={onClose}
              className="h-8 text-[12px]"
            >
              Close
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Settle Up Modal */}
      {settleOpen ? (
        <SettleModal
          contact={currentContact}
          open={settleOpen}
          onClose={() => setSettleOpen(false)}
        />
      ) : null}

      {/* Add Standalone Entry Modal */}
      {addEntryOpen ? (
        <AddPeerEntryModal
          contact={currentContact}
          open={addEntryOpen}
          onClose={() => setAddEntryOpen(false)}
        />
      ) : null}

      {/* Archive Confirmation Dialog */}
      {archiveConfirmOpen ? (
        <Dialog
          open={archiveConfirmOpen}
          onOpenChange={(isOpen) => !isOpen && setArchiveConfirmOpen(false)}
        >
          <DialogContent className="max-w-md">
            <DialogHeader>
              <DialogTitle>Archive {currentContact.name}?</DialogTitle>
              <DialogDescription>
                Archiving hides this contact from pickers but preserves ledger
                history and transaction links.
              </DialogDescription>
            </DialogHeader>

            {currentContact.balance_paise !== 0 ? (
              <div className="rounded-md border border-amber-600/30 bg-amber-600/10 p-3 text-[12px] text-amber-900 dark:text-amber-200">
                <span className="font-semibold">Notice:</span> This contact has an
                outstanding balance of{" "}
                <span className="font-mono font-medium">
                  <Sensitive>
                    {formatINR(Math.abs(currentContact.balance_paise))}
                  </Sensitive>
                </span>
                . Archiving with a balance requires your explicit confirmation.
              </div>
            ) : null}

            <DialogFooter className="gap-2 sm:gap-0 pt-2">
              <Button
                type="button"
                variant="ghost"
                onClick={() => setArchiveConfirmOpen(false)}
                disabled={archiveMutation.isPending}
                className="h-8 text-[12px]"
              >
                Cancel
              </Button>
              <Button
                type="button"
                variant="destructive"
                onClick={() => archiveMutation.mutate(currentContact.balance_paise !== 0)}
                disabled={archiveMutation.isPending}
                className="h-8 text-[12px]"
              >
                {archiveMutation.isPending ? "Archiving…" : "Confirm Archive"}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      ) : null}

      {/* Delete Entry Confirmation Dialog */}
      {entryToDelete ? (
        <Dialog
          open={entryToDelete !== null}
          onOpenChange={(isOpen) => !isOpen && setEntryToDelete(null)}
        >
          <DialogContent className="max-w-md">
            <DialogHeader>
              <DialogTitle>Delete Ledger Entry?</DialogTitle>
              <DialogDescription>
                Are you sure you want to delete this {formatINR(entryToDelete.amount_paise)} entry?
                This will recalculate the running balance.
              </DialogDescription>
            </DialogHeader>

            <DialogFooter className="gap-2 sm:gap-0 pt-2">
              <Button
                type="button"
                variant="ghost"
                onClick={() => setEntryToDelete(null)}
                disabled={deleteEntryMutation.isPending}
                className="h-8 text-[12px]"
              >
                Cancel
              </Button>
              <Button
                type="button"
                variant="destructive"
                onClick={() => deleteEntryMutation.mutate(entryToDelete.id)}
                disabled={deleteEntryMutation.isPending}
                className="h-8 text-[12px]"
              >
                {deleteEntryMutation.isPending ? "Deleting…" : "Delete Entry"}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      ) : null}
    </>
  );
}

function SettleModal({
  contact,
  open,
  onClose,
}: {
  contact: PeerContactWithBalance;
  open: boolean;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [date, setDate] = useState(() => new Date().toISOString().slice(0, 10));
  const [notes, setNotes] = useState("Full settlement");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const settleMutation = useMutation({
    mutationFn: () =>
      settlePeerContact(contact.id, {
        date,
        notes: notes.trim() || null,
      }),
    onSuccess: () => {
      invalidateFriends(queryClient);
      onClose();
    },
    onError: (err) => {
      setErrorMsg(
        err instanceof ApiError
          ? err.detail
          : err instanceof Error
            ? err.message
            : "Failed to settle contact",
      );
    },
  });

  const owesUser = contact.balance_paise > 0;
  const amountToSettle = Math.abs(contact.balance_paise);

  return (
    <Dialog open={open} onOpenChange={(isOpen) => !isOpen && onClose()}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Settle Balance with {contact.name}</DialogTitle>
          <DialogDescription>
            Record a full settlement to bring the outstanding net balance to zero.
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-3.5 py-1">
          <div className="rounded-md border border-border bg-muted/30 p-3 text-center">
            <span className="text-[11px] text-muted-foreground uppercase tracking-wider block">
              Settlement Amount
            </span>
            <span className="font-mono text-2xl font-bold text-foreground">
              <Sensitive>{formatINR(amountToSettle)}</Sensitive>
            </span>
            <span className="text-[11.5px] text-muted-foreground block mt-1">
              {owesUser
                ? `${contact.name} pays you in full`
                : `You pay ${contact.name} in full`}
            </span>
          </div>

          <Field label="Settlement Date">
            <input
              type="date"
              value={date}
              onChange={(e) => setDate(e.target.value)}
              className="w-full rounded-md border border-border bg-background px-2.5 py-2 text-[12.5px] text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
          </Field>

          <Field label="Notes (optional)">
            <TextInput
              value={notes}
              onChange={setNotes}
              placeholder="e.g. Paid via UPI / GPay"
            />
          </Field>

          {errorMsg ? <p className="text-[12px] text-neg">{errorMsg}</p> : null}
        </div>

        <DialogFooter className="gap-2 sm:gap-0 pt-2">
          <Button
            type="button"
            variant="ghost"
            onClick={onClose}
            disabled={settleMutation.isPending}
            className="h-8 text-[12.5px]"
          >
            Cancel
          </Button>
          <Button
            type="button"
            onClick={() => settleMutation.mutate()}
            disabled={settleMutation.isPending || amountToSettle === 0}
            className="h-8 text-[12.5px]"
          >
            {settleMutation.isPending ? "Settling…" : "Confirm Settle Up"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function AddPeerEntryModal({
  contact,
  open,
  onClose,
}: {
  contact: PeerContactWithBalance;
  open: boolean;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();

  const [direction, setDirection] = useState<PeerDirection>("you_gave");
  const [amountRupees, setAmountRupees] = useState("");
  const [date, setDate] = useState(() => new Date().toISOString().slice(0, 10));
  const [notes, setNotes] = useState("");

  // Upfront discount / cashback offset
  const [hasDiscount, setHasDiscount] = useState(false);
  const [originalRupees, setOriginalRupees] = useState("");
  const [discountRupees, setDiscountRupees] = useState("0.00");

  const [errorMsg, setErrorMsg] = useState<string | null>(null);

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

  const mutation = useMutation({
    mutationFn: async () => {
      const amountPaise = rupeesToPaise(amountRupees);
      if (amountPaise <= 0) {
        throw new Error("Amount must be greater than zero");
      }
      if (!date) {
        throw new Error("Date is required");
      }

      let originalPaise: number | undefined;
      let discountPaise: number | undefined;
      if (hasDiscount) {
        originalPaise = rupeesToPaise(originalRupees);
        discountPaise = rupeesToPaise(discountRupees);
        if (originalPaise - discountPaise !== amountPaise) {
          throw new Error("Amount must equal original less discount");
        }
      }

      return createPeerEntry({
        contact_id: contact.id,
        transaction_id: null,
        date,
        amount_paise: amountPaise,
        direction,
        is_settlement: false,
        notes: notes.trim() || null,
        original_paise: originalPaise,
        discount_paise: discountPaise,
      });
    },
    onSuccess: () => {
      invalidateFriends(queryClient);
      onClose();
    },
    onError: (err) => {
      setErrorMsg(
        err instanceof ApiError
          ? err.detail
          : err instanceof Error
            ? err.message
            : "Failed to add entry",
      );
    },
  });

  return (
    <Dialog open={open} onOpenChange={(isOpen) => !isOpen && onClose()}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Add Entry for {contact.name}</DialogTitle>
          <DialogDescription>
            Record a debt or repayment directly in {contact.name}&apos;s ledger.
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-3.5 py-1">
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
                You Gave (They owe)
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
                You Got (You owe)
              </Button>
            </div>
          </div>

          <Field label="Date">
            <input
              type="date"
              value={date}
              onChange={(e) => setDate(e.target.value)}
              className="w-full rounded-md border border-border bg-background px-2.5 py-2 text-[12.5px] text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
          </Field>

          {/* Upfront discount / cashback toggle */}
          <div className="flex items-center justify-between pt-1">
            <label className="flex items-center gap-2 cursor-pointer text-[12px] text-foreground">
              <input
                type="checkbox"
                checked={hasDiscount}
                onChange={(e) => {
                  setHasDiscount(e.target.checked);
                  if (!e.target.checked) {
                    setAmountRupees(originalRupees);
                  }
                }}
                className="rounded border-border text-primary focus:ring-ring"
              />
              <span>Apply discount / cashback offset</span>
            </label>
          </div>

          {hasDiscount ? (
            <div className="grid grid-cols-2 gap-2 rounded-md border border-border bg-muted/20 p-2.5">
              <Field label="Original (₹)">
                <TextInput
                  type="number"
                  step="0.01"
                  min="0"
                  value={originalRupees}
                  onChange={handleOriginalChange}
                  placeholder="0.00"
                />
              </Field>
              <Field label="Discount (₹)">
                <TextInput
                  type="number"
                  step="0.01"
                  min="0"
                  value={discountRupees}
                  onChange={handleDiscountChange}
                  placeholder="0.00"
                />
              </Field>
            </div>
          ) : null}

          <Field label="Amount (₹)">
            <TextInput
              type="number"
              step="0.01"
              min="0"
              value={amountRupees}
              onChange={(val) => {
                setAmountRupees(val);
                if (hasDiscount) setHasDiscount(false);
              }}
              placeholder="0.00"
            />
          </Field>

          <Field label="Notes (optional)">
            <TextInput
              value={notes}
              onChange={setNotes}
              placeholder="e.g. Lunch split, Cash borrow"
            />
          </Field>

          {errorMsg ? <p className="text-[12px] text-neg">{errorMsg}</p> : null}
        </div>

        <DialogFooter className="gap-2 sm:gap-0 pt-2">
          <Button
            type="button"
            variant="ghost"
            onClick={onClose}
            disabled={mutation.isPending}
            className="h-8 text-[12.5px]"
          >
            Cancel
          </Button>
          <Button
            type="button"
            onClick={() => {
              setErrorMsg(null);
              mutation.mutate();
            }}
            disabled={mutation.isPending || !amountRupees || rupeesToPaise(amountRupees) <= 0}
            className="h-8 text-[12.5px]"
          >
            {mutation.isPending ? "Adding…" : "Add Entry"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
