"use client";

/**
 * Auto-tag rules list + author + prune (PRD §F3 / §F3a). Reads the ["rules"]
 * query — the grouped-by-merchant view of merchant_tag_map (→ category) and
 * merchant_label_map (→ tags) — and lets the user:
 *   - create/pin a rule (the "New rule" dialog) — a pinned rule always wins the
 *     prefill regardless of hit_count, so imports can't silently overturn it;
 *   - re-point a merchant's suggested category, or pin/un-pin a tag (row menu);
 *   - add a tag to a merchant (per-merchant "Add tag");
 *   - forget a rule (delete) — which does NOT touch any committed transaction, so
 *     unlike deleting a category/tag we don't invalidate ["transactions"].
 *
 * Every write invalidates ["rules"] plus ["dashboards","tagging-stats"] (the
 * health card's rules_count) and ["candidates"] (an open review queue derives
 * prior-match confidence live from the maps). Not ["labels"] — this page never
 * creates a tag (pins reference existing ones only).
 */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
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
import { IconChevronDown, IconPlus } from "@/components/icons";
import { LabelChip } from "@/components/labels/label-chip";
import {
  ApiError,
  createLabelRule,
  deleteCategoryRule,
  deleteLabelRule,
  listRules,
  patchCategoryRulePinned,
  patchLabelRulePinned,
  type CategoryRuleRead,
  type LabelRead,
  type LabelRuleRead,
} from "@/lib/api/client";
import { invalidateRules } from "@/lib/queries/invalidate";
import { labelDisplay } from "@/lib/labels";
import { cn } from "@/lib/utils";
import { ExistingLabelPicker } from "./existing-label-picker";
import { NewRuleDialog } from "./new-rule-dialog";

/** The one open forget-confirm dialog (or none); each variant carries its row. */
type RuleDialog =
  | null
  | { kind: "category"; merchant: string; rule: CategoryRuleRead }
  | { kind: "label"; merchant: string; rule: LabelRuleRead };

export function RulesManager() {
  const rulesQuery = useQuery({ queryKey: ["rules"], queryFn: listRules });
  const rules = rulesQuery.data ?? [];

  const [dialog, setDialog] = useState<RuleDialog>(null);
  const [newOpen, setNewOpen] = useState(false);

  return (
    <Card className="max-w-3xl">
      <CardHeader className="items-center border-b">
        <CardTitle className="text-[14px]">
          {rules.length} {rules.length === 1 ? "merchant" : "merchants"}
        </CardTitle>
        <CardAction className="self-center">
          <Button
            className="h-8 gap-1 px-2.5 text-[12.5px]"
            onClick={() => setNewOpen(true)}
          >
            <IconPlus className="size-3.5" />
            New rule
          </Button>
        </CardAction>
      </CardHeader>

      <Legend />

      <CardContent className="px-0">
        {rulesQuery.isPending ? (
          <Row tone="muted">Loading…</Row>
        ) : rulesQuery.isError ? (
          <Row tone="error">Couldn’t load rules — is the API running?</Row>
        ) : rules.length === 0 ? (
          <Row tone="muted">
            No rules yet — confirm categories and tags on imported transactions
            and fin-tracker remembers them here, or add one with “New rule”.
          </Row>
        ) : (
          rules.map((rule) => (
            <div
              key={rule.merchant_normalized}
              className="border-b border-border/60 px-4 py-3 last:border-b-0"
            >
              <div className="flex items-center justify-between gap-2">
                <p className="font-mono text-[12px] font-medium text-foreground">
                  {rule.merchant_normalized}
                </p>
                <AddTagControl
                  merchant={rule.merchant_normalized}
                  existingLabelIds={new Set(rule.labels.map((l) => l.label_id))}
                />
              </div>

              {rule.categories.length > 0 ? (
                <div className="mt-2 space-y-1.5">
                  {rule.categories.map((cat) => (
                    <CategoryRuleLine
                      key={`c-${cat.id}`}
                      rule={cat}
                      onForget={() =>
                        setDialog({
                          kind: "category",
                          merchant: rule.merchant_normalized,
                          rule: cat,
                        })
                      }
                    />
                  ))}
                </div>
              ) : null}

              {rule.labels.length > 0 ? (
                <div className="mt-2 space-y-1.5">
                  {rule.labels.map((lab) => (
                    <LabelRuleLine
                      key={`l-${lab.id}`}
                      rule={lab}
                      onForget={() =>
                        setDialog({
                          kind: "label",
                          merchant: rule.merchant_normalized,
                          rule: lab,
                        })
                      }
                    />
                  ))}
                </div>
              ) : null}
            </div>
          ))
        )}
      </CardContent>

      <NewRuleDialog open={newOpen} onClose={() => setNewOpen(false)} />
      {dialog ? (
        <ForgetConfirm dialog={dialog} onClose={() => setDialog(null)} />
      ) : null}
    </Card>
  );
}

function Legend() {
  return (
    <p className="flex flex-wrap gap-x-3 gap-y-1 border-b border-border/60 px-4 py-2 text-[11px] text-muted-foreground">
      <span>
        <b className="font-medium text-foreground">suggested</b> — prefilled at
        import
      </span>
      <span>
        <b className="font-medium text-foreground">auto-applies</b> — tag added
        automatically
      </span>
      <span>
        <b className="font-medium text-foreground">learning</b> — not seen
        enough yet
      </span>
      <span>
        <PinnedBadge /> — you set this; always wins
      </span>
    </p>
  );
}

function PinnedBadge() {
  return (
    <span className="rounded-sm bg-accent px-1 py-px text-[10px] font-medium uppercase tracking-wide text-accent-foreground">
      pinned
    </span>
  );
}

