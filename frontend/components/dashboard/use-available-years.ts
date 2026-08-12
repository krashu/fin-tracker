"use client";

/**
 * Distinct calendar years worth offering in a year picker — the shared query
 * behind every `<PeriodPicker>` (overview.tsx, spending-dashboard.tsx,
 * filter-row.tsx). One query, not three copies of the same
 * `["dashboards", "available-years"]` fetch.
 *
 * The empty-array case is handled explicitly: `?? [...]` does not catch `[]`
 * (an empty array isn't nullish), so without this a resolved-but-dataless
 * query would render a zero-item dropdown instead of falling back to the
 * current year — the bug both /dashboard and /spending shipped with before
 * this hook existed.
 */
import { useQuery } from "@tanstack/react-query";

import { listAvailableYears } from "@/lib/api/client";

export function useAvailableYears() {
  const query = useQuery({
    queryKey: ["dashboards", "available-years"],
    queryFn: listAvailableYears,
  });
  const fetched = query.data?.years;
  const years = fetched && fetched.length > 0 ? fetched : [new Date().getUTCFullYear()];
  return { ...query, years };
}
