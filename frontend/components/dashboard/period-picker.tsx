"use client";

/**
 * A month-or-year picker for the dashboards' period-taking routes — one
 * component replacing three hand-rolled selectors: the year stepper on
 * /dashboard (year-only — that page has never had month granularity), the
 * year+month dropdown pair + `MONTH_OPTIONS` on /spending, and the year pill
 * on /expenses (whose separate month stepper is untouched — it drives
 * `monthAnchor`, a different, board-specific control this component doesn't
 * own).
 *
 * The year is the calendar year (PRD §F8), shown as "2026". `allowMonth={false}`
 * renders year-only. `allowMonth={true}` (default) adds the month dropdown, listed
 * January to December; picking "All months" drops back to a year-only period,
 * matching `Period`'s own shape (`mon` absent = whole year). Picking another year
 * keeps the month. `yearLabel` overrides how a year is named: the /tax page passes
 * FY start years, year-only, and says "Tax year 2026-27".
 */
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Pill } from "@/components/ui/pill";
import { IconChevronDown } from "@/components/icons";
import { periodLabel, type Period } from "@/lib/period";

const MONTH_NAMES = [
  "January",
  "February",
  "March",
  "April",
  "May",
  "June",
  "July",
  "August",
  "September",
  "October",
  "November",
  "December",
] as const;

export function PeriodPicker({
  period,
  availableYears,
  onChange,
  allowMonth = true,
  yearLabel = (y) => periodLabel({ year: y }),
}: {
  period: Period;
  availableYears: number[];
  onChange: (next: Period) => void;
  allowMonth?: boolean;
  yearLabel?: (year: number) => string;
}) {
  const year = period.year;
  return (
    <div className="flex items-center gap-2">
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Pill active={true}>
            {yearLabel(year)}
            <IconChevronDown className="size-3 opacity-70" />
          </Pill>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="max-h-72 w-36">
          {availableYears.map((yr) => (
            <DropdownMenuItem
              key={yr}
              onSelect={() =>
                onChange(period.mon != null ? { year: yr, mon: period.mon } : { year: yr })
              }
            >
              {yearLabel(yr)}
            </DropdownMenuItem>
          ))}
        </DropdownMenuContent>
      </DropdownMenu>

      {allowMonth ? (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Pill active={period.mon != null}>
              {period.mon != null ? MONTH_NAMES[period.mon - 1] : "All months"}
              <IconChevronDown className="size-3 opacity-70" />
            </Pill>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" className="max-h-72 w-40">
            <DropdownMenuItem onSelect={() => onChange({ year })}>
              <span className="text-muted-foreground">All months</span>
            </DropdownMenuItem>
            {MONTH_NAMES.map((name, i) => (
              <DropdownMenuItem key={name} onSelect={() => onChange({ year, mon: i + 1 })}>
                {name}
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
      ) : null}
    </div>
  );
}
