// @ts-check
const { defineConfig, devices } = require("@playwright/test");

// Serves web/ as static files so the player loads exactly as it would in
// production (no app.py/nginx dependency - this is a pure client-side
// parsing/rendering check of web/player.js).
module.exports = defineConfig({
  testDir: __dirname,
  fullyParallel: true,
  reporter: [["list"]],
  webServer: {
    command: "node static-server.js",
    cwd: __dirname,
    port: 4873,
    reuseExistingServer: true,
    timeout: 15000,
  },
  use: {
    baseURL: "http://127.0.0.1:4873",
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
    { name: "firefox", use: { ...devices["Desktop Firefox"] } },
    { name: "webkit", use: { ...devices["Desktop Safari"] } },
    // Real branded browsers, not just bundled Chromium - requires the
    // binary to be installed on the host; skipped automatically otherwise.
    { name: "chrome", use: { ...devices["Desktop Chrome"], channel: "chrome" } },
    { name: "msedge", use: { ...devices["Desktop Edge"], channel: "msedge" } },
  ],
});
