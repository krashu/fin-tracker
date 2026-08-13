import assert from "node:assert/strict";
import { BASE_URL, getChromium, loginAsDemo } from "../helpers/browser.mjs";

export async function runCategoriesHierarchyTest() {
  console.log("  [Test] Running Categories Hierarchy E2E Test...");
  const chromium = await getChromium();
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();

  try {
    // 1. Authenticate
    await loginAsDemo(page);

    // 2. Navigate to /settings/categories
    await page.goto(`${BASE_URL}/settings/categories`, { waitUntil: "networkidle" });
    await page.waitForTimeout(600);

    // Check page title / header
    const heading = page.locator("h1, h2, h3").filter({ hasText: /categories/i }).first();
    assert.ok((await heading.count()) > 0, "Categories header must be visible");

    // Check that parent cards and subcategory buttons exist
    const subcategoryButtons = page.locator('button:has-text("Subcategory")');
    const subBtnCount = await subcategoryButtons.count();
    assert.ok(subBtnCount > 0, "Parent categories must have '+ Subcategory' button");

    // 3. Create a subcategory under the first parent
    const testSubName = `E2E Subcat ${Date.now()}`;
    await subcategoryButtons.first().click();
    await page.waitForTimeout(400);

    // Fill name and create
    const nameInput = page.locator('input[placeholder*="Groceries"], input[placeholder*="e.g."]').first();
    await nameInput.fill(testSubName);
    await page.click('button:has-text("Create")');
    await page.waitForTimeout(1000);

    // Verify newly created subcategory appears in the list
    const createdItem = page.locator(`text=${testSubName}`);
    assert.ok((await createdItem.count()) > 0, `Created subcategory '${testSubName}' must appear in list`);

    // 4. Test Edit dialog on newly created subcategory
    const editBtn = page
      .locator(`div:has-text("${testSubName}")`)
      .locator('button:has-text("Edit")')
      .first();
    if ((await editBtn.count()) > 0) {
      await editBtn.click();
      await page.waitForTimeout(400);

      // Verify parent category reparenting field exists
      const parentField = page.locator('text=Parent category (optional)');
      assert.ok(
        (await parentField.count()) > 0,
        "Reparenting selector field must be present in edit dialog",
      );
      await page.click('button:has-text("Cancel")');
      await page.waitForTimeout(300);
    }

    console.log("  ✓ Categories Hierarchy E2E Test passed");
  } finally {
    await browser.close();
  }
}
