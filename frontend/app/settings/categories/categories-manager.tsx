"use client";

/**
 * Category list + CRUD (PRD §F5). Reads/writes the shared ["categories"] query
 * the expenses board, transaction dialog, and import review all consume, so a
 * create/rename/archive propagates everywhere on invalidate (PRD §F9).
 *
 * Seeded defaults are renamed/archived like any other (the backend permits it);
 * the "default" tag is display-only. Archiving a parent category also cascades
 * to archive its child subcategories server-side.
 */
import { useMemo, useState } from "react";
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
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { IconArchive, IconChevronDown, IconPlus } from "@/components/icons";
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
  buildCategoryTree,
  getReparentingOptions,
  nextCategoryColor,
  resolveSiblingDisplayColor,
  type CategoryTreeNode,
} from "@/lib/categories";
import { invalidateRules } from "@/lib/queries/invalidate";
import { cn } from "@/lib/utils";

const KINDS: readonly CategoryKind[] = ["spend", "income"];

/** The one open dialog (or none). */
type CategoryDialog =
  | null
  | {
      kind: "create";
      defaultParentId?: number | null;
      defaultKind?: CategoryKind;
    }
  | { kind: "edit"; category: CategoryRead }
  | {
      kind: "archive";
      category: CategoryRead;
      subcategories: CategoryRead[];
    };

export function CategoriesManager() {
  const categoriesQuery = useQuery({
    queryKey: ["categories"],
    queryFn: () => listCategories(),
  });
  const categories = categoriesQuery.data ?? [];

  const [dialog, setDialog] = useState<CategoryDialog>(null);
  const closeDialog = () => setDialog(null);

  const rootCategoriesCount = useMemo(
    () => categories.filter((c) => c.parent_id === null).length,
    [categories],
  );

  return (
    <Card className="max-w-3xl">
      <CardHeader className="flex flex-row items-center justify-between border-b">
        <div>
          <CardTitle className="text-[14px]">
            {categories.length}{" "}
            {categories.length === 1 ? "category" : "categories"}
          </CardTitle>
          <p className="mt-0.5 text-[12px] text-muted-foreground">
            {rootCategoriesCount} parent{" "}
            {rootCategoriesCount === 1 ? "category" : "categories"} ·{" "}
            {categories.length - rootCategoriesCount} subcategories
          </p>
        </div>
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
              allCategories={categories}
              onAddSubcategory={(parent) =>
                setDialog({
                  kind: "create",
                  defaultParentId: parent.id,
                  defaultKind: parent.kind,
                })
              }
              onEdit={(c) => setDialog({ kind: "edit", category: c })}
              onArchive={(c, subcategories) =>
                setDialog({ kind: "archive", category: c, subcategories })
              }
            />
          ))
        )}
      </CardContent>

      {dialog?.kind === "create" ? (
        <CategoryFormDialog
          mode="create"
          allCategories={categories}
          defaultParentId={dialog.defaultParentId ?? null}
          defaultKind={dialog.defaultKind ?? "spend"}
          existingColors={categories.map((c) => c.color)}
          onClose={closeDialog}
        />
      ) : dialog?.kind === "edit" ? (
        <CategoryFormDialog
          key={dialog.category.id}
          mode="edit"
          category={dialog.category}
          allCategories={categories}
          existingColors={categories.map((c) => c.color)}
          onClose={closeDialog}
        />
      ) : dialog?.kind === "archive" ? (
        <ArchiveConfirm
          category={dialog.category}
          subcategories={dialog.subcategories}
          onClose={closeDialog}
        />
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
  allCategories,
  onAddSubcategory,
  onEdit,
  onArchive,
}: {
  kind: CategoryKind;
  allCategories: CategoryRead[];
  onAddSubcategory: (parent: CategoryRead) => void;
  onEdit: (c: CategoryRead) => void;
  onArchive: (c: CategoryRead, subcategories: CategoryRead[]) => void;
}) {
  const kindCategories = useMemo(
    () => allCategories.filter((c) => c.kind === kind),
    [allCategories, kind],
  );

  const tree = useMemo(
    () => buildCategoryTree(kindCategories),
    [kindCategories],
  );

  if (tree.length === 0) return null;

  return (
    <>
      <p className="border-b border-border/60 bg-muted/30 px-4 py-1.5 text-xs font-medium uppercase tracking-wide text-foreground/70">
        {CATEGORY_KIND_LABELS[kind]}
      </p>
      {tree.map((parent) => (
        <ParentCategoryCard
          key={parent.id}
          parent={parent}
          allCategories={allCategories}
          onAddSubcategory={() => onAddSubcategory(parent)}
          onEditParent={() => onEdit(parent)}
          onArchiveParent={() => onArchive(parent, parent.subcategories)}
          onEditSubcategory={(sub) => onEdit(sub)}
          onArchiveSubcategory={(sub) => onArchive(sub, [])}
        />
      ))}
    </>
  );
}

