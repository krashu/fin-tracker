"use client";

/**
 * Category list + CRUD (PRD §F5). Reads/writes the shared ["categories"] query
 * the expenses board, transaction dialog, and import review all consume, so a
 * create/rename/archive propagates everywhere on invalidate (PRD §F9).
 *
 * Seeded defaults are renamed/archived like any other (the backend permits it);
 * the "default" tag is display-only. Archiving a category also clears its
 * merchant→category auto-tag mappings server-side.
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
import { IconArchive, IconPlus } from "@/components/icons";
import { Field, TextInput } from "@/components/form/fields";
import {
  ApiError,
  createCategory,
  deleteCategory,
  listCategories,
  patchCategory,
  type CategoryColor,
  type CategoryKind,
  type CategoryRead,
  type CategoryUpdate,
} from "@/lib/api/client";
import {
  CATEGORY_KIND_LABELS,
  CATEGORY_PALETTE,
  nextCategoryColor,
} from "@/lib/categories";
import { invalidateRules } from "@/lib/queries/invalidate";
import { cn } from "@/lib/utils";

const KINDS: readonly CategoryKind[] = ["spend", "income"];

/** The one open dialog (or none). `edit`/`archive` always carry their row. */
type CategoryDialog =
  | null
  | { kind: "create" }
  | { kind: "edit"; category: CategoryRead }
  | { kind: "archive"; category: CategoryRead };

