"use client";

/**
 * Merchant-alias table (ADR-0011 merchant-alias layer, Phase A4) — the
 * `/settings/rules` section that lets a user fold different raw merchant
 * descriptors of the same merchant (order ids, RRNs, UPI handles) onto one
 * canonical name, so F3/F3a's per-merchant memory aggregates across them
 * instead of starting cold on every new descriptor.
 *
 * Reads the ["rules", "aliases"] query (GET /rules/aliases). Every write
 * (create / rename / delete) calls invalidateRules — its ["rules"] prefix
 * already covers this query key (see lib/queries/invalidate.ts's docstring),
 * so no other cache needs a separate poke.
 *
 * `pattern` is immutable once created — it is the row's identity (part of its
 * unique key). Renaming only ever changes `canonical`; to change a pattern,
 * delete the alias and add a new one. Deliberately minimal: plain text
 * inputs, no merchant-autocomplete combobox (that's `NewRuleDialog`'s
 * pattern, for a different field with different semantics — extracting a
 * shared combobox here would be reaching for the second use ahead of an
 * actual second need).
 */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardAction,
  CardContent,
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
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Field, TextInput } from "@/components/form/fields";
import { IconCheck, IconChevronDown, IconPlus } from "@/components/icons";
import {
  ApiError,
  createAlias,
  deleteAlias,
  listAliases,
  patchAliasCanonical,
  type MerchantAliasRead,
} from "@/lib/api/client";
import { invalidateRules } from "@/lib/queries/invalidate";
import { cn } from "@/lib/utils";

type AliasDialog =
  | null
  | { kind: "rename"; alias: MerchantAliasRead }
  | { kind: "delete"; alias: MerchantAliasRead };

export function AliasManager() {
  const aliasesQuery = useQuery({ queryKey: ["rules", "aliases"], queryFn: listAliases });
  const aliases = aliasesQuery.data ?? [];

  const [dialog, setDialog] = useState<AliasDialog>(null);
  const [newOpen, setNewOpen] = useState(false);

  return (
    <Card className="mt-6 max-w-3xl">
      <CardHeader className="items-center border-b">
        <CardTitle className="text-[14px]">
          {aliases.length} {aliases.length === 1 ? "alias" : "aliases"}
        </CardTitle>
        <CardAction className="self-center">
          <Button className="h-8 gap-1 px-2.5 text-[12.5px]" onClick={() => setNewOpen(true)}>
            <IconPlus className="size-3.5" />
            New alias
          </Button>
        </CardAction>
      </CardHeader>

      <p className="border-b border-border/60 px-4 py-2 text-[11px] text-muted-foreground">
        Fold different raw descriptors of the same merchant onto one name, so rules and tags
        learn once instead of once per descriptor.
      </p>

      <CardContent className="px-0">
        {aliasesQuery.isPending ? (
          <Row tone="muted">Loading…</Row>
        ) : aliasesQuery.isError ? (
          <Row tone="error">Couldn’t load aliases — is the API running?</Row>
        ) : aliases.length === 0 ? (
          <Row tone="muted">
            No aliases yet — add one to fold a recurring merchant's descriptors together.
          </Row>
        ) : (
          aliases.map((alias) => (
            <div
              key={alias.id}
              className="flex items-center gap-2 border-b border-border/60 px-4 py-3 last:border-b-0"
            >
              <p className="min-w-0 flex-1 truncate font-mono text-[12px]">
                <span className="text-muted-foreground">{alias.pattern}</span>
                <span className="mx-1.5 text-muted-foreground">→</span>
                <span className="font-medium text-foreground">{alias.canonical}</span>
              </p>
              {alias.is_seeded ? <DictionaryBadge /> : null}
              <RowMenu label={`Actions for ${alias.pattern}`}>
                <DropdownMenuItem onSelect={() => setDialog({ kind: "rename", alias })}>
                  Rename canonical
                </DropdownMenuItem>
                <DropdownMenuSeparator />
                <DropdownMenuItem
                  variant="destructive"
                  onSelect={() => setDialog({ kind: "delete", alias })}
                >
                  Delete alias
                </DropdownMenuItem>
              </RowMenu>
            </div>
          ))
        )}
      </CardContent>

      <NewAliasDialog open={newOpen} onClose={() => setNewOpen(false)} />
      {dialog?.kind === "rename" ? (
        <RenameAliasDialog alias={dialog.alias} onClose={() => setDialog(null)} />
      ) : null}
      {dialog?.kind === "delete" ? (
        <DeleteAliasDialog alias={dialog.alias} onClose={() => setDialog(null)} />
      ) : null}
    </Card>
  );
}

/** ADR-0011 Phase A5's seed dictionary marker — mirrors rules-manager.tsx's
 * SeededBadge visual language (dashed border, muted text) for an unconfirmed
 * dictionary entry, applied here to the alias row instead of a category row. */
function DictionaryBadge() {
  return (
    <span
      className="shrink-0 rounded-sm border border-dashed border-ring/30 px-1 py-px text-[10px] font-medium uppercase tracking-wide text-muted-foreground"
      title="From the merchant dictionary"
    >
      dictionary
    </span>
  );
}

