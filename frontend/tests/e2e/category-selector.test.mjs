import assert from "node:assert/strict";
import { BASE_URL, getChromium, loginAsDemo } from "../helpers/browser.mjs";

export async function runCategorySelectorTest() {
  console.log("  [Test] Running Category Selector & Scroll E2E Test...");
  const chromium = await getChromium();
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();

  try {
    // 1. Authenticate
    await loginAsDemo(page);

    // 2. Navigate to /expenses
    await page.goto(`${BASE_URL}/expenses`, { waitUntil: "networkidle" });
    await page.waitForTimeout(600);

    // Open Add Transaction modal
    const addTxnBtn = page.locator('button:has-text("Add transaction"), button:has-text("Add")').first();
    assert.ok((await addTxnBtn.count()) > 0, "Add transaction button must exist");
    await addTxnBtn.click();
    await page.waitForTimeout(400);

    // Open CategorySelector in modal
    const categorySelectorTrigger = page
      .locator('[data-slot="dialog-content"] button')
      .filter({ hasText: /Uncategorized|Select category/i })
      .first();
    assert.ok((await categorySelectorTrigger.count()) > 0, "Category selector trigger must exist");
    await categorySelectorTrigger.click();
    await page.waitForTimeout(400);

    // Verify CommandList & items exist
    const commandList = page.locator('[data-slot="command-list"], [cmdk-list]').first();
    assert.ok((await commandList.count()) > 0, "Command list must open");

    // 3. Test Real-time Search Filtering
    const searchInput = page.locator('input[placeholder*="Search categories"]').first();
    await searchInput.fill("Food");
    await page.waitForTimeout(300);
    const filteredItems = page.locator('[data-slot="command-item"], [cmdk-item]');
    assert.ok((await filteredItems.count()) > 0, "Search for 'Food' must return matching category items");

    // Clear search
    await searchInput.fill("");
    await page.waitForTimeout(300);

    // 4. Test Mouse Wheel Scroll inside the dialog popover
    const box = await commandList.boundingBox();
    assert.ok(box, "CommandList bounding box must exist for scroll test");

    const initialScrollTop = await commandList.evaluate((el) => el.scrollTop);
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.wheel(0, 300);
    await page.waitForTimeout(400);

    const afterScrollTop = await commandList.evaluate((el) => el.scrollTop);
    assert.ok(
      afterScrollTop > initialScrollTop,
      `Mouse wheel scroll must increase scrollTop (was ${initialScrollTop}, now ${afterScrollTop})`,
    );

    // 5. Select a category
    const categoryOption = page.locator('[data-slot="command-item"]').filter({ hasText: /Food|Groceries|Shopping/ }).first();
    if ((await categoryOption.count()) > 0) {
      await categoryOption.click();
      await page.waitForTimeout(300);
    }

    // Close modal
    const closeBtn = page.locator('[data-slot="dialog-close"], button:has-text("Done"), button:has-text("Cancel")').first();
    if ((await closeBtn.count()) > 0) {
      await closeBtn.click();
    }

    console.log("  ✓ Category Selector & Scroll E2E Test passed");
  } finally {
    await browser.close();
  }
}
