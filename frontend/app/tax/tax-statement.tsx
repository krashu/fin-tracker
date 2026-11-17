"use client";

/**
 * The /tax body: a tax-year picker over the F11 sections (PRD §F11). The year list comes
 * from `GET /tax/available-years` (every year with a realised sale, plus the current one);
 * the picked year is a financial-year start year (the spend dashboards use calendar years).
 */
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { PeriodPicker } from "@/components/dashboard/period-picker";
import { listTaxYears } from "@/lib/api/client";
import { currentFy, fyOf, taxYearLabel } from "@/lib/period";
import { IntradayTrades } from "./intraday-trades";

export function TaxStatement() {
  const [fy, setFy] = useState(() => currentFy());
  const { data } = useQuery({
    queryKey: ["tax", "available-years"],
    queryFn: listTaxYears,
  });
  const years = data?.years?.length ? data.years : [currentFy()];

  return (
    <div className="mt-6 flex flex-col gap-5">
      <PeriodPicker
        period={{ year: fy }}
        availableYears={years}
        onChange={(p) => setFy(fyOf(p))}
        allowMonth={false}
        yearLabel={taxYearLabel}
      />
      <IntradayTrades year={fy} />
    </div>
  );
}
