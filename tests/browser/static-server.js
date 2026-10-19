// Minimal static file server for web/, used only by Playwright browser tests.
"use strict";
const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.join(__dirname, "..", "..", "web");
const PORT = 4873;

const TYPES = {
  ".html": "text/html",
  ".js": "application/javascript",
  ".css": "text/css",
};

http
  .createServer((req, res) => {
    const url = new URL(req.url, "http://localhost");
    // Mirror production's nginx `location /player/ { alias web/; }` so that
    // relative asset paths like player.js resolve the same way under test.
    let file = url.pathname.replace(/^\/player\//, "/");
    if (file === "/" || file === "") file = "/index.html";
    const full = path.join(ROOT, path.normalize(file).replace(/^(\.\.[/\\])+/, ""));
    fs.readFile(full, (err, data) => {
      if (err) {
        res.writeHead(404).end("not found");
        return;
      }
      res.writeHead(200, { "Content-Type": TYPES[path.extname(full)] || "application/octet-stream" });
      res.end(data);
    });
  })
  .listen(PORT, () => {
    console.log("static server on " + PORT);
  });
