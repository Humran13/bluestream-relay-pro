// @ts-check
// Interactive Studio editor: real console app, real browser engines.
const { test, expect } = require("@playwright/test");

const BASE = "http://127.0.0.1:4874";
const PASSWORD = "correct-horse-battery-staple";
let seq = 0;

async function login(page) {
  await page.goto(BASE + "/console/login");
  await page.fill('input[name="username"]', "admin");
  await page.fill('input[name="password"]', PASSWORD);
  // NOTE: /console/login itself matches /console/..., so wait until we have LEFT the login page.
  await Promise.all([page.waitForURL((u) => !u.pathname.includes("/login")), page.click('button[type="submit"]')]);
}

/** Create a blank project and add the given overlay kinds via the real UI. */
async function project(page, kinds) {
  // Unique across PARALLEL workers/engines (a per-process counter + timestamp collided and the server rightly refused the duplicate).
  const name = "e2e-" + require("crypto").randomBytes(6).toString("hex") + "-" + seq++;
  await login(page);
  await page.goto(BASE + "/console/studio");
  await page.fill('input[name="name"]', name);
  await page.selectOption('select[name="source_kind"]', "media");
  await page.fill('input[name="source_ref"]', "loop.mp4");
  await page.selectOption('select[name="template"]', "blank");
  await Promise.all([page.waitForURL(new RegExp("/console/studio/" + name)), page.click("text=Create project")]);
  await ready(page);
  for (const k of kinds) {
    await Promise.all([page.waitForNavigation(), page.click('button[name="action"][value="add:' + k + '"]')]);
    await ready(page);
  }
  return name;
}

/** The editor script is deferred: wait until it has really initialised. */
async function ready(page) {
  await page.waitForFunction(() => !!window.studioEditor);
}

/** Overlay property panels are collapsed <details>; open one before using its fields. */
async function openLayer(page, i) {
  const d = page.locator('details.overlay-box[data-i="' + i + '"]');
  if (!(await d.evaluate((e) => e.open))) { await d.locator("summary").click(); }
}

/** Submit via a button and wait for the resulting page to be ready again. */
async function submit(page, selector) {
  await Promise.all([page.waitForNavigation(), page.click(selector)]);
  await ready(page);
}

const box = (page, i) => page.evaluate((n) => window.studioEditor.getBox(n), i);
const item = (page, i) => page.locator('.studio-item[data-i="' + i + '"]');