function RowMenu({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          className="h-7 w-7 shrink-0 px-0 text-muted-foreground hover:text-foreground"
          aria-label={label}
        >
          <IconChevronDown className="size-3.5" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="min-w-40">
        {children}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function Row({ children, tone }: { children: React.ReactNode; tone: "muted" | "error" }) {
  return (
    <p
      className={cn(
        "px-4 py-8 text-center text-[13px]",
        tone === "error" ? "text-neg" : "text-muted-foreground",
      )}
    >
      {children}
    </p>
  );
}

function NewAliasDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const [pattern, setPattern] = useState("");
  const [canonical, setCanonical] = useState("");
  const [justAdded, setJustAdded] = useState<string | null>(null);

  const canSubmit = pattern.trim().length > 0 && canonical.trim().length > 0;

  function reset() {
    setPattern("");
    setCanonical("");
  }

  const mutation = useMutation({
    mutationFn: () => createAlias({ pattern: pattern.trim(), canonical: canonical.trim() }),
    onSuccess: (result) => {
      setJustAdded(result.pattern);
      reset();
    },
    onSettled: () => invalidateRules(qc),
  });

  function handleOpenChange(next: boolean) {
    if (next) return;
    reset();
    setJustAdded(null);
    mutation.reset();
    onClose();
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <DialogTitle>New alias</DialogTitle>
          <DialogDescription>
            Any merchant containing these words folds onto the canonical name below.
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-3">
          <Field label="Pattern">
            <TextInput value={pattern} onChange={setPattern} placeholder="e.g. swiggy blr" maxLength={512} />
          </Field>
          <Field label="Canonical">
            <TextInput value={canonical} onChange={setCanonical} placeholder="e.g. Swiggy" maxLength={512} />
          </Field>
        </div>

        {mutation.isError ? (
          <p className="text-[12px] text-neg">
            {mutation.error instanceof ApiError ? mutation.error.detail : "Couldn’t save — try again."}
          </p>
        ) : justAdded ? (
          <p className="inline-flex items-center gap-1.5 text-[12px] text-pos">
            <IconCheck className="size-3.5" />
            Added alias for “{justAdded}”. Add another, or Done.
          </p>
        ) : null}

        <DialogFooter>
          <Button
            variant="ghost"
            className="h-8 px-3 text-[12.5px]"
            onClick={() => handleOpenChange(false)}
            disabled={mutation.isPending}
          >
            Done
          </Button>
          <Button
            className="h-8 px-3 text-[12.5px]"
            onClick={() => mutation.mutate()}
            disabled={!canSubmit || mutation.isPending}
          >
            {mutation.isPending ? "Saving…" : "Add alias"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function RenameAliasDialog({
  alias,
  onClose,
}: {
  alias: MerchantAliasRead;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [canonical, setCanonical] = useState(alias.canonical);

  const mutation = useMutation({
    mutationFn: () => patchAliasCanonical(alias.id, canonical.trim()),
    onSuccess: () => {
      invalidateRules(qc);
      onClose();
    },
  });

  return (
    <Dialog open onOpenChange={(open) => (open ? null : onClose())}>
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <DialogTitle>Rename “{alias.pattern}”’s canonical</DialogTitle>
          <DialogDescription>
            The pattern itself can’t be edited here — delete and re-add the alias to change it.
          </DialogDescription>
        </DialogHeader>

        <Field label="Canonical">
          <TextInput value={canonical} onChange={setCanonical} maxLength={512} />
        </Field>

        {mutation.isError ? (
          <p className="text-[12px] text-neg">
            {mutation.error instanceof ApiError ? mutation.error.detail : "Couldn’t save — try again."}
          </p>
        ) : null}

        <DialogFooter>
          <Button
            variant="ghost"
            className="h-8 px-3 text-[12.5px]"
            onClick={onClose}
            disabled={mutation.isPending}
          >
            Cancel
          </Button>
          <Button
            className="h-8 px-3 text-[12.5px]"
            onClick={() => mutation.mutate()}
            disabled={canonical.trim().length === 0 || mutation.isPending}
          >
            {mutation.isPending ? "Saving…" : "Save"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function DeleteAliasDialog({
  alias,
  onClose,
}: {
  alias: MerchantAliasRead;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const mutation = useMutation({
    mutationFn: () => deleteAlias(alias.id),
    onSuccess: () => {
      invalidateRules(qc);
      onClose();
    },
  });

  return (
    <Dialog open onOpenChange={(open) => (open ? null : onClose())}>
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <DialogTitle>
            Delete alias “{alias.pattern}” → “{alias.canonical}”?
          </DialogTitle>
          <DialogDescription>
            Transactions already tagged keep their category and tags. Merchants matching this
            pattern stop folding onto “{alias.canonical}” going forward.
          </DialogDescription>
        </DialogHeader>
        {mutation.isError ? (
          <p className="text-[12px] text-neg">
            {mutation.error instanceof ApiError ? mutation.error.detail : "Couldn’t delete — try again."}
          </p>
        ) : null}
        <DialogFooter>
          <Button
            variant="ghost"
            className="h-8 px-3 text-[12.5px]"
            onClick={onClose}
            disabled={mutation.isPending}
          >
            Cancel
          </Button>
          <Button
            variant="destructive"
            className="h-8 px-3 text-[12.5px]"
            onClick={() => mutation.mutate()}
            disabled={mutation.isPending}
          >
            {mutation.isPending ? "Deleting…" : "Delete"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