function ParentCategoryCard({
  parent,
  allCategories,
  onAddSubcategory,
  onEditParent,
  onArchiveParent,
  onEditSubcategory,
  onArchiveSubcategory,
}: {
  parent: CategoryTreeNode;
  allCategories: CategoryRead[];
  onAddSubcategory: () => void;
  onEditParent: () => void;
  onArchiveParent: () => void;
  onEditSubcategory: (sub: CategoryRead) => void;
  onArchiveSubcategory: (sub: CategoryRead) => void;
}) {
  return (
    <div className="border-b border-border/60 last:border-b-0">
      {/* Root Category Row */}
      <div className="flex items-center gap-3 bg-card px-4 py-2.5 transition-colors hover:bg-muted/30">
        {parent.color ? (
          <span
            className="size-4 shrink-0 rounded-[5px] border border-border"
            style={{ backgroundColor: parent.color }}
          />
        ) : (
          <span
            className="size-4 shrink-0 rounded-[5px] border border-dashed border-border"
            title="No color (neutral dot)"
          />
        )}
        <div className="flex min-w-0 flex-1 items-center gap-2">
          <span className="truncate text-[13px] font-semibold text-foreground">
            {parent.name}
          </span>
          {parent.is_seeded ? (
            <span
              className="shrink-0 rounded bg-muted px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground"
              title="Seeded default category"
            >
              default
            </span>
          ) : null}
          {parent.subcategories.length > 0 ? (
            <span className="shrink-0 text-[11px] text-muted-foreground/70">
              ({parent.subcategories.length}{" "}
              {parent.subcategories.length === 1
                ? "subcategory"
                : "subcategories"}
              )
            </span>
          ) : null}
        </div>

        <div className="flex items-center gap-1">
          <Button
            type="button"
            variant="ghost"
            onClick={onAddSubcategory}
            aria-label={`Add subcategory to ${parent.name}`}
            title="Add subcategory"
            className="h-7 gap-1 px-2 text-[12px] text-muted-foreground hover:text-foreground"
          >
            <IconPlus className="size-3" />
            <span className="hidden sm:inline">Subcategory</span>
          </Button>
          <Button
            type="button"
            variant="ghost"
            onClick={onEditParent}
            aria-label={`Edit ${parent.name}`}
            className="h-7 px-2.5 text-[12px]"
          >
            Edit
          </Button>
          <Button
            type="button"
            variant="ghost"
            onClick={onArchiveParent}
            className="h-7 px-2 text-[12px] text-muted-foreground hover:text-neg"
            aria-label={`Archive ${parent.name}`}
            title="Archive"
          >
            <IconArchive className="size-3.5" />
          </Button>
        </div>
      </div>

      {/* Subcategories */}
      {parent.subcategories.length > 0 ? (
        <div className="bg-muted/15 pb-1">
          {parent.subcategories.map((sub) => {
            // Siblings rendered side by side — the shade-aware resolver, not
            // the plain family hue, so an inheriting sibling's swatch reads
            // as distinct from its siblings' (and from the parent's own dot
            // elsewhere), rather than all rendering the identical parent
            // colour (locked decision #5 / 5.6 / 8.6).
            const subColor = resolveSiblingDisplayColor(sub, allCategories);
            return (
              <div
                key={sub.id}
                className="ml-6 flex items-center gap-3 border-l-2 border-border/60 py-1.5 pl-4 pr-4 transition-colors hover:bg-muted/40"
              >
                {sub.color ? (
                  <span
                    className="size-3.5 shrink-0 rounded-[4px] border border-border"
                    style={{ backgroundColor: sub.color }}
                  />
                ) : (
                  <span
                    className="size-3.5 shrink-0 rounded-[4px] border border-dashed border-border"
                    style={{ backgroundColor: subColor ?? undefined }}
                    title={
                      subColor
                        ? "Inheriting color from parent"
                        : "No color (neutral dot)"
                    }
                  />
                )}
                <span className="min-w-0 flex-1 truncate text-[12.5px] font-normal text-foreground/90">
                  {sub.name}
                </span>
                <Button
                  type="button"
                  variant="ghost"
                  onClick={() => onEditSubcategory(sub)}
                  aria-label={`Edit ${sub.name}`}
                  className="h-6 px-2 text-[11.5px]"
                >
                  Edit
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  onClick={() => onArchiveSubcategory(sub)}
                  className="h-6 px-1.5 text-[11.5px] text-muted-foreground hover:text-neg"
                  aria-label={`Archive ${sub.name}`}
                  title="Archive"
                >
                  <IconArchive className="size-3" />
                </Button>
              </div>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}

type CategoryFormDialogProps =
  | {
      mode: "create";
      allCategories: CategoryRead[];
      defaultParentId?: number | null;
      defaultKind?: CategoryKind;
      existingColors: (CategoryColor | null)[];
      onClose: () => void;
    }
  | {
      mode: "edit";
      category: CategoryRead;
      allCategories: CategoryRead[];
      existingColors: (CategoryColor | null)[];
      onClose: () => void;
    };

function CategoryFormDialog(props: CategoryFormDialogProps) {
  const { onClose, allCategories } = props;
  const category = props.mode === "edit" ? props.category : undefined;
  const queryClient = useQueryClient();

  const [name, setName] = useState(category?.name ?? "");
  const [kind, setKind] = useState<CategoryKind>(
    category?.kind ??
      (props.mode === "create" ? (props.defaultKind ?? "spend") : "spend"),
  );
  const [parentId, setParentId] = useState<number | null>(() => {
    if (props.mode === "create") {
      return props.defaultParentId ?? null;
    }
    return props.category.parent_id ?? null;
  });

  const [color, setColor] = useState<CategoryColor | null>(() =>
    props.mode === "edit"
      ? (props.category.color ?? null)
      : nextCategoryColor(props.existingColors),
  );

  // Eligible parent options — kind + archived-exclusion now live inside the
  // helper itself (8.4), so no caller-side re-filter is needed here.
  const eligibleParents = useMemo(() => {
    return getReparentingOptions(category ?? null, allCategories, kind);
  }, [category, allCategories, kind]);

  const selectedParent = useMemo(
    () => allCategories.find((c) => c.id === parentId) ?? null,
    [allCategories, parentId],
  );

  // Check if current category has children (preventing nesting)
  const isParentWithChildren = useMemo(() => {
    if (!category) return false;
    return allCategories.some((c) => c.parent_id === category.id);
  }, [category, allCategories]);

  function handleKindChange(nextKind: CategoryKind) {
    setKind(nextKind);
    // If switching kind and selected parent is of different kind, reset parent
    if (selectedParent && selectedParent.kind !== nextKind) {
      setParentId(null);
    }
  }

  const mutation = useMutation({
    mutationFn: () => {
      const trimmedName = name.trim();
      if (props.mode === "create") {
        return createCategory({
          name: trimmedName,
          kind,
          parent_id: parentId,
          ...(color != null ? { color } : {}),
        });
      }
      // Send ONLY changed fields
      const body: CategoryUpdate = {};
      if (trimmedName !== props.category.name) body.name = trimmedName;
      if (color !== (props.category.color ?? null)) body.color = color;
      if (parentId !== (props.category.parent_id ?? null)) {
        body.parent_id = parentId;
      }
      return patchCategory(props.category.id, body);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["categories"] });
      queryClient.invalidateQueries({ queryKey: ["dashboards"] });
      invalidateRules(queryClient);
      onClose();
    },
  });

  const trimmed = name.trim();
  const colorChanged =
    props.mode === "edit" && color !== (props.category.color ?? null);
  const parentChanged =
    props.mode === "edit" && parentId !== (props.category.parent_id ?? null);
  const changed =
    props.mode === "create"
      ? trimmed.length > 0
      : trimmed !== props.category.name || colorChanged || parentChanged;
  const canSubmit = trimmed.length > 0 && changed && !mutation.isPending;

  return (
    <Dialog open onOpenChange={(open) => (open ? null : onClose())}>
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <DialogTitle>
            {props.mode === "create"
              ? parentId != null
                ? "New subcategory"
                : "New category"
              : "Edit category"}
          </DialogTitle>
          <DialogDescription className="sr-only">
            {props.mode === "create"
              ? "Add a category for tagging transactions."
              : "Edit this category's name, parent, and color."}
          </DialogDescription>
        </DialogHeader>

        <Field label="Name">
          <TextInput
            value={name}
            onChange={setName}
            placeholder={
              parentId != null ? "e.g. Groceries" : "e.g. Food & Dining"
            }
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
                    onClick={() => handleKindChange(k)}
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

        {/* Parent Category Selector / Reparenting */}
        <div className="mt-3">
          <Field label="Parent category (optional)">
            {isParentWithChildren ? (
              <div className="rounded-md border border-border/80 bg-muted/40 px-3 py-2 text-[12px] text-muted-foreground">
                Parent category with active subcategories — cannot be nested
                under another category.
              </div>
            ) : (
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button
                    type="button"
                    variant="outline"
                    className="h-9 w-full justify-between px-2.5 text-[12.5px] font-normal"
                  >
                    <span className={cn(!selectedParent && "text-muted-foreground")}>
                      {selectedParent
                        ? selectedParent.name
                        : "None (Top-level parent category)"}
                    </span>
                    <IconChevronDown className="size-3 text-muted-foreground" />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent
                  align="start"
                  className="max-h-60 w-[--radix-dropdown-menu-trigger-width]"
                >
                  <DropdownMenuItem onSelect={() => setParentId(null)}>
                    <span className="text-muted-foreground">
                      None (Top-level parent category)
                    </span>
                  </DropdownMenuItem>
                  {eligibleParents.map((p) => (
                    <DropdownMenuItem
                      key={p.id}
                      onSelect={() => setParentId(p.id)}
                    >
                      {p.name}
                    </DropdownMenuItem>
                  ))}
                </DropdownMenuContent>
              </DropdownMenu>
            )}
          </Field>
        </div>

        {/* Color Palette */}
        <div className="mt-3">
          <Field
            label={
              parentId != null
                ? "Color (optional, inherits parent by default)"
                : "Color"
            }
          >
            <div className="flex flex-wrap gap-1.5">
              <button
                type="button"
                onClick={() => setColor(null)}
                aria-label="No custom color"
                aria-pressed={color === null}
                title={
                  parentId != null
                    ? "Inherit parent category color"
                    : "No custom color (neutral dot)"
                }
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
          <p className="mt-2 text-[12px] text-neg">
            {mutation.error instanceof ApiError
              ? mutation.error.detail
              : "Couldn’t save — try again."}
          </p>
        ) : null}

        <DialogFooter className="mt-4">
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
  subcategories,
  onClose,
}: {
  category: CategoryRead;
  subcategories: CategoryRead[];
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: () => deleteCategory(category.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["categories"] });
      queryClient.invalidateQueries({ queryKey: ["dashboards"] });
      invalidateRules(queryClient);
      onClose();
    },
  });

  const hasSubcategories = subcategories.length > 0;

  return (
    <Dialog open onOpenChange={(open) => (open ? null : onClose())}>
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <DialogTitle>Archive {category.name}?</DialogTitle>
          <DialogDescription>
            {hasSubcategories ? (
              <>
                Archiving <span className="font-semibold text-foreground">{category.name}</span> will
                also archive its <span className="font-semibold text-foreground">{subcategories.length}</span> child{" "}
                {subcategories.length === 1 ? "subcategory" : "subcategories"} (
                {subcategories.map((s) => s.name).join(", ")}).
                <br />
                <br />
                Merchant auto-tag rules will stop applying while archived.
                Existing transactions will keep their historical categories.
              </>
            ) : (
              <>
                It will be removed from active category lists and its merchant
                auto-tag rules will stop applying. Existing transactions will keep
                this category.
              </>
            )}
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
