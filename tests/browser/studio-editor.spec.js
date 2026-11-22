// @ts-check
// BlueStream Studio workspace: real console app, real browser engines. The fake engine bridge is stateful
// (uploads land in the library, streams start and stop) so every control is exercised end to end.
const { test, expect } = require("@playwright/test");
const zlib = require("zlib");
const crypto = require("crypto");

const BASE = "http://127.0.0.1:4874";
const PASSWORD = "correct-horse-battery-staple";
let seq = 0;
const uid = () => crypto.randomBytes(5).toString("hex") + "-" + seq++;

function mkpng(w, h) {
  const raw = Buffer.concat(Array.from({ length: h }, () => Buffer.concat([Buffer.from([0]), Buffer.alloc(w * 3, 180)])));
  const crc = (buf) => { let c, t = []; for (let n = 0; n < 256; n++) { c = n; for (let k = 0; k < 8; k++) { c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1; } t[n] = c >>> 0; }
    let x = 0xffffffff; for (const b of buf) { x = t[(x ^ b) & 0xff] ^ (x >>> 8); } return (x ^ 0xffffffff) >>> 0; };
  const chunk = (type, data) => { const len = Buffer.alloc(4); len.writeUInt32BE(data.length); const td = Buffer.concat([Buffer.from(type), data]); const c = Buffer.alloc(4); c.writeUInt32BE(crc(td)); return Buffer.concat([len, td, c]); };
  const ihdr = Buffer.alloc(13); ihdr.writeUInt32BE(w, 0); ihdr.writeUInt32BE(h, 4); ihdr[8] = 8; ihdr[9] = 2;
  return Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), chunk("IHDR", ihdr), chunk("IDAT", zlib.deflateSync(raw)), chunk("IEND", Buffer.alloc(0))]);
}

async function login(page) {
  await page.goto(BASE + "/console/login");
  await page.fill('input[name="username"]', "admin");
  await page.fill('input[name="password"]', PASSWORD);
  await Promise.all([page.waitForURL((u) => !u.pathname.includes("/login")), page.click('button[type="submit"]')]);
}

/** Create a project through the real New-project form, choosing the media with the visual picker (no typing). */
async function project(page, template = "blank") {
  const name = "ws-" + uid();
  await login(page);
  await page.goto(BASE + "/console/studio");
  await page.fill('input[name="name"]', name);
  await page.selectOption('select[name="source_kind"]', "media");
  await page.click("#sr-browse");
  await page.locator(".sdlg .smb-card").first().waitFor();
  await page.locator('.sdlg .smb-card[data-name="loop.mp4"]').click();
  await page.click(".sdlg-foot .btn:not(.btn-secondary)");
  await expect(page.locator("#sr")).toHaveValue("loop.mp4");
  await page.selectOption('select[name="template"]', template);
  await Promise.all([page.waitForURL(new RegExp("/console/studio/" + name)), page.click("text=Create project")]);
  await page.waitForFunction(() => !!window.studioWorkspace);
  return name;
}

const addSource = async (page, label) => { await page.click("#ws-add"); await page.getByRole("menuitem", { name: label, exact: true }).click(); };
const layerCount = (page) => page.locator("#ws-layers .lrow:not(.base)").count();
const box = (page, i) => page.evaluate((n) => window.studioWorkspace.box(n), i);
const layer = (page, i) => page.locator('#ws-canvas .ly[data-i="' + i + '"]');
const prop = (page, key) => page.locator('#ws-props [data-key="' + key + '"]');
/** Row actions only show on hover/selection, like OBS: hover first, then press. */
const rowAct = async (page, text, label) => { const row = page.locator("#ws-layers .lrow", { hasText: text }).first(); await row.hover(); await row.locator('[aria-label="' + label + '"]').click(); };
const saveDraft = async (page) => { await page.click("#ws-save"); await expect(page.locator("#ws-dirty")).toBeHidden(); };

