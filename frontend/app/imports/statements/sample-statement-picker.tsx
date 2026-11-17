"use client";

/**
 * The guest's way into the import flow (ADR-0003 rule 15): bundled synthetic sample
 * statements. The server says which of the user's accounts each sample fits
 * (`account_ids`), so nothing here matches issuers. Importing stages the rows and opens the
 * same review queue an upload does.
 */
import { useState } from "react";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import { Field, PickerButton } from "@/components/form/fields";
import { DropdownMenuItem } from "@/components/ui/dropdown-menu";
import {
  ApiError,
  importSampleStatement,
  listAccounts,
  listSampleStatements,
  type AccountRead,
  type SampleStatementRead,
} from "@/lib/api/client";
import { accountLabel } from "@/lib/accounts";

export function SampleStatementPicker() {
  const samples = useQuery({
    queryKey: ["imports", "samples"],
    queryFn: listSampleStatements,
    staleTime: Infinity,
  });
  // Only to label the account picker; the server owns which accounts fit.
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: listAccounts });

  if (samples.isError) {
    return <p className="text-[12px] text-neg">Couldn’t load the sample statements.</p>;
  }
  return (
    <div className="flex max-w-xl flex-col gap-3">
      <p className="text-[12.5px] text-muted-foreground">
        Guests can’t upload files. Import a sample statement to try the review flow.
      </p>
      {(samples.data ?? []).map((sample) => (
        <SampleCard key={sample.id} sample={sample} accounts={accounts.data ?? []} />
      ))}
    </div>
  );
}

function SampleCard({
  sample,
  accounts,
}: {
  sample: SampleStatementRead;
  accounts: AccountRead[];
}) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [accountId, setAccountId] = useState<number | null>(null);
  const [done, setDone] = useState(false);
  const target = accountId ?? sample.account_ids[0] ?? null;

  const mutation = useMutation({
    mutationFn: (id: number) =>
      importSampleStatement({ sample_id: sample.id, account_id: id }),
    onSuccess: (s) => {
      queryClient.invalidateQueries({ queryKey: ["imports", "pending"] });
      if (s.pending_count > 0) {
        const qs = s.skipped > 0 ? `?present=${s.skipped}` : "";
        router.push(`/imports/review/${s.batch_id}${qs}`);
      } else setDone(true);
    },
  });

  const labelFor = (id: number) => {
    const a = accounts.find((x) => x.id === id);
    return a ? accountLabel(a) : `Account ${id}`;
  };

  return (
    <div className="flex flex-col gap-3 rounded-lg border border-border p-4">
      <p className="text-[13px] font-medium text-foreground">{sample.label}</p>
      {sample.account_ids.length > 1 ? (
        <Field label="Import into">
          <PickerButton label={target != null ? labelFor(target) : "Choose an account"}>
            {sample.account_ids.map((id) => (
              <DropdownMenuItem key={id} onSelect={() => setAccountId(id)}>
                {labelFor(id)}
              </DropdownMenuItem>
            ))}
          </PickerButton>
        </Field>
      ) : null}
      {mutation.isError ? (
        <p className="text-[12px] text-neg">
          {mutation.error instanceof ApiError
            ? mutation.error.detail
            : "Couldn’t import — try again."}
        </p>
      ) : null}
      {done ? (
        <p className="text-[12px] text-muted-foreground">
          This sample is already in your expenses.
        </p>
      ) : null}
      <div>
        <Button
          type="button"
          className="h-9 px-3.5 text-[12.5px] font-medium"
          disabled={target == null || mutation.isPending}
          onClick={() => target != null && mutation.mutate(target)}
        >
          {target == null
            ? "Needs a matching account"
            : mutation.isPending
              ? "Importing…"
              : "Import sample"}
        </Button>
      </div>
    </div>
  );
}
