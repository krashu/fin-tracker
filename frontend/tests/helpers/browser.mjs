export const BASE_URL = process.env.TEST_BASE_URL || "http://localhost:3000";

export async function getChromium() {
  try {
    const pw = await import("playwright");
    return pw.chromium;
  } catch {}
  try {
    const pwCore = await import("playwright-core");
    return pwCore.chromium;
  } catch {}
  try {
    const globalCliPath =
      "file:///C:/Users/ashut/AppData/Roaming/npm/node_modules/@playwright/cli/node_modules/playwright-core/index.mjs";
    const pw = await import(globalCliPath);
    return pw.chromium;
  } catch (err) {
    throw new Error(
      "Playwright not found. Install playwright or @playwright/cli: " +
        err.message,
    );
  }
}

export async function loginAsDemo(page) {
  await page.goto(`${BASE_URL}/`, { waitUntil: "networkidle" });
  if (page.url().includes("/login")) {
    const demoButton = page.locator('button:has-text("Try the demo")');
    if ((await demoButton.count()) > 0) {
      await demoButton.click();
      await page.waitForNavigation({ waitUntil: "networkidle" }).catch(() => {});
      await page.waitForTimeout(800);
    }
  }
}
