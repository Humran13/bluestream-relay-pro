// Regression tests for web/player.js URL-duplication handling.
// Pure Node (node:test + node:vm), no browser/DOM dependency required.
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");
const path = require("node:path");

const SRC = fs.readFileSync(path.join(__dirname, "player.js"), "utf8");

function makeEl() {
  return { textContent: "", hidden: true, value: "" };
}

function runPlayer(searchString) {
  const replaceStateCalls = [];
  const elements = {
    video: Object.assign(makeEl(), {
      canPlayType: (type) => (type === "application/vnd.apple.mpegurl" ? "maybe" : ""),
      play: () => Promise.resolve(),
    }),
    info: makeEl(),
    error: makeEl(),
    "channel-label": makeEl(),
  };
  const location = {
    origin: "https://stream.example.com",
    pathname: "/player/",
    hash: "",
    search: searchString,
  };
  const sandbox = {
    window: {
      location,
      history: {
        replaceState: (state, title, url) => {
          replaceStateCalls.push(url);
          // Reflect the rewrite back into location, like a real browser would.
          const qIdx = url.indexOf("?");
          location.search = qIdx === -1 ? "" : url.slice(qIdx);
        },
      },
      Hls: undefined,
    },
    document: {
      getElementById: (id) => elements[id],
    },
    URL,
    URLSearchParams,
    console,
  };
  sandbox.window.document = sandbox.document;
  vm.createContext(sandbox);
  vm.runInContext(SRC, sandbox);
  return { elements, replaceStateCalls, location };
}

test("duplicated relay param with no & separator is recovered, not treated as invalid", () => {
  const { elements, replaceStateCalls, location } = runPlayer("?relay=1080test1?relay=1080test1");
  assert.equal(elements.error.hidden, true, "player must not show an error for this known-bad paste");
  assert.equal(elements["channel-label"].textContent, "Relay: 1080test1");
  // Address bar must be rewritten to the canonical single-parameter form.
  assert.equal(replaceStateCalls.length, 1);
  assert.equal(location.search, "?relay=1080test1");
});

test("duplicated playlist param is recovered the same way", () => {
  const { elements, location } = runPlayer("?playlist=evening-promo?playlist=evening-promo");
  assert.equal(elements.error.hidden, true);
  assert.equal(elements["channel-label"].textContent, "Playlist: evening-promo");
  assert.equal(location.search, "?playlist=evening-promo");
});

test("normal single relay param is untouched and never rewritten", () => {
  const { elements, replaceStateCalls, location } = runPlayer("?relay=1080test1");
  assert.equal(elements.error.hidden, true);
  assert.equal(elements["channel-label"].textContent, "Relay: 1080test1");
  assert.equal(replaceStateCalls.length, 0, "must not touch the address bar when nothing is malformed");
  assert.equal(location.search, "?relay=1080test1");
});

test("relay name with extra valid query params preserved via & is untouched", () => {
  const { replaceStateCalls, location } = runPlayer("?relay=1080test1&debug=1");
  assert.equal(replaceStateCalls.length, 0);
  assert.equal(location.search, "?relay=1080test1&debug=1");
});

test("malformed/invalid relay name after recovery still reports an error, never an invalid HLS url", () => {
  const { elements } = runPlayer("?relay=BAD_NAME!?relay=BAD_NAME!");
  assert.equal(elements.error.hidden, false);
  assert.match(elements.error.textContent, /No channel specified/);
});

test("no params shows the no-channel error", () => {
  const { elements } = runPlayer("");
  assert.equal(elements.error.hidden, false);
  assert.match(elements.error.textContent, /No channel specified/);
});

test("src param may legitimately contain its own query string and is passed through verbatim", () => {
  const { elements } = runPlayer("?src=" + encodeURIComponent("https://cdn.example.com/x.m3u8?token=abc"));
  assert.equal(elements["channel-label"].textContent, "Custom source");
});
