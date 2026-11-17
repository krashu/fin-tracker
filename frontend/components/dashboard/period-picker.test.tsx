import { describe, expect, it, vi } from "vitest";
import { PeriodPicker } from "./period-picker";
import { renderWithProviders, screen } from "@/tests/helpers/test-utils";

describe("PeriodPicker (components/dashboard/period-picker.tsx)", () => {
  it("labels years as the calendar year and keeps a February pick in that year", async () => {
    const onChange = vi.fn();
    const { user } = renderWithProviders(
      <PeriodPicker
        period={{ year: 2026 }}
        availableYears={[2026, 2025]}
        onChange={onChange}
      />,
    );

    // PRD §F8: a spend year is the calendar year.
    expect(screen.getByText("2026")).toBeInTheDocument();

    await user.click(screen.getByText("All months"));
    const items = (await screen.findAllByRole("menuitem")).map((i) => i.textContent);
    // The month menu runs January to December.
    expect(items).toEqual([
      "All months",
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
    ]);

    await user.click(screen.getByRole("menuitem", { name: "February" }));
    expect(onChange).toHaveBeenCalledWith({ year: 2026, mon: 2 });
  });

  it("keeps the month when another year is picked", async () => {
    const onChange = vi.fn();
    const { user } = renderWithProviders(
      <PeriodPicker
        period={{ year: 2026, mon: 2 }}
        availableYears={[2026, 2025]}
        onChange={onChange}
      />,
    );

    await user.click(screen.getByText("2026"));
    await user.click(await screen.findByRole("menuitem", { name: "2025" }));
    expect(onChange).toHaveBeenCalledWith({ year: 2025, mon: 2 });
  });
});
