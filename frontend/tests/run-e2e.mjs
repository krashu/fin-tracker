#!/usr/bin/env node

/**
 * Frontend E2E Test Runner using Playwright.
 * Runs all automated UI test suites and reports summary.
 */
import { runCategoriesHierarchyTest } from "./e2e/categories-hierarchy.test.mjs";
import { runCategorySelectorTest } from "./e2e/category-selector.test.mjs";

async function main() {
  console.log("==========================================");
  console.log("   Fin-Tracker Frontend E2E Test Suite    ");
  console.log("==========================================");

  const tests = [
    { name: "Categories Hierarchy & Management", fn: runCategoriesHierarchyTest },
    { name: "Category Selector Combobox & Scroll", fn: runCategorySelectorTest },
  ];

  let passed = 0;
  let failed = 0;
  const startTime = Date.now();

  for (const test of tests) {
    try {
      await test.fn();
      passed++;
    } catch (err) {
      console.error(`  ✗ ${test.name} failed:`, err.message);
      failed++;
    }
  }

  const duration = ((Date.now() - startTime) / 1000).toFixed(2);
  console.log("------------------------------------------");
  console.log(`Results: ${passed} passed, ${failed} failed (${duration}s)`);
  console.log("==========================================");

  if (failed > 0) {
    process.exit(1);
  }
}

main().catch((err) => {
  console.error("Fatal test runner error:", err);
  process.exit(1);
});
