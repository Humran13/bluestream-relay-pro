// @ts-check
const { test, expect } = require("@playwright/test");

// Cross-engine confirmation that web/player.js parses and recovers the
// reported duplicated-query-string shape identically in Chromium, Firefox,
// WebKit, and (where installed) real Chrome/Edge.
//
// IMPORTANT SCOPE NOTE: Playwright drives pages via CDP/page.goto() and
// cannot simulate a human pasting text into the browser's own address bar
// and pressing Enter - that omnibox autocomplete/paste-and-go path is not
// exposed to any browser automation protocol. So this suite verifies the
// END STATE (a page loaded with the duplicated query string the bug report
// showed) behaves correctly across real browser engines; it does NOT, and
// cannot, mechanically reproduce the omnibox step that produces that string
// in the first place. That omnibox behavior is cited as the likely cause
// based on the bug report (reproduces in Edge/Chrome/Chrome-Incognito, not
// in Brave) but is UNVERIFIED by automation - see player-url.spec.js
// comments and the final report for this distinction.

// Note: these tests serve web/ with no backing HLS/relay server, so hls.js
// will legitimately show its own "Network error - retrying..." box once it
// tries to fetch a nonexistent .m3u8 - that's correct app behavior, not the
// bug under test. What matters here is that the channel was recognized at
// all (never "No channel specified") and no uncaught JS exception occurred.

test("reported duplicated relay URL loads the correct channel, no parsing error", async ({ page }) => {
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.goto("/player/?relay=1080test1?relay=1080test1");
  await expect(page.locator("#channel-label")).toHaveText("Relay: 1080test1");
  await expect(page.locator("#error")).not.toContainText("No channel specified");
  expect(errors).toEqual([]);
});

test("address bar is canonicalized after loading the duplicated URL", async ({ page }) => {
  await page.goto("/player/?relay=1080test1?relay=1080test1");
  await expect(page.locator("#channel-label")).toHaveText("Relay: 1080test1");
  await expect.poll(() => new URL(page.url()).search).toBe("?relay=1080test1");
});

test("a normal, correctly-formed player URL is never rewritten", async ({ page }) => {
  await page.goto("/player/?relay=1080test1");
  await expect(page.locator("#channel-label")).toHaveText("Relay: 1080test1");
  expect(new URL(page.url()).search).toBe("?relay=1080test1");
});

test("duplicated playlist URL recovers the same way", async ({ page }) => {
  await page.goto("/player/?playlist=evening-promo?playlist=evening-promo");
  await expect(page.locator("#channel-label")).toHaveText("Playlist: evening-promo");
  await expect(page.locator("#error")).not.toContainText("No channel specified");
});

test("unrecognized malformed query (not a clean duplicate) fails honestly with an error, not a wrong channel", async ({
  page,
}) => {
  await page.goto("/player/?relay=foo?relay=bar");
  await expect(page.locator("#error")).toBeVisible();
  await expect(page.locator("#error")).toContainText("No channel specified");
});

test("refreshing the recovered URL is stable (idempotent)", async ({ page }) => {
  await page.goto("/player/?relay=1080test1?relay=1080test1");
  await expect(page.locator("#channel-label")).toHaveText("Relay: 1080test1");
  await page.reload();
  await expect(page.locator("#channel-label")).toHaveText("Relay: 1080test1");
  await expect(page.locator("#error")).not.toContainText("No channel specified");
});