export function CategoriesManager() {
  const categoriesQuery = useQuery({
    queryKey: ["categories"],
    queryFn: listCategories,
  });
  const categories = categoriesQuery.data ?? [];

  // One dialog at a time; a single discriminated state makes "edit/archive
  // without a row" unrepresentable.
  const [dialog, setDialog] = useState<CategoryDialog>(null);
  const closeDialog = () => setDialog(null);

  return (
    <Card className="max-w-3xl">
      <CardHeader className="flex flex-row items-center justify-between border-b">
        <CardTitle className="text-[14px]">
          {categories.length}{" "}
          {categories.length === 1 ? "category" : "categories"}
        </CardTitle>
        <Button
          type="button"
          onClick={() => setDialog({ kind: "create" })}
          className="h-8 gap-1.5 px-2.5 text-[12px] font-medium"
        >
          <IconPlus className="size-3" />
          New category
        </Button>
      </CardHeader>

      <CardContent className="px-0">
        {categoriesQuery.isPending ? (
          <Row tone="muted">Loading…</Row>
        ) : categoriesQuery.isError ? (
          <Row tone="error">Couldn’t load categories — is the API running?</Row>
        ) : categories.length === 0 ? (
          <Row tone="muted">No categories yet — add one with New category.</Row>
        ) : (
          // Grouped by kind so the two distinct scopes are obvious at a glance.
          KINDS.map((kind) => (
            <CategorySection
              key={kind}
              kind={kind}
              categories={categories.filter((c) => c.kind === kind)}
              onEdit={(c) => setDialog({ kind: "edit", category: c })}
              onArchive={(c) => setDialog({ kind: "archive", category: c })}
            />
          ))
        )}
      </CardContent>

      {dialog?.kind === "create" ? (
        <CategoryFormDialog
          mode="create"
          existingColors={categories.map((c) => c.color)}
          onClose={closeDialog}
        />
      ) : dialog?.kind === "edit" ? (
        <CategoryFormDialog
          key={dialog.category.id}
          mode="edit"
          category={dialog.category}
          onClose={closeDialog}
        />
      ) : dialog?.kind === "archive" ? (
        <ArchiveConfirm category={dialog.category} onClose={closeDialog} />
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

function CategorySection({
  kind,
  categories,
  onEdit,
  onArchive,
}: {
  kind: CategoryKind;
  categories: CategoryRead[];
  onEdit: (c: CategoryRead) => void;
  onArchive: (c: CategoryRead) => void;
}) {
  if (categories.length === 0) return null;
  return (
    <>
      <p className="border-b border-border/60 bg-muted/30 px-4 py-1.5 text-xs font-medium uppercase tracking-wide text-foreground/70">
        {CATEGORY_KIND_LABELS[kind]}
      </p>
      {categories.map((c) => (
        <div
          key={c.id}
          className="flex items-center gap-3 border-b border-border/60 px-4 py-2.5 last:border-b-0"
        >
          {/* COLOR leads the name — the same position the dot occupies on the
              board and the spend-by-category bar. Filled swatch when the user
              picked a color; a dashed outline when none is set (color is the
              user's choice, nothing auto-assigned). A fixed size keeps the swatch
              in a straight, aligned column down the list. */}
          {c.color ? (
            <span
              className="size-4 shrink-0 rounded-[5px] border border-border"
              style={{ backgroundColor: c.color }}
            />
          ) : (
            <span
              className="size-4 shrink-0 rounded-[5px] border border-dashed border-border"
              title="No color"
            />
          )}
          <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-foreground">
            {c.name}
          </span>
          <Button
            type="button"
            variant="ghost"
            onClick={() => onEdit(c)}
            aria-label={`Edit ${c.name}`}
            className="h-7 px-2.5 text-[12px]"
          >
            Edit
          </Button>
          <Button
            type="button"
            variant="ghost"
            onClick={() => onArchive(c)}
            className="h-7 gap-1 px-2 text-[12px] text-muted-foreground hover:text-neg"
            aria-label={`Archive ${c.name}`}
            title="Archive"
          >
            <IconArchive className="size-3.5" />
          </Button>
        </div>
      ))}
    </>
  );
}

type CategoryFormDialogProps =
  | {
      mode: "create";
      existingColors: (CategoryColor | null)[];
      onClose: () => void;
    }
  | { mode: "edit"; category: CategoryRead; onClose: () => void };

function CategoryFormDialog(props: CategoryFormDialogProps) {
  const { onClose } = props;
  // Present only in edit mode. Narrowed `props.category` is used wherever a
  // definite value is required (no non-null assertions).
  const category = props.mode === "edit" ? props.category : undefined;
  const queryClient = useQueryClient();
  const [name, setName] = useState(category?.name ?? "");
  // Kind is immutable after create (the backend has no PATCH kind), so it's a
  // picker on create and a read-only label on edit.
  const [kind, setKind] = useState<CategoryKind>(category?.kind ?? "spend");
  // A new category auto-picks the first unused palette color so it reads
  // distinctly without the user choosing; edit keeps the stored color.
  // null = no color (a neutral dot).
  const [color, setColor] = useState<CategoryColor | null>(() =>
    props.mode === "edit"
      ? (props.category.color ?? null)
      : nextCategoryColor(props.existingColors),
  );

  const mutation = useMutation({
    mutationFn: () => {
      const trimmedName = name.trim();
      if (props.mode === "create") {
        return createCategory({
          name: trimmedName,
          kind,
          ...(color != null ? { color } : {}),
        });
      }
      // Send ONLY changed fields: the PATCH route short-circuits when `name` is
      // present and unchanged, which would otherwise silently drop a color-only
      // edit. An explicit `color: null` reverts to Auto.
      const body: CategoryUpdate = {};
      if (trimmedName !== props.category.name) body.name = trimmedName;
      if (color !== (props.category.color ?? null)) body.color = color;
      return patchCategory(props.category.id, body);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["categories"] });
      // Category names/scopes feed the spend-by-category breakdown and tagging
      // health on /dashboard (PRD §F9); refresh those on a rename/create too.
      queryClient.invalidateQueries({ queryKey: ["dashboards"] });
      // A rename changes the server-joined category_name in /settings/rules and
      // in any open import review queue — the identical reasoning the labels
      // manager records for label_name. lib/queries/invalidate.ts names this
      // manager as a required caller; ArchiveConfirm below already calls it.
      invalidateRules(queryClient);
      onClose();
    },
  });

  const trimmed = name.trim();
  const colorChanged =
    props.mode === "edit" && color !== (props.category.color ?? null);
  const changed =
    props.mode === "create"
      ? trimmed.length > 0
      : trimmed !== props.category.name || colorChanged;
  const canSubmit = trimmed.length > 0 && changed && !mutation.isPending;

  return (
    <Dialog open onOpenChange={(open) => (open ? null : onClose())}>
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <DialogTitle>
            {props.mode === "create" ? "New category" : "Edit category"}
          </DialogTitle>
          <DialogDescription className="sr-only">
            {props.mode === "create"
              ? "Add a category for tagging transactions."
              : "Edit this category's name and color."}
          </DialogDescription>
        </DialogHeader>

        <Field label="Name">
          <TextInput
            value={name}
            onChange={setName}
            placeholder="e.g. Dining out"
            autoFocus
            maxLength={64}
          />
        </Field>

        <div className="mt-3">
          <Field label="Type">
            {props.mode === "create" ? (
              <div className="grid grid-cols-2 gap-1 rounded-md border border-border bg-background p-0.5">
                {KINDS.map((k) => (
                  <button
                    key={k}
                    type="button"
                    onClick={() => setKind(k)}
                    className={cn(
                      "rounded-[5px] py-1.5 text-[12px] font-medium transition-colors",
                      kind === k
                        ? "bg-primary text-primary-foreground"
                        : "text-muted-foreground hover:text-foreground",
                    )}
                  >
                    {CATEGORY_KIND_LABELS[k]}
                  </button>
                ))}
              </div>
            ) : (
              <span className="text-[12.5px] text-muted-foreground">
                {CATEGORY_KIND_LABELS[kind]}
                <span className="ml-1.5 text-[11px]">· can’t be changed</span>
              </span>
            )}
          </Field>
        </div>

        {/* A curated swatch palette (no freeform picker — overkill for tagging
            and risks clashing/low-contrast colors). "None" clears to a neutral
            dot. New categories auto-pick the first unused hue. */}
        <div className="mt-3">
          <Field label="Color">
            <div className="flex flex-wrap gap-1.5">
              <button
                type="button"
                onClick={() => setColor(null)}
                aria-label="No color"
                aria-pressed={color === null}
                title="No color"
                className={cn(
                  "size-6 rounded-md border border-dashed border-border transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-card",
                  color === null &&
                    "ring-2 ring-ring/30 ring-offset-2 ring-offset-card dark:ring-ring/40",
                )}
              />
              {CATEGORY_PALETTE.map((c) => (
                <button
                  key={c}
                  type="button"
                  onClick={() => setColor(c)}
                  aria-label={`Color ${c}`}
                  aria-pressed={color === c}
                  className={cn(
                    "size-6 rounded-md border border-border transition-transform focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-card",
                    color === c &&
                      "ring-2 ring-ring/30 ring-offset-2 ring-offset-card dark:ring-ring/40",
                  )}
                  style={{ backgroundColor: c }}
                />
              ))}
            </div>
          </Field>
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

function ArchiveConfirm({
  category,
  onClose,
}: {
  category: CategoryRead;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: () => deleteCategory(category.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["categories"] });
      // Archiving hides the category's auto-tag rules (they're kept, not
      // deleted) and shifts the spend-by-category / tagging-health aggregates on
      // /dashboard (PRD §F9). invalidateRules refreshes /settings/rules (the
      // archived category's rules drop out of the list) + any open review
      // queue's confidence.
      queryClient.invalidateQueries({ queryKey: ["dashboards"] });
      invalidateRules(queryClient);
      onClose();
    },
  });

  return (
    <Dialog open onOpenChange={(open) => (open ? null : onClose())}>
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <DialogTitle>Archive {category.name}?</DialogTitle>
          <DialogDescription>
            It’s removed from the category list and its merchant auto-tag rules
            stop applying while it’s archived. Existing transactions keep this
            category.
          </DialogDescription>
        </DialogHeader>
        {mutation.isError ? (
          <p className="text-[12px] text-neg">
            {mutation.error instanceof ApiError
              ? mutation.error.detail
              : "Couldn’t archive — try again."}
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
            {mutation.isPending ? "Archiving…" : "Archive"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
