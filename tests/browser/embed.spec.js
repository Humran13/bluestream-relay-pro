// @ts-check
const { test, expect, devices } = require("@playwright/test");

// Verifies the WordPress/website embedding path documented in
// docs/wordpress.md: an <iframe src=".../player/?relay=..."> on an
// unrelated host page must load and render the player, exactly as a
// WordPress post embedding the bundled player would. Also verifies mobile
// browser playback via Playwright's device emulation (real phone hardware
// is NOT available in this environment - see test titles/comments for what
// is and is not covered).

test("player loads correctly inside a cross-origin-style iframe (WordPress embed simulation)", async ({ page }) => {
  // A minimal "host page" standing in for a WordPress post body, using the
  // exact iframe snippet from docs/wordpress.md.
  await page.setContent(`
    <!doctype html>
    <html><body>
      <h1>A WordPress post</h1>
      <iframe id="bsframe" src="http://127.0.0.1:4873/player/?relay=1080test1"
              width="960" height="540" frameborder="0" allowfullscreen></iframe>
    </body></html>
  `, { baseURL: undefined });

  const frame = page.frameLocator("#bsframe");
  await expect(frame.locator("#channel-label")).toHaveText("Relay: 1080test1");
  await expect(frame.locator("#error")).not.toContainText("No channel specified");
});

for (const deviceName of ["iPhone 14", "Pixel 7"]) {
  test(`mobile browser playback renders correctly on ${deviceName} viewport/UA`, async ({ browser, browserName }) => {
    // Firefox's engine doesn't support the isMobile context flag at all
    // (real-world mobile Firefox isn't Playwright-emulatable either); drop
    // it and keep just the viewport/UA/touch emulation, which it does support.
    const device = { ...devices[deviceName] };
    if (browserName === "firefox") delete device.isMobile;
    const context = await browser.newContext(device);
    const page = await context.newPage();
    await page.goto("/player/?relay=1080test1");
    await expect(page.locator("#channel-label")).toHaveText("Relay: 1080test1");
    await expect(page.locator("#error")).not.toContainText("No channel specified");
    // The video element must be present and sized by the page's own CSS,
    // not clipped or hidden at phone width - a common mobile embed failure.
    const box = await page.locator("#video").boundingBox();
    expect(box).not.toBeNull();
    expect(box.width).toBeGreaterThan(0);
    await context.close();
  });
}
