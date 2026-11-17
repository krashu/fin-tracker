import { formatDateWithYear } from "@/lib/format";

/**
 * The statement's own printed period, or the plain statement that it printed none
 * (ADR-0010 rule 8). A period the statement did not print is never inferred from its
 * rows, so with either end missing this says "not printed" rather than guessing. The
 * term is defined in `GLOSSARY.md` ("Statement period"). Years are always shown: a
 * parsed statement can be from any year and the user is confirming what will be
 * committed.
 */
export function StatementPeriod({
  start,
  end,
}: {
  start: string | null;
  end: string | null;
}) {
  return (
    <span>
      {start != null && end != null
        ? `Statement period ${formatDateWithYear(start)} – ${formatDateWithYear(end)}`
        : "Statement period not printed"}
    </span>
  );
}
