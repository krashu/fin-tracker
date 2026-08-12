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
 * `allowMonth={false}` renders year-only. `allowMonth={true}` (default) adds
 * the month dropdown; picking "All months" drops back to a year-only period,
 * matching `Period`'s own shape (`mon` absent = whole year).
 */
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Pill } from "@/components/ui/pill";
import { IconChevronDown } from "@/components/icons";
import type { Period } from "@/lib/period";

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
}: {
  period: Period;
  availableYears: number[];
  onChange: (next: Period) => void;
  allowMonth?: boolean;
}) {
  return (
    <div className="flex items-center gap-2">
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Pill active={true}>
            {period.year}
            <IconChevronDown className="size-3 opacity-70" />
          </Pill>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="max-h-72 w-32">
          {availableYears.map((yr) => (
            <DropdownMenuItem
              key={yr}
              onSelect={() => onChange({ year: yr, mon: period.mon })}
            >
              {yr}
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
            <DropdownMenuItem onSelect={() => onChange({ year: period.year })}>
              <span className="text-muted-foreground">All months</span>
            </DropdownMenuItem>
            {MONTH_NAMES.map((name, idx) => (
              <DropdownMenuItem
                key={name}
                onSelect={() => onChange({ year: period.year, mon: idx + 1 })}
              >
                {name}
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
      ) : null}
    </div>
  );
}