/** Shared row shell: chip + status on the left, count + actions menu on the right. */
function RuleLine({
  children,
  hitCount,
  menu,
}: {
  children: React.ReactNode;
  hitCount: number;
  menu: React.ReactNode;
}) {
  return (
    <div className="flex items-center gap-2">
      {children}
      <span className="min-w-0 flex-1" />
      <span
        className="text-[11px] tabular-nums text-muted-foreground"
        title={`Matched ${hitCount} ${hitCount === 1 ? "time" : "times"}`}
      >
        {hitCount}×
      </span>
      {menu}
    </div>
  );
}

function StatusText({ children }: { children: React.ReactNode }) {
  return <span className="text-[11px] text-muted-foreground">{children}</span>;
}

/** The row actions dropdown. `items` are the state-specific entries above Forget. */
function RowMenu({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          className="h-7 w-7 px-0 text-muted-foreground hover:text-foreground"
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

function CategoryRuleLine({
  rule,
  onForget,
}: {
  rule: CategoryRuleRead;
  onForget: () => void;
}) {
  const qc = useQueryClient();
  const pin = useMutation({
    mutationFn: (pinned: boolean) => patchCategoryRulePinned(rule.id, pinned),
    onSuccess: () => invalidateRules(qc),
  });

  return (
    <RuleLine
      hitCount={rule.hit_count}
      menu={
        <RowMenu label={`Actions for ${rule.category_name}`}>
          {rule.pinned ? (
            <DropdownMenuItem onSelect={() => pin.mutate(false)}>
              Unpin (revert to learned)
            </DropdownMenuItem>
          ) : rule.is_winner ? (
            <DropdownMenuItem onSelect={() => pin.mutate(true)}>
              Pin this suggestion
            </DropdownMenuItem>
          ) : (
            <DropdownMenuItem onSelect={() => pin.mutate(true)}>
              Make the suggestion
            </DropdownMenuItem>
          )}
          <DropdownMenuSeparator />
          <DropdownMenuItem variant="destructive" onSelect={onForget}>
            Forget rule
          </DropdownMenuItem>
        </RowMenu>
      }
    >
      <Badge variant="secondary" className="font-normal">
        {rule.category_name}
      </Badge>
      {rule.pinned ? <PinnedBadge /> : null}
      {rule.is_winner ? (
        <StatusText>suggested</StatusText>
      ) : (
        <StatusText>also learned</StatusText>
      )}
    </RuleLine>
  );
}

function LabelRuleLine({
  rule,
  onForget,
}: {
  rule: LabelRuleRead;
  onForget: () => void;
}) {
  const qc = useQueryClient();
  const pin = useMutation({
    mutationFn: (pinned: boolean) => patchLabelRulePinned(rule.id, pinned),
    onSuccess: () => invalidateRules(qc),
  });

  return (
    <RuleLine
      hitCount={rule.hit_count}
      menu={
        <RowMenu label={`Actions for ${labelDisplay(rule.label_name)}`}>
          {rule.pinned ? (
            <DropdownMenuItem onSelect={() => pin.mutate(false)}>
              Unpin (revert to learned)
            </DropdownMenuItem>
          ) : (
            <DropdownMenuItem onSelect={() => pin.mutate(true)}>
              Pin (always apply)
            </DropdownMenuItem>
          )}
          <DropdownMenuSeparator />
          <DropdownMenuItem variant="destructive" onSelect={onForget}>
            Forget rule
          </DropdownMenuItem>
        </RowMenu>
      }
    >
      <LabelChip name={rule.label_name} />
      {rule.pinned ? <PinnedBadge /> : null}
      {rule.prefills ? (
        <StatusText>auto-applies</StatusText>
      ) : (
        <StatusText>
          learning · {rule.hit_count}/{rule.prefill_threshold}
        </StatusText>
      )}
    </RuleLine>
  );
}

/** Per-merchant "Add tag" — pins an existing tag onto this merchant. */
function AddTagControl({
  merchant,
  existingLabelIds,
}: {
  merchant: string;
  existingLabelIds: Set<number>;
}) {
  const qc = useQueryClient();
  const add = useMutation({
    mutationFn: (label: LabelRead) =>
      createLabelRule({ merchant, label_id: label.id }),
    onSuccess: () => invalidateRules(qc),
  });

  return (
    <ExistingLabelPicker
      exclude={existingLabelIds}
      onPick={(label) => add.mutate(label)}
      trigger={
        <button
          type="button"
          className="inline-flex items-center gap-1 rounded-sm px-1 py-0.5 text-[11px] text-muted-foreground outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
        >
          <IconPlus className="size-3" />
          Add tag
        </button>
      }
    />
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

function ForgetConfirm({
  dialog,
  onClose,
}: {
  dialog: NonNullable<RuleDialog>;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const target =
    dialog.kind === "category"
      ? dialog.rule.category_name
      : labelDisplay(dialog.rule.label_name);

  const mutation = useMutation({
    mutationFn: () =>
      dialog.kind === "category"
        ? deleteCategoryRule(dialog.rule.id)
        : deleteLabelRule(dialog.rule.id),
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
            Forget {dialog.merchant} → {target}?
          </DialogTitle>
          <DialogDescription>
            fin-tracker stops suggesting this for {dialog.merchant}. Your
            transactions keep their{" "}
            {dialog.kind === "category" ? "category" : "tags"} — it re-learns
            the next time you confirm this merchant.
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
            {mutation.isPending ? "Forgetting…" : "Forget"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
