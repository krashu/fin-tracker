import { beforeEach, describe, expect, it } from "vitest";
import { ThemeToggle } from "@/components/shell/theme-toggle";
import { renderWithProviders, screen } from "@/tests/helpers/test-utils";

describe("ThemeToggle & ThemeProvider", () => {
  beforeEach(() => {
    document.documentElement.classList.remove("dark");
    window.localStorage.clear();
  });

  it("renders with light theme by default", () => {
    renderWithProviders(<ThemeToggle />);
    const button = screen.getByRole("button", { name: "Switch to dark theme" });
    expect(button).toBeInTheDocument();
    expect(document.documentElement.classList.contains("dark")).toBe(false);
  });

  it("toggles to dark theme on click and updates localStorage and document class", async () => {
    const { user } = renderWithProviders(<ThemeToggle />);
    const button = screen.getByRole("button", { name: "Switch to dark theme" });

    await user.click(button);

    expect(document.documentElement.classList.contains("dark")).toBe(true);
    expect(window.localStorage.getItem("theme")).toBe("dark");
    expect(screen.getByRole("button", { name: "Switch to light theme" })).toBeInTheDocument();

    // Toggle back to light
    await user.click(screen.getByRole("button", { name: "Switch to light theme" }));

    expect(document.documentElement.classList.contains("dark")).toBe(false);
    expect(window.localStorage.getItem("theme")).toBe("light");
    expect(screen.getByRole("button", { name: "Switch to dark theme" })).toBeInTheDocument();
  });
});
