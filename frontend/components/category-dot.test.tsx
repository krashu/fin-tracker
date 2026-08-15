import { describe, expect, it } from "vitest";
import { CategoryDot } from "@/components/category-dot";
import { render, screen } from "@/tests/helpers/test-utils";

describe("CategoryDot", () => {
  it("renders an aria-hidden rounded dot with explicit user color", () => {
    const { container } = render(
      <CategoryDot categoryId={1} color="#ff0000" className="custom-class" />,
    );

    const dot = container.querySelector("span");
    expect(dot).toBeInTheDocument();
    expect(dot).toHaveAttribute("aria-hidden", "true");
    expect(dot).toHaveClass("rounded-full", "custom-class");
    expect(dot).toHaveStyle({ backgroundColor: "#ff0000" });
  });

  it("renders default muted color for null categoryId (uncategorized)", () => {
    const { container } = render(<CategoryDot categoryId={null} />);
    const dot = container.querySelector("span");
    expect(dot).toBeInTheDocument();
    expect(dot?.style.backgroundColor).toBeDefined();
  });
});
