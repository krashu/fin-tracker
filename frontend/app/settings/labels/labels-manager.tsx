"use client";

/**
 * Tag (label) list + CRUD (PRD §F3a). Reads/writes the shared ["labels"] query
 * that the label input (board dialog, add dialog, review queue) and the board
 * filter all consume, so a create/rename/delete propagates everywhere on
 * invalidate (PRD §F9).
 *
 * Unlike categories (soft-archive), labels HARD-delete: the DB cascades the
 * delete to `transaction_labels`, so removing a tag strips it from every
 * transaction that carried it — the confirm dialog spells that out. A rename or
 * delete also refreshes ["transactions"] so the board's chips update.
 */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { IconPlus, IconX } from "@/components/icons";
import { Field, TextInput } from "@/components/form/fields";
import { LabelChip } from "@/components/labels/label-chip";
import {
  ApiError,
  createLabel,
  deleteLabel,
  listLabels,
  patchLabel,
  type LabelRead,
} from "@/lib/api/client";
import {
  LABEL_INPUT_MAX_CHARS,
  labelDisplay,
  normalizeLabelName,
} from "@/lib/labels";
import { invalidateRules } from "@/lib/queries/invalidate";
import { cn } from "@/lib/utils";

/** The one open dialog (or none). `edit`/`delete` always carry their row. */
type LabelDialog =
  | null
  | { kind: "create" }
  | { kind: "edit"; label: LabelRead }
  | { kind: "delete"; label: LabelRead };

export function LabelsManager() {
  const labelsQuery = useQuery({ queryKey: ["labels"], queryFn: listLabels });
  const labels = labelsQuery.data ?? [];

  // One dialog at a time; a single discriminated state makes "edit/delete
  // without a row" unrepresentable.
  const [dialog, setDialog] = useState<LabelDialog>(null);
  const closeDialog = () => setDialog(null);

  return (
    <Card className="max-w-3xl">
      <CardHeader className="flex flex-row items-center justify-between border-b">
        <CardTitle className="text-[14px]">
          {labels.length} {labels.length === 1 ? "tag" : "tags"}
        </CardTitle>
        <Button
          type="button"
          onClick={() => setDialog({ kind: "create" })}
          className="h-8 gap-1.5 px-2.5 text-[12px] font-medium"
        >
          <IconPlus className="size-3" />
          New tag
        </Button>
      </CardHeader>

      <CardContent className="px-0">
        {labelsQuery.isPending ? (
          <Row tone="muted">Loading…</Row>
        ) : labelsQuery.isError ? (
          <Row tone="error">Couldn’t load tags — is the API running?</Row>
        ) : labels.length === 0 ? (
          <Row tone="muted">
            No tags yet — add one with New tag, or tag a transaction from its
            row.
          </Row>
        ) : (
          labels.map((label) => (
            <div
              key={label.id}
              className="flex items-center gap-3 border-b border-border/60 px-4 py-2.5 last:border-b-0"
            >
              <LabelChip name={label.name} />
              <span className="min-w-0 flex-1" />
              <Button
                type="button"
                variant="ghost"
                onClick={() => setDialog({ kind: "edit", label })}
                aria-label={`Rename ${labelDisplay(label.name)}`}
                className="h-7 px-2.5 text-[12px]"
              >
                Rename
              </Button>
              <Button
                type="button"
                variant="ghost"
                onClick={() => setDialog({ kind: "delete", label })}
                className="h-7 gap-1 px-2 text-[12px] text-muted-foreground hover:text-neg"
                aria-label={`Delete ${labelDisplay(label.name)}`}
                title="Delete"
              >
                <IconX className="size-3.5" />
              </Button>
            </div>
          ))
        )}
      </CardContent>

      {dialog?.kind === "create" ? (
        <LabelFormDialog mode="create" onClose={closeDialog} />
      ) : dialog?.kind === "edit" ? (
        <LabelFormDialog
          key={dialog.label.id}
          mode="edit"
          label={dialog.label}
          onClose={closeDialog}
        />
      ) : dialog?.kind === "delete" ? (
        <DeleteConfirm label={dialog.label} onClose={closeDialog} />
      ) : null}
    </Card>
  );
}