test.describe("Studio workspace", () => {
  test.describe.configure({ timeout: 90000 });

  test("layout: toolbar, media, canvas, properties, layers, stream and controls", async ({ page }) => {
    await project(page);
    for (const sel of ["#ws-save", "#ws-apply", "#ws-settings", "#ws-media", "#ws-canvas", "#ws-props", "#ws-layers", "#ws-status", "#ws-ctl", "#ws-add", "#ws-monbar"]) {
      await expect(page.locator(sel).first(), sel).toBeVisible();
    }
    await expect(page.locator(".ws-brand")).toContainText("BlueStream Studio");
    const cb = await page.locator("#ws-canvas").boundingBox();
    expect(Math.abs(cb.width / cb.height - 16 / 9)).toBeLessThan(0.03);
    const wide = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
    expect(wide).toBe(false);
    await expect(page.locator("#ws-ctl")).toContainText("Create Stream");
    await expect(page.locator("#ws-status")).toContainText("x264");
  });

  test("Add Source menu offers only source types that really work", async ({ page }) => {
    await project(page);
    await page.click("#ws-add");
    const items = await page.locator("#ws-menu .mi").allInnerTexts();
    for (const want of ["Image", "Text", "Clock", "Countdown", "Countup", "Ticker", "Colour block", "Existing BlueStream stream", "Existing playlist", "Audio file (with background)", "Image slideshow (background)"]) {
      expect(items.join("|")).toContain(want);
    }
    const all = items.join("|").toLowerCase();
    for (const fake of ["screen", "window capture", "game", "camera", "capture device", "display"]) { expect(all).not.toContain(fake); }
    expect(all).toContain("browser source (not available)");   // visible, but honest: it needs the isolated renderer enabled on the server
    await page.getByRole("menuitem", { name: /Browser source/ }).click({ force: true });
    await expect(page.locator(".toast.error")).toContainText("Browser source is not available");
    await page.click("#ws-add");
    await expect(page.locator("#ws-menu .mnote")).toContainText("not offered");
    await page.keyboard.press("Escape");
    await expect(page.locator("#ws-menu")).toBeHidden();
  });

  test("text layer: properties drive the canvas; Save Draft clears the unsaved flag and persists", async ({ page }) => {
    const name = await project(page);
    await addSource(page, "Text");
    await expect(page.locator("#ws-dirty")).toBeVisible();
    expect(await layerCount(page)).toBe(1);
    await prop(page, "text").fill("Welcome to the show");
    await expect(layer(page, 0)).toContainText("Welcome to the show");
    await prop(page, "x").fill("30");
    expect((await box(page, 0)).x).toBeCloseTo(0.3, 2);
    await prop(page, "opacity").fill("0.5");
    await expect(layer(page, 0)).toHaveCSS("opacity", "0.5");
    await saveDraft(page);
    await page.goto(BASE + "/console/studio/" + name);
    await page.waitForFunction(() => !!window.studioWorkspace);
    await expect(layer(page, 0)).toContainText("Welcome to the show");
    expect((await box(page, 0)).x).toBeCloseTo(0.3, 2);
    await expect(page.locator("#ws-dirty")).toBeHidden();
  });

  test("drag moves, handles resize, arrow keys nudge; layers stay inside the canvas", async ({ page }) => {
    await project(page);
    await addSource(page, "Text");
    const before = await box(page, 0), b = await layer(page, 0).boundingBox();
    await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2);
    await page.mouse.down();
    await page.mouse.move(b.x + b.width / 2 + 150, b.y + b.height / 2 + 80, { steps: 6 });
    await page.mouse.up();
    const after = await box(page, 0);
    expect(after.x).toBeGreaterThan(before.x); expect(after.y).toBeGreaterThan(before.y);
    // far outside the canvas: clamped
    const b2 = await layer(page, 0).boundingBox();
    await page.mouse.move(b2.x + b2.width / 2, b2.y + b2.height / 2);
    const cb = await page.locator("#ws-canvas").boundingBox(), vp = page.viewportSize();
    const tx = Math.min(cb.x + cb.width + 40, vp.width - 2), ty = Math.min(cb.y + cb.height + 40, vp.height - 2);   // overshoot the corner, but stay inside the window
    await page.mouse.down(); await page.mouse.move(tx, ty, { steps: 8 }); await page.mouse.up();
    const far = await box(page, 0);
    expect(far.x + far.w).toBeLessThanOrEqual(1.0001); expect(far.y + far.h).toBeLessThanOrEqual(1.0001);
    expect(far.x).toBeGreaterThan(after.x);
    // resize with the south-east handle
    const w0 = (await box(page, 0)).w;
    const hd = await page.locator('#ws-canvas .ly[data-i="0"] .hnd-nw').boundingBox();
    await page.mouse.move(hd.x + hd.width / 2, hd.y + hd.height / 2); await page.mouse.down();
    await page.mouse.move(hd.x + hd.width / 2 + 60, hd.y + hd.height / 2 + 20, { steps: 5 }); await page.mouse.up();
    expect((await box(page, 0)).w).toBeLessThan(w0);
    // keyboard
    await layer(page, 0).focus();
    const k0 = await box(page, 0);
    await page.keyboard.press("ArrowLeft"); await page.keyboard.press("Shift+ArrowUp");
    const k1 = await box(page, 0);
    expect(k1.x).toBeLessThan(k0.x); expect(k1.y).toBeLessThan(k0.y);
  });

  test("media browser lists the library with sizes and formats, searches and filters", async ({ page }) => {
    await project(page);
    const cards = page.locator("#ws-media .smb-card");
    await expect(cards.first()).toBeVisible();
    await expect(page.locator('#ws-media .smb-card[data-name="logo.png"]')).toContainText("PNG");
    await expect(page.locator('#ws-media .smb-card[data-name="loop.mp4"]')).toContainText("5.0 MB");
    await page.locator("#ws-media .smb-search").fill("badge");
    await expect(cards).toHaveCount(1);
    await page.locator("#ws-media .smb-search").fill("");
    await page.locator("#ws-media .smb-chip", { hasText: "Audio" }).click();
    await expect(cards).toHaveCount(1);
    await expect(cards.first()).toHaveAttribute("data-name", "song.mp3");
    await page.locator("#ws-media .smb-chip", { hasText: "Image" }).click();
    await expect(page.locator('#ws-media .smb-card[data-kind="video"]')).toHaveCount(0);
    await page.locator("#ws-media .smb-search").fill("zzzz-nothing");
    await expect(page.locator("#ws-media .smb-status")).toContainText("No media matches");
  });

  test("upload an image from the browser: progress, new library entry, real preview on the canvas", async ({ page }) => {
    await project(page);
    const fname = "Upload " + uid() + ".png";
    await page.setInputFiles("#ws-media input[type=file]", { name: fname, mimeType: "image/png", buffer: mkpng(40, 10) });
    const safe = fname.toLowerCase().replace(/ /g, "-");
    const card = page.locator('#ws-media .smb-card[data-name="' + safe + '"]');
    await expect(card).toBeVisible({ timeout: 15000 });
    await expect(card).toHaveAttribute("aria-selected", "true");
    await page.click("text=Add as image layer");
    const img = layer(page, 0).locator("img.ly-img");
    await expect(img).toBeVisible();
    await expect.poll(() => img.evaluate((e) => /** @type {HTMLImageElement} */ (e).naturalWidth)).toBeGreaterThan(0);
    // box shape follows the picture: 4:1 image on a 16:9 canvas
    await expect.poll(async () => { const b = await box(page, 0); return Math.abs(b.h - b.w * (16 / 9) * (10 / 40)) < 0.003; }).toBe(true);
  });

  test("unsupported and oversized uploads show clear errors and add nothing", async ({ page }) => {
    await project(page);
    await page.setInputFiles("#ws-media input[type=file]", { name: "virus.exe", mimeType: "application/octet-stream", buffer: Buffer.from("MZ") });
    await expect(page.locator("#ws-media .smb-up.err")).toContainText("not a supported");
    await page.setInputFiles("#ws-media input[type=file]", { name: "huge.png", mimeType: "image/png", buffer: Buffer.alloc(11 * 1024 * 1024, 1) });
    await expect(page.locator("#ws-media .smb-up.err").nth(1)).toContainText("larger than 10 MB");
    await expect(page.locator('#ws-media .smb-card[data-name="huge.png"], #ws-media .smb-card[data-name="virus.exe"]')).toHaveCount(0);
  });

  test("several image and video overlays can be added from the media panel", async ({ page }) => {
    await project(page);
    await page.locator('#ws-media .smb-card[data-name="logo.png"]').click(); await page.click("text=Add as image layer");
    await page.locator('#ws-media .smb-card[data-name="badge.png"]').click(); await page.click("text=Add as image layer");
    await page.locator('#ws-media .smb-card[data-name="loop.mp4"]').click(); await page.click("text=Add as video overlay");
    expect(await layerCount(page)).toBe(3);
    const kinds = await page.evaluate(() => window.studioWorkspace.project.overlays.map((o) => o.kind + ":" + o.asset));
    expect(kinds).toEqual(["image:logo.png", "image:badge.png", "video:loop.mp4"]);
    // the two images show real pictures with their own shapes (2:1 and 1:1)
    await expect.poll(async () => (await box(page, 0)).h / (await box(page, 0)).w).toBeCloseTo(16 / 9 / 2, 1);
    await expect.poll(async () => (await box(page, 1)).h / (await box(page, 1)).w).toBeCloseTo(16 / 9, 1);
  });

  test("the layer's Media Library picker replaces the picture and re-fits the box", async ({ page }) => {
    await project(page);
    await page.locator('#ws-media .smb-card[data-name="logo.png"]').click(); await page.click("text=Add as image layer");
    await page.click("#ws-props >> text=Choose from Media Library");
    await page.locator('.sdlg .smb-card[data-name="banner.png"]').click();
    await page.click(".sdlg-foot .btn:not(.btn-secondary)");
    await expect(page.locator("#ws-props .mname")).toHaveText("banner.png");
    await expect.poll(async () => { const b = await box(page, 0); return b.h / b.w; }).toBeCloseTo(16 / 9 / 4, 1);
    await expect(layer(page, 0).locator("img.ly-img")).toHaveAttribute("src", /banner\.png/);
  });

  test("layer list: hide, reorder, duplicate and remove", async ({ page }) => {
    await project(page);
    await addSource(page, "Text"); await addSource(page, "Clock"); await addSource(page, "Ticker");
    expect(await layerCount(page)).toBe(3);
    const names = () => page.locator("#ws-layers .lrow:not(.base) .lname").allInnerTexts();
    expect(await names()).toEqual(["Ticker", "Clock", "Text"]);                         // front to back
    await rowAct(page, "Clock", "Move layer up (in front)");
    expect(await names()).toEqual(["Clock", "Ticker", "Text"]);
    await page.locator("#ws-layers .lrow", { hasText: "Text" }).locator('[aria-label="Hide layer"]').click();
    await expect(layer(page, 0)).toHaveClass(/hidden/);
    await rowAct(page, "Clock", "Duplicate layer");
    expect(await layerCount(page)).toBe(4);
    await rowAct(page, "Clock copy", "Remove layer");
    expect(await layerCount(page)).toBe(3);
    await saveDraft(page);
    const saved = await page.evaluate(() => window.studioWorkspace.project.overlays.map((o) => o.kind + ":" + o.visible));
    expect(saved).toEqual(["text:false", "ticker:true", "clock:true"]);
  });

  test("Ctrl+S saves and a server-side rejection keeps the unsaved changes with a clear message", async ({ page }) => {
    await project(page);
    await addSource(page, "Text");
    await page.keyboard.press("Control+s");
    await expect(page.locator("#ws-dirty")).toBeHidden();
    await prop(page, "size").fill("9999");
    await expect(page.locator("#ws-dirty")).toBeVisible();
    await page.click("#ws-save");
    await expect(page.locator(".toast.error")).toContainText("font size");
    await expect(page.locator("#ws-dirty")).toBeVisible();
  });

  test("settings: tabs, persisted preferences, validated output settings", async ({ page }) => {
    const name = await project(page);
    await page.click("#ws-settings");
    const tabs = await page.locator(".sset-tab").allInnerTexts();
    expect(tabs).toEqual(["General", "Stream", "Output", "Audio", "Video", "Hotkeys", "Advanced"]);
    await page.getByLabel("Snap sensitivity").fill("20");
    await page.locator(".sset-pane select").first().selectOption("light");
    await page.getByLabel("Draw safe areas").check();
    await page.locator(".sset-tab", { hasText: "Output" }).click();
    await expect(page.locator(".sset-pane")).toContainText("NVIDIA H.264 (h264_nvenc)");
    await expect(page.locator('.sset-pane option[value="h264_nvenc"]')).toHaveAttribute("disabled", "");
    await page.getByLabel("Video bitrate (kbps)").fill("2500");
    await page.locator(".sset-tab", { hasText: "Audio" }).click();
    await page.locator(".sset-pane select").selectOption("160");
    await page.locator(".sset-tab", { hasText: "Video" }).click();
    await page.locator(".sset-pane select").nth(1).selectOption("60");
    await expect(page.locator(".sset-pane .cap")).toContainText("CPU cores");
    await page.locator(".sset-pane select").nth(1).selectOption("30");
    await page.locator(".sset-pane select").nth(2).selectOption("lanczos");
    await page.locator(".sdlg-foot .btn", { hasText: /^OK$/ }).click();
    await expect(page.locator(".toast.success", { hasText: "Settings saved" })).toBeVisible();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
    await expect(page.locator("#ws-dirty")).toBeVisible();                                 // output settings are part of the draft
    await saveDraft(page);
    await page.goto(BASE + "/console/studio/" + name);
    await page.waitForFunction(() => !!window.studioWorkspace);
    const p = await page.evaluate(() => window.studioWorkspace.project.profile);
    expect([p.video_kbps, p.audio_kbps, p.fps, p.scaler]).toEqual([2500, 160, 30, "lanczos"]);
    await expect(page.locator("html")).toHaveAttribute("data-theme", "light");             // server-side preference
    await page.click("#ws-settings");
    await expect(page.getByLabel("Snap sensitivity")).toHaveValue("20");
    await expect(page.locator("#ws-canvas")).toHaveClass(/safe/);
    await page.locator(".sdlg-foot .btn", { hasText: "Cancel" }).click();
    // leave the shared test server in its default state for the other tests
    const defaults = await page.evaluate(async () => (await (await fetch("/console/studio/settings.json")).json()).defaults);
    const csrf = await page.evaluate(() => document.getElementById("ws").dataset.csrf);
    const r = await page.evaluate(async ([d, t]) => (await fetch("/console/studio/settings.json", { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": t }, body: JSON.stringify({ settings: d }) })).status, [defaults, csrf]);
    expect(r).toBe(200);
  });

  test("hotkeys can be rebound and duplicates are rejected by the server", async ({ page }) => {
    await project(page);
    await page.click("#ws-settings");
    await page.locator(".sset-tab", { hasText: "Hotkeys" }).click();
    const save = page.getByLabel("Save draft shortcut");
    await save.click(); await page.keyboard.press("Control+Shift+S");
    await expect(save).toHaveValue("Ctrl+Shift+S");
    const dup = page.getByLabel("Duplicate layer shortcut");
    await dup.click(); await page.keyboard.press("Control+Shift+S");                        // same combo as "save"
    await page.locator(".sdlg-foot .btn", { hasText: /^OK$/ }).click();
    await expect(page.locator(".toast.error")).toContainText("used for both");
  });

  test("stream controls: create, attach a destination, start and stop with confirmation", async ({ page }) => {
    await project(page, "ticker");
    await page.click("#ws-ctl >> text=Create Stream");
    await expect(page.locator("#ws-ctl")).toContainText("Start Streaming");
    await page.click("#ws-settings");
    await page.locator(".sset-tab", { hasText: "Stream" }).click();
    const dest = page.locator(".dest", { hasText: "YouTube main" });
    await expect(dest).toContainText("youtube"); await expect(dest).toContainText("key saved");
    await expect(page.locator(".sset-pane")).not.toContainText("stream_key");
    await dest.locator("input").check();
    await page.locator(".sdlg-foot .btn", { hasText: /^OK$/ }).click();
    await expect(page.locator(".toast.success", { hasText: "Destinations saved" })).toBeVisible();
    await page.click("#ws-start");
    await expect(page.locator("#ws-ctl")).toContainText("Stop Streaming");
    await expect(page.locator("#ws-status")).toContainText("HEALTHY");
    await expect(page.locator("#ws-status")).toContainText("1.01x");
    await expect(page.locator("#ws-status .pub")).toContainText("LIVE");
    await page.click("#ws-stop");
    await page.locator(".sdlg .btn", { hasText: "Cancel" }).click();
    await expect(page.locator("#ws-ctl")).toContainText("Stop Streaming");                   // cancelled: still running
    await page.click("#ws-stop");
    await page.locator(".sdlg .btn", { hasText: "Stop streaming" }).click();
    await expect(page.locator("#ws-ctl")).toContainText("Start Streaming");
  });

  test("one editable canvas: no Preview/Program switch; the state chain tells draft, applied, pipeline and output apart", async ({ page }) => {
    await project(page);
    await expect(page.locator('[data-view="program"]')).toHaveCount(0);
    await addSource(page, "Text");
    await prop(page, "text").fill("LIVE LAYER");
    await expect(page.locator("#ws-status .chain")).toContainText("has unsaved edits");
    await page.click("#ws-apply");                                                         // saves the draft first, then applies
    await expect(page.locator(".toast.success", { hasText: "Revision 1 applied" })).toBeVisible();
    const chain = page.locator("#ws-status .chain");
    await expect(chain).toContainText("saved");
    await expect(chain).toContainText("revision 1");
    await expect(chain).toContainText("no Studio stream yet");                             // applied is NOT on air: said plainly
    await expect(page.locator('[data-mon="output"]')).toBeDisabled();
    await addSource(page, "Clock");                                                         // a draft change after applying
    await page.click("#ws-save");
    await expect(page.locator("#ws-status .chain")).toContainText("your draft differs");
  });

  test("stream running: content-only changes apply LIVE with no restart; structural changes ask first and verify the new revision", async ({ page }) => {
    await project(page, "ticker");
    await page.click("#ws-ctl >> text=Create Stream");
    await page.click("#ws-start");
    await expect(page.locator("#ws-status .chain")).toContainText("running revision 1");
    await expect(page.locator("#ws-status .chain")).toContainText("live-composition");
    // content only (ticker text): hot
    await page.locator("#ws-layers .lrow", { hasText: "Ticker" }).locator(".lname").click();
    await prop(page, "text").fill("Breaking news");
    await page.click("#ws-apply");
    await expect(page.locator(".toast.success", { hasText: "no interruption" })).toBeVisible();
    await expect(page.locator(".sdlg")).toHaveCount(0);                                     // no confirmation: nothing is interrupted
    // structural (a new layer): explicit confirmation, then the new revision must come up
    await addSource(page, "Clock");
    await page.click("#ws-apply");
    await expect(page.locator(".sdlg")).toContainText("restarts this Studio stream");
    await page.locator(".sdlg .btn", { hasText: "Apply and restart" }).click();
    await expect(page.locator(".toast.success", { hasText: /is running \(live-composition\)/ })).toBeVisible({ timeout: 20000 });
  });

  test("a restart whose new pipeline never becomes healthy is rolled back automatically to the previous layout", async ({ page }) => {
    await page.addInitScript(() => { window.__bsVerifyTries = 3; window.__bsVerifyMs = 400; });
    const name = "ws-fail-" + uid();
    await login(page);
    await page.goto(BASE + "/console/studio");
    await page.fill('input[name="name"]', name);
    await page.selectOption('select[name="source_kind"]', "media");
    await page.fill("#sr", "loop.mp4");
    await page.selectOption('select[name="template"]', "ticker");
    await Promise.all([page.waitForURL(new RegExp("/console/studio/" + name)), page.click("text=Create project")]);
    await page.waitForFunction(() => !!window.studioWorkspace);
    await page.click("#ws-ctl >> text=Create Stream");
    await page.click("#ws-start");
    await addSource(page, "Clock");
    await page.click("#ws-apply");
    await page.locator(".sdlg .btn", { hasText: "Apply and restart" }).click();
    await expect(page.locator(".toast.error", { hasText: "put back on air automatically" })).toBeVisible({ timeout: 20000 });
  });

  test("background source can be chosen from the Media Library and existing streams/playlists are offered honestly", async ({ page }) => {
    await project(page);
    await addSource(page, "Image slideshow (background)");
    await page.locator(".sdlg .btn", { hasText: "Replace" }).click();                        // a background already exists: confirm first
    await page.locator('.sdlg .smb-card[data-name="logo.png"]').click();
    await page.locator('.sdlg .smb-card[data-name="badge.png"]').click();
    await page.click(".sdlg-foot .btn:not(.btn-secondary)");
    const src = await page.evaluate(() => [window.studioWorkspace.project.source_kind, window.studioWorkspace.project.source_ref]);
    expect(src[0]).toBe("slideshow"); expect(src[1].split(",").sort()).toEqual(["badge.png", "logo.png"]);
    await expect(page.locator("#ws-props")).toContainText("Seconds per image");
    await addSource(page, "Existing BlueStream stream");
    await expect(page.locator(".sdlg")).toContainText("Use an existing stream");              // choose from the REAL list (dino, sleepy, ghost)
    await page.locator(".sdlg select").selectOption("dino");
    await page.locator(".sdlg .btn", { hasText: "Use this" }).click();
    const src2 = await page.evaluate(() => [window.studioWorkspace.project.source_kind, window.studioWorkspace.project.source_ref]);
    expect(src2).toEqual(["relay", "dino"]);
  });

  test("mobile viewport: one column, no horizontal scroll, canvas fits", async ({ browser }) => {
    const ctx = await browser.newContext({ viewport: { width: 390, height: 800 } });
    const page = await ctx.newPage();
    await project(page, "ticker");
    expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1)).toBe(false);
    const cb = await page.locator("#ws-canvas").boundingBox();
    expect(cb.width).toBeLessThanOrEqual(390);
    await expect(page.locator("#ws-layers")).toBeVisible();
    await ctx.close();
  });

  // ---- real video in the canvas (H.264 playback needs a browser with proprietary codecs: Edge/Chrome/Safari/Firefox on a normal OS) ----
  const canH264 = (page) => page.evaluate(() => !!document.createElement("video").canPlayType('video/mp4; codecs="avc1.42E01E"') && !!(window.MediaSource && MediaSource.isTypeSupported('video/mp4; codecs="avc1.42E01E"')));
  /** Mean absolute difference between two frames of the canvas <video> (drawn to a scratch canvas): > 0 means real motion. */
  const motion = (page) => page.evaluate(async () => {
    const v = document.querySelector("video.ws-video"), c = document.createElement("canvas"); c.width = 160; c.height = 90;
    const g = c.getContext("2d"), grab = () => { g.drawImage(v, 0, 0, 160, 90); return g.getImageData(0, 0, 160, 90).data; };
    const a = grab(); await new Promise((r) => setTimeout(r, 700)); const b = grab(); let d = 0; for (let i = 0; i < a.length; i += 4) { d += Math.abs(a[i] - b[i]); }
    return d / (a.length / 4);
  });

  test("Mode A: a Media Library video PLAYS in the canvas (real motion, not a still), with local-only controls and a loop that continues", async ({ page }) => {
    await project(page);
    test.skip(!(await canH264(page)), "this browser build cannot decode H.264 (UNTESTED here, not passed)");
    await page.locator('#ws-media .smb-card[data-name="motion.mp4"]').click();
    await page.click("text=Use as background");
    const v = page.locator("video.ws-video");
    await expect(v).toBeVisible();
    await expect.poll(() => v.evaluate((e) => e.readyState >= 2 && e.currentTime > 0.3), { timeout: 20000 }).toBe(true);
    await expect(page.locator(".ws-vstate")).toContainText("Playing");
    expect(await motion(page)).toBeGreaterThan(2);                                          // frames differ: it is moving
    expect(await v.evaluate((e) => [e.videoWidth, e.videoHeight])).toEqual([320, 180]);
    // overlays sit on top of the playing video
    await addSource(page, "Text");
    expect(await page.evaluate(() => document.elementFromPoint(...(() => { const r = document.querySelector("#ws-canvas .ly").getBoundingClientRect(); return [r.x + r.width / 2, r.y + r.height / 2]; })()).closest(".ly") !== null)).toBe(true);
    // local controls never reach the server's stream endpoints
    const writes = []; page.on("request", (r) => { if (r.method() === "POST" && /stream\.json|\/relays\/|publish/.test(r.url())) { writes.push(r.url()); } });
    await page.click("#ws-vbar >> text=Pause");
    await expect.poll(() => v.evaluate((e) => e.paused)).toBe(true);
    await page.click("#ws-vbar >> text=Play");
    await expect.poll(() => v.evaluate((e) => !e.paused)).toBe(true);
    await page.click("#ws-vbar >> text=Preview muted");
    await expect.poll(() => v.evaluate((e) => e.muted)).toBe(false);
    expect(writes).toEqual([]);
    // loop: the 6 s clip ends and playback continues from the start
    await expect.poll(() => v.evaluate((e) => e.currentTime), { timeout: 15000 }).toBeGreaterThan(3);
    await expect.poll(() => v.evaluate((e) => e.currentTime < 2 && !e.paused && e.readyState >= 2), { timeout: 20000 }).toBe(true);
    await page.click("#ws-vbar >> text=Loop: on");
    await expect(page.locator("#ws-vbar")).toContainText("Loop: off");
  });

  test("Mode C: an existing running relay is monitored live and read-only; stopped and unreachable ones say so", async ({ page }) => {
    await project(page);
    const writes = []; page.on("request", (r) => { if (r.method() !== "GET" && !/save\.json|preview-text|status/.test(r.url())) { writes.push(r.method() + " " + r.url()); } });
    await page.click("#tab-sources");
    await expect(page.locator("#ws-sources .srcrow", { hasText: "dino" })).toContainText("RUNNING");
    await expect(page.locator("#ws-sources .srcrow", { hasText: "sleepy" })).toContainText("STOPPED");
    await expect(page.locator("#ws-sources")).toContainText("Watching a stream is read-only");
    // stopped relay: clear message, no pretend picture
    await page.locator("#ws-sources .srcrow", { hasText: "sleepy" }).getByRole("button").click();
    await expect(page.locator(".ws-vstate")).toContainText("stopped");
    await expect(page.locator("video.ws-video")).toBeHidden();
    // unreachable (running but its HLS is gone): disconnected + retry, never a fake still
    await page.locator("#ws-sources .srcrow", { hasText: "ghost" }).getByRole("button").click();
    test.skip(!(await canH264(page)), "H.264/MSE unavailable: the playing-stream part is UNTESTED in this browser");
    await expect(page.locator(".ws-vstate")).toContainText(/Disconnected|not reachable/, { timeout: 20000 });
    await expect(page.locator("#ws-vbar >> text=Retry")).toBeVisible();
    // running relay: its real HLS plays
    await page.locator("#ws-sources .srcrow", { hasText: "dino" }).getByRole("button").click();
    const v = page.locator("video.ws-video");
    await expect.poll(() => v.evaluate((e) => e.readyState >= 2 && e.currentTime > 0.3), { timeout: 25000 }).toBe(true);
    await expect(page.locator(".ws-vstate")).toContainText("LIVE");
    await expect(page.locator(".ws-vstate")).toContainText("behind");
    expect(await motion(page)).toBeGreaterThan(2);
    await expect(page.locator("#ws-props")).toContainText("Studio is only WATCHING it");
    expect(writes).toEqual([]);                                                              // monitoring changed nothing anywhere
  });

  test("the Studio stream's actual output can be watched (Live output) once it runs", async ({ page }) => {
    await project(page, "ticker");
    await expect(page.locator('[data-mon="output"]')).toBeDisabled();
    await page.click("#ws-ctl >> text=Create Stream");
    await page.click("#ws-start");
    await expect(page.locator('[data-mon="output"]')).toBeEnabled({ timeout: 15000 });
    await page.click('[data-mon="output"]');
    await expect(page.locator("#ws-monnote")).toContainText("burned into this video");
    await expect(page.locator("#ws-canvas .ly")).toHaveCount(0);                             // no double drawing: the overlays are IN the video
    test.skip(!(await canH264(page)), "H.264/MSE unavailable: playback of the output is UNTESTED in this browser");
    await expect.poll(() => page.locator("video.ws-video").evaluate((e) => e.readyState >= 2 && e.currentTime > 0.3), { timeout: 25000 }).toBe(true);
    await expect(page.locator(".ws-vstate")).toContainText("Studio output");
    await page.click('[data-mon="source"]');
    expect(await page.locator("#ws-canvas .ly").count()).toBeGreaterThan(0);
  });

  test("layers: double-click renames, lock stops dragging and disables position fields", async ({ page }) => {
    await project(page);
    await addSource(page, "Text");
    const row0 = page.locator("#ws-layers .lrow:not(.base)").first();
    await row0.hover(); await row0.locator('[aria-label="Rename layer"]').click();
    await page.locator("#ws-layers .lrename").fill("Channel title");
    await page.keyboard.press("Enter");
    await expect(page.locator("#ws-layers .lrow:not(.base) .lname")).toHaveText("Channel title");
    await page.locator("#ws-layers .lrow:not(.base)").locator('[aria-label="Lock layer"]').click();
    const before = await box(page, 0), b = await layer(page, 0).boundingBox();
    await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2); await page.mouse.down();
    await page.mouse.move(b.x + b.width / 2 + 120, b.y + b.height / 2 + 60, { steps: 5 }); await page.mouse.up();
    expect(await box(page, 0)).toEqual(before);                                              // locked: did not move
    await expect(prop(page, "x")).toBeDisabled();
    await expect(page.locator("#ws-props")).toContainText("locked");
    await saveDraft(page);
    const kept = await page.evaluate(() => [window.studioWorkspace.project.overlays[0].label, window.studioWorkspace.project.overlays[0].locked]);
    expect(kept).toEqual(["Channel title", true]);
    await page.locator("#ws-layers .lrow:not(.base)").locator('[aria-label="Unlock layer"]').click();
    await expect(prop(page, "x")).toBeEnabled();
  });

  test("text properties: font, alignment and background box persist; countdown can be set to a duration from now", async ({ page }) => {
    const name = await project(page);
    await addSource(page, "Text");
    await page.locator('#ws-props [aria-label="Text alignment"] >> text=Center').click();
    await prop(page, "bg_opacity").fill("0.6");
    await expect(page.locator("#ws-props select[data-key=font]")).toBeVisible();
    await addSource(page, "Countdown");
    await page.getByLabel("Min", { exact: true }).fill("5");
    await page.click("text=Start from now");
    const t = await page.evaluate(() => window.studioWorkspace.project.overlays[1]);
    const secs = (Date.parse(t.target + "Z") - Date.now()) / 1000;
    expect(t.tz).toBe("UTC"); expect(secs).toBeGreaterThan(280); expect(secs).toBeLessThan(310);
    await saveDraft(page);
    await page.goto(BASE + "/console/studio/" + name);
    await page.waitForFunction(() => !!window.studioWorkspace);
    const saved = await page.evaluate(() => window.studioWorkspace.project.overlays.map((o) => [o.kind, o.align, o.bg_opacity]));
    expect(saved).toEqual([["text", "center", 0.6], ["countdown", "left", 0]]);
  });

  test("encoder choice: NVIDIA is untested until you test it and is never selected automatically; software stays the default", async ({ page }) => {
    await project(page);
    await page.click("#ws-settings");
    await page.locator(".sset-tab", { hasText: "Output" }).click();
    const sel = page.locator('.sset-pane select[aria-label="Encoder"]');
    await expect(sel).toHaveValue("libx264");
    await expect(page.locator('.sset-pane option[value="h264_nvenc"]')).toHaveAttribute("disabled", "");
    await expect(page.locator(".sset-pane")).toContainText(/Not tested yet|NVIDIA H\.264 \(h264_nvenc\):/);
    await page.getByRole("button", { name: "Test NVIDIA encoding" }).click();
    await expect(page.locator(".toast", { hasText: /NVENC/ })).toBeVisible({ timeout: 30000 });
    // on a machine without a working GPU encoder the option stays unavailable and says why; software remains selected
    const nvOk = await page.locator('.sset-pane option[value="h264_nvenc"]').evaluate((o) => !o.disabled);
    if (!nvOk) { await expect(page.locator(".sset-pane")).toContainText("NVIDIA H.264 (h264_nvenc):"); }
    await expect(sel).toHaveValue("libx264");
  });

  test("Mode B: a playlist can be created from Media Library videos in a chosen order and then used as the source", async ({ page }) => {
    await project(page);
    await page.click("#tab-sources");
    await page.click("text=Create a playlist from Media Library videos");
    await page.locator('.sdlg .smb-card[data-name="motion.mp4"]').click();
    await page.keyboard.down("Control"); await page.locator('.sdlg .smb-card[data-name="intro.webm"]').click(); await page.keyboard.up("Control");
    await page.locator(".sdlg-foot .btn", { hasText: "Use selected" }).click();
    await page.getByLabel("Playlist name").fill("pl-" + uid().replace(/[^a-z0-9-]/g, ""));
    await page.locator('.sdlg [aria-label="Move later"]').first().click();
    await page.locator(".sdlg .btn", { hasText: "Create playlist" }).click();
    await expect(page.locator(".toast.success", { hasText: "created" })).toBeVisible();
  });

  test("a logged-out visitor is sent to the login page and the JSON API answers 401", async ({ page, request }) => {
    await page.goto(BASE + "/console/studio/anything");
    await expect(page).toHaveURL(/\/login/);
    const r = await request.get(BASE + "/console/studio/media.json");
    expect(r.status()).toBe(401);
  });
});
