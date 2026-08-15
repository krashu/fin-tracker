import { describe, expect, it, vi } from "vitest";
import { LabelChip } from "@/components/labels/label-chip";
import { render, screen } from "@/tests/helpers/test-utils";
import userEvent from "@testing-library/user-event";

describe("LabelChip", () => {
  it("renders label name with # prefix", () => {
    render(<LabelChip name="travel" />);
    expect(screen.getByText("#travel")).toBeInTheDocument();
    // No remove button in read-only mode
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("renders remove button and triggers onRemove callback when clicked", async () => {
    const user = userEvent.setup();
    const handleRemove = vi.fn();

    render(<LabelChip name="dining" onRemove={handleRemove} />);

    const removeBtn = screen.getByRole("button", { name: "Remove #dining" });
    expect(removeBtn).toBeInTheDocument();

    await user.click(removeBtn);
    expect(handleRemove).toHaveBeenCalledTimes(1);
  });
});