function Row({
  children,
  tone,
}: {
  children: React.ReactNode;
  tone: "muted" | "error";
}) {
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

type LabelFormDialogProps =
  | { mode: "create"; onClose: () => void }
  | { mode: "edit"; label: LabelRead; onClose: () => void };

function LabelFormDialog(props: LabelFormDialogProps) {
  const { onClose } = props;
  const label = props.mode === "edit" ? props.label : undefined;
  const queryClient = useQueryClient();
  const [name, setName] = useState(label?.name ?? "");

  const mutation = useMutation({
    mutationFn: () => {
      const trimmed = name.trim();
      if (props.mode === "create") {
        return createLabel({ name: trimmed });
      }
      return patchLabel(props.label.id, { name: trimmed });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["labels"] });
      // A rename changes the chip text shown on the board AND the joined
      // label_name in /settings/rules + any open import review queue.
      // invalidateRules covers rules + candidates (+ tagging-stats), else a
      // stale cached name is re-sent on the next edit and get-or-created as a
      // phantom duplicate, and the rules list shows the old name.
      if (props.mode === "edit") {
        queryClient.invalidateQueries({ queryKey: ["transactions"] });
        // /spending's spend-by-tag heatmap + coverage tile are keyed
        // ["dashboards", "spend-by-tag*"], which invalidateRules does NOT reach
        // (it nudges only the narrower ["dashboards","tagging-stats"]), so a
        // rename left the old name rendering on the heatmap.
        queryClient.invalidateQueries({ queryKey: ["dashboards"] });
        invalidateRules(queryClient);
      }
      onClose();
    },
  });

  // Gate on the NORMALIZED value (mirrors the backend), so `#Travel`, `travel`,
  // and `  travel ` all read as the same label — a rename to an equivalent name
  // is a no-op the button disables rather than firing a pointless PATCH.
  const normalized = normalizeLabelName(name);
  const canSubmit =
    normalized.length > 0 &&
    (props.mode === "create" || normalized !== props.label.name) &&
    !mutation.isPending;

  return (
    <Dialog open onOpenChange={(open) => (open ? null : onClose())}>
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <DialogTitle>
            {props.mode === "create" ? "New tag" : "Rename tag"}
          </DialogTitle>
          <DialogDescription className="sr-only">
            {props.mode === "create"
              ? "Add a tag for labeling transactions."
              : "Rename this tag everywhere it's used."}
          </DialogDescription>
        </DialogHeader>

        <Field label="Name">
          <TextInput
            value={name}
            onChange={setName}
            placeholder="e.g. online"
            autoFocus
            maxLength={LABEL_INPUT_MAX_CHARS}
          />
        </Field>
        <p className="mt-1.5 text-[11px] text-muted-foreground">
          Lowercased automatically; the <span className="font-medium">#</span>{" "}
          is added for display. Stored as{" "}
          <span className="font-medium text-foreground/80">
            {normalized ? labelDisplay(normalized) : "#…"}
          </span>
          .
        </p>

        {mutation.isError ? (
          <p className="mt-2 text-[12px] text-neg">
            {mutation.error instanceof ApiError
              ? mutation.error.detail
              : "Couldn’t save — try again."}
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
            disabled={!canSubmit}
          >
            {mutation.isPending
              ? "Saving…"
              : props.mode === "create"
                ? "Create"
                : "Save"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function DeleteConfirm({
  label,
  onClose,
}: {
  label: LabelRead;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: () => deleteLabel(label.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["labels"] });
      // Delete cascades to the join rows AND to merchant_label_map, so the tag
      // vanishes from every transaction and from /settings/rules. Refresh the
      // board chips (transactions) + invalidateRules (rules + candidates), else
      // a stale cached name is re-sent on the next review-queue edit and
      // get-or-created, resurrecting the just-deleted tag, and a dead rule row
      // stays listed (Forget/Pin then 404s).
      queryClient.invalidateQueries({ queryKey: ["transactions"] });
      // Same gap as the rename above: /spending's heatmap and coverage tile sit
      // under ["dashboards", "spend-by-tag*"], outside invalidateRules' reach,
      // so a delete left the tag's row rendering with its old spend and the
      // coverage tile reporting the old tagged_paise.
      queryClient.invalidateQueries({ queryKey: ["dashboards"] });
      invalidateRules(queryClient);
      onClose();
    },
  });

  return (
    <Dialog open onOpenChange={(open) => (open ? null : onClose())}>
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <DialogTitle>Delete {labelDisplay(label.name)}?</DialogTitle>
          <DialogDescription>
            This removes the tag from every transaction that has it. It can’t be
            undone.
          </DialogDescription>
        </DialogHeader>
        {mutation.isError ? (
          <p className="text-[12px] text-neg">
            {mutation.error instanceof ApiError
              ? mutation.error.detail
              : "Couldn’t delete — try again."}
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
