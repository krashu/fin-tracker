import { describe, expect, it } from "vitest";
import {
  LABEL_INPUT_MAX_CHARS,
  labelDisplay,
  normalizeLabelName,
  sameLabelSet,
} from "@/lib/labels";
import { LABEL_MAX_CHARS } from "@/lib/utils";

describe("labels helpers (PRD §F3a)", () => {
  it("formats stored label for chip display with leading #", () => {
    expect(labelDisplay("travel")).toBe("#travel");
    expect(labelDisplay("groceries")).toBe("#groceries");
  });

  it("sets input max length to 1 more than storage cap for the # prefix", () => {
    expect(LABEL_INPUT_MAX_CHARS).toBe(LABEL_MAX_CHARS + 1);
  });

  it("normalizes user-typed label strings to canonical format", () => {
    // Leading hash stripped, lowercased, whitespace trimmed
    expect(normalizeLabelName("#Travel")).toBe("travel");
    expect(normalizeLabelName("  #Food & Dining   ")).toBe("food & dining");
    expect(normalizeLabelName("vacation;trip")).toBe("vacationtrip"); // semicolon stripped
    expect(normalizeLabelName("multi   space   tag")).toBe("multi space tag");
    expect(normalizeLabelName("")).toBe("");
    expect(normalizeLabelName("   ")).toBe("");
    expect(normalizeLabelName("#")).toBe("");
  });

  it("truncates normalized label to 64 chars", () => {
    const longName = "a".repeat(100);
    const normalized = normalizeLabelName(longName);
    expect(normalized.length).toBe(64);
    expect(normalized).toBe("a".repeat(64));
  });

  it("compares label sets independent of order", () => {
    expect(sameLabelSet(["a", "b", "c"], ["c", "a", "b"])).toBe(true);
    expect(sameLabelSet(["a", "b"], ["a", "b", "c"])).toBe(false);
    expect(sameLabelSet(["a", "b"], ["a", "d"])).toBe(false);
    expect(sameLabelSet([], [])).toBe(true);
  });
});
