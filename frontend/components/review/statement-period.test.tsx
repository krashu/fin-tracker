import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { StatementPeriod } from "./statement-period";

describe("StatementPeriod (components/review/statement-period.tsx)", () => {
  it("says the period was not printed when the statement printed none", () => {
    const { rerender } = render(<StatementPeriod start={null} end={null} />);
    expect(screen.getByText(/Statement period not printed/)).toBeInTheDocument();

    // One printed end is not a period either (GLOSSARY: Statement period).
    rerender(<StatementPeriod start="2026-06-01" end={null} />);
    expect(screen.getByText(/Statement period not printed/)).toBeInTheDocument();
    rerender(<StatementPeriod start={null} end="2026-06-30" />);
    expect(screen.getByText(/Statement period not printed/)).toBeInTheDocument();
  });

  it("shows the printed period with years", () => {
    render(<StatementPeriod start="2025-12-01" end="2025-12-31" />);
    const el = screen.getByText(/^Statement period /);
    expect(el).not.toHaveTextContent("not printed");
    // The year is never inferred: both ends carry it.
    expect(el.textContent).toMatch(/1 Dec 2025/);
    expect(el.textContent).toMatch(/31 Dec 2025/);
  });
});