test.describe("Studio visual editor", () => {
  // WebKit on shared CI runners is slow at login + project creation; this is not a product timeout.
  test.describe.configure({ timeout: 90000 });
  test("drag moves a layer and stays inside the canvas", async ({ page }) => {
    await project(page, ["text"]);
    const before = await box(page, 0);
    const el = item(page, 0);
    const b = await el.boundingBox();
    await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2);
    await page.mouse.down();
    await page.mouse.move(b.x + b.width / 2 + 120, b.y + b.height / 2 + 60, { steps: 6 });
    await page.mouse.up();
    const after = await box(page, 0);
    expect(after.x).toBeGreaterThan(before.x);
    expect(after.y).toBeGreaterThan(before.y);
    // drag far outside: must clamp to the canvas
    const b2 = await el.boundingBox();
    await page.mouse.move(b2.x + b2.width / 2, b2.y + b2.height / 2);
    await page.mouse.down();
    await page.mouse.move(b2.x + 5000, b2.y + 5000, { steps: 4 });
    await page.mouse.up();
    const far = await box(page, 0);
    expect(far.x + far.w).toBeLessThanOrEqual(1.0001);
    expect(far.y + far.h).toBeLessThanOrEqual(1.0001);
    expect(far.x).toBeGreaterThanOrEqual(0);
    // the REAL form field was updated (what the server will validate and save)
    expect(parseFloat(await page.inputValue('input[name="x_0"]'))).toBeCloseTo(far.x, 3);
  });

  test("resize handle changes size, respects minimum and bounds", async ({ page }) => {
    await project(page, ["text"]);
    await item(page, 0).click({ position: { x: 5, y: 5 } });
    const before = await box(page, 0);
    const h = page.locator('.studio-item[data-i="0"] .h-se');
    const hb = await h.boundingBox();
    await page.mouse.move(hb.x + hb.width / 2, hb.y + hb.height / 2);
    await page.mouse.down();
    await page.mouse.move(hb.x + 90, hb.y + 40, { steps: 5 });
    await page.mouse.up();
    const grown = await box(page, 0);
    expect(grown.w).toBeGreaterThan(before.w);
    expect(grown.h).toBeGreaterThan(before.h);
    // shrink past zero: stays above the minimum edge
    const hb2 = await h.boundingBox();
    await page.mouse.move(hb2.x + hb2.width / 2, hb2.y + hb2.height / 2);
    await page.mouse.down();
    await page.mouse.move(hb2.x - 5000, hb2.y - 5000, { steps: 4 });
    await page.mouse.up();
    const tiny = await box(page, 0);
    expect(tiny.w).toBeGreaterThanOrEqual(0.02);
    expect(tiny.h).toBeGreaterThanOrEqual(0.02);
  });

  test("keyboard nudges and resizes; numeric field drives the canvas", async ({ page }) => {
    await project(page, ["text"]);
    await item(page, 0).focus();
    const b0 = await box(page, 0);
    await page.keyboard.press("ArrowRight");
    await page.keyboard.press("ArrowDown");
    const b1 = await box(page, 0);
    expect(b1.x).toBeCloseTo(b0.x + 0.005, 3);
    expect(b1.y).toBeCloseTo(b0.y + 0.005, 3);
    await page.keyboard.press("Alt+ArrowRight");
    expect((await box(page, 0)).w).toBeGreaterThan(b0.w);
    await openLayer(page, 0);
    await page.fill('input[name="x_0"]', "0.3");
    await expect(item(page, 0)).toHaveCSS("left", /./);
    const left = await item(page, 0).evaluate((e) => e.style.left);
    expect(parseFloat(left)).toBeCloseTo(30, 0);
  });

  test("visibility toggle hides the layer; save persists geometry across reload", async ({ page }) => {
    const name = await project(page, ["rect", "text"]);
    await openLayer(page, 1);
    await page.fill('input[name="x_1"]', "0.4");
    await page.fill('input[name="y_1"]', "0.25");
    await page.locator('input[name="visible_0"]').uncheck();
    await expect(item(page, 0)).toHaveClass(/studio-hidden/);
    await submit(page, 'button[form="studio-form"]');
    await page.goto(BASE + "/console/studio/" + name);
    await ready(page);
    await openLayer(page, 1);
    expect(await page.inputValue('input[name="x_1"]')).toBe("0.4");
    expect(await page.inputValue('input[name="y_1"]')).toBe("0.25");
    await expect(page.locator('input[name="visible_0"]')).not.toBeChecked();
    await expect(item(page, 0)).toHaveClass(/studio-hidden/);
  });

  test("layer ordering: move down swaps the stacking order after saving", async ({ page }) => {
    const name = await project(page, ["rect", "text"]);
    const kindsBefore = await page.locator("details.overlay-box summary").allInnerTexts();
    expect(kindsBefore[0]).toContain("rect");
    await submit(page, 'button[name="action"][value="down:0"]');
    await page.goto(BASE + "/console/studio/" + name);
    await ready(page);
    const kindsAfter = await page.locator("details.overlay-box summary").allInnerTexts();
    expect(kindsAfter[0]).toContain("text");
    expect(kindsAfter[1]).toContain("rect");
    // z-index follows list order: last layer is on top
    const z0 = await item(page, 0).evaluate((e) => +e.style.zIndex);
    const z1 = await item(page, 1).evaluate((e) => +e.style.zIndex);
    expect(z1).toBeGreaterThan(z0);
  });

  test("apply to live creates a revision and rollback loads it into the draft only", async ({ page }) => {
    const name = await project(page, ["text"]);
    await openLayer(page, 0);
    await page.fill('input[name="text_0"]', "first version");
    await submit(page, 'button[form="studio-form"]');
    await submit(page, "text=Apply to Live >> nth=-1");
    await expect(page.locator(".flash")).toContainText("Revision 1 applied");
    await openLayer(page, 0);
    await page.fill('input[name="text_0"]', "second version");
    await submit(page, 'button[form="studio-form"]');
    await submit(page, "text=Apply to Live >> nth=-1");
    await expect(page.locator(".flash")).toContainText("Revision 2 applied");
    await page.selectOption("#rev", "1");
    await submit(page, "text=Load into draft");
    await page.goto(BASE + "/console/studio/" + name);
    await ready(page);
    await openLayer(page, 0);
    expect(await page.inputValue('input[name="text_0"]')).toBe("first version");
    await expect(page.locator("text=UNPUBLISHED CHANGES")).toBeVisible();
  });

  test("data overlay exposes URL, JSON path, refresh, format and text fields", async ({ page }) => {
    await project(page, ["data"]);
    await openLayer(page, 0);
    for (const n of ["url_0", "json_path_0", "refresh_s_0", "value_format_0", "text_0"]) {
      await expect(page.locator('[name="' + n + '"]')).toBeVisible();
    }
    await page.fill('input[name="url_0"]', "https://example.com/stats.json");
    await page.fill('input[name="json_path_0"]', "channel.subs");
    await page.fill('input[name="refresh_s_0"]', "120");
    await page.selectOption('select[name="value_format_0"]', "thousands");
    await submit(page, 'button[form="studio-form"]');
    await expect(page.locator(".flash")).toContainText("Draft saved");
    expect(await page.inputValue('input[name="json_path_0"]')).toBe("channel.subs");
    expect(await page.inputValue('input[name="refresh_s_0"]')).toBe("120");
    expect(await page.inputValue('select[name="value_format_0"]')).toBe("thousands");
  });

  test("invalid geometry is rejected by the server and not saved", async ({ page }) => {
    await project(page, ["rect"]);
    await openLayer(page, 0);
    await page.fill('input[name="x_0"]', "5");
    await submit(page, 'button[form="studio-form"]');
    await expect(page.locator(".flash")).toContainText("Not saved");
  });

  test("mobile viewport: no horizontal scroll and the canvas fits", async ({ browser }) => {
    test.setTimeout(120000);   // WebKit on CI is slow to log in and create a project
    const ctx = await browser.newContext({ viewport: { width: 390, height: 844 } });
    const page = await ctx.newPage();
    await project(page, ["rect", "ticker"]);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow).toBeLessThanOrEqual(1);
    const cb = await page.locator("#studio-canvas").boundingBox();
    expect(cb.width).toBeLessThanOrEqual(390);
    expect(Math.abs(cb.width / cb.height - 16 / 9)).toBeLessThan(0.05);
    await ctx.close();
  });
});
