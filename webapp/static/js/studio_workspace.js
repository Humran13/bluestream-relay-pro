/* BlueStream Studio workspace: toolbar, media browser, source menu, preview canvas, properties, layers, stream
 * controls and settings. The server renders the project as JSON (#studio-state); every change is validated
 * again on the server (save.json), so what you see here is only ever a view of what the server will accept.
 * Dragging a layer never encodes anything: the canvas is plain HTML on top of a still frame of the source.
 */
(function () {
  "use strict";
  var stateEl = document.getElementById("studio-state"), root = document.getElementById("ws");
  if (!stateEl || !root) { return; }
  var S = JSON.parse(stateEl.textContent), csrf = root.dataset.csrf, U = S.urls;
  var P = S.project, settings = S.settings, texts = S.texts.slice(), ptexts = [];
  var view = "draft", sel = P.overlays.length ? P.overlays.length - 1 : -2, saved = "", dirty = false, busy = false;
  var stream = { exists: S.stream_exists, active: false, publishers: [], state: "", detail: "" };
  var natural = {};            // media name -> natural height/width ratio (from the loaded preview image)
  var published = S.published, unpublished = S.unpublished, capacity = (S.capabilities || {}).assessment || null;
  var missing = [];
  var INPLACE = !!P.target_relay, inplaceRaw = null;   // INPLACE: these overlays go onto the EXISTING stream named in P.target_relay
  var $ = function (s, r) { return (r || root).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || root).querySelectorAll(s)); };

  function h(tag, attrs, kids) {
    var e = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      var v = attrs[k];
      if (v == null || v === false) { return; }
      if (k === "class") { e.className = v; } else if (k === "text") { e.textContent = v; }
      else if (k.slice(0, 2) === "on") { e.addEventListener(k.slice(2), v); }
      else if (k === "value") { e.value = v; } else { e.setAttribute(k, v === true ? "" : v); }
    });
    (kids || []).forEach(function (c) { if (c != null) { e.appendChild(typeof c === "string" ? document.createTextNode(c) : c); } });
    return e;
  }
  var ICONS = {
    video: "M3 6h13v12H3zM16 10l5-3v10l-5-3", unlock: "M6 11h12v9H6zM8 11V8a4 4 0 0 1 7.5-2", pencil: "M4 20h4L19 9l-4-4L4 16zM14 6l4 4", image: "M3 4h18v16H3zM3 16l5-5 4 4 3-3 6 6M16 9h.01", slideshow: "M6 7h14v11H6zM3 4h14",
    text: "M5 5h14M12 5v14M9 19h6", rect: "M4 4h16v16H4z", clock: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM12 7v5l3 2",
    countdown: "M5 3h14M5 21h14M7 3v4l5 5-5 5v4M17 3v4l-5 5 5 5v4", countup: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM12 7v5h4",
    pomodoro: "M12 6a8 8 0 1 0 0 16 8 8 0 0 0 0-16zM12 2v4M12 10v4l2 2", ticker: "M3 8h18v8H3zM7 12h6",
    nowplaying: "M9 18V6l10-2v12M9 18a3 3 0 1 1-6 0 3 3 0 0 1 6 0zM19 16a3 3 0 1 1-6 0 3 3 0 0 1 6 0z",
    data: "M4 20V10M10 20V4M16 20v-8M22 20H2", browser: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM3 12h18M12 3c3 3 3 15 0 18M12 3c-3 3-3 15 0 18",
    stream: "M7.8 7.8a6 6 0 0 0 0 8.4M16.2 7.8a6 6 0 0 1 0 8.4M12 11a1 1 0 1 0 0 2 1 1 0 0 0 0-2z", playlist: "M4 6h12M4 12h12M4 18h8M18 15v5",
    audio: "M4 9v6h4l5 4V5L8 9zM16 9a4 4 0 0 1 0 6", eye: "M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8zM12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z",
    eyeoff: "M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8zM3 3l18 18", up: "M6 15l6-6 6 6", down: "M6 9l6 6 6-6",
    trash: "M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13", copy: "M9 9h11v11H9zM5 15V5h10", plus: "M12 5v14M5 12h14",
    gear: "M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6zM19 12l2-1-1-3-2 .5-1.5-1.5.5-2-3-1-1 2h-2l-1-2-3 1 .5 2L6 8.5 4 8 3 11l2 1v0l-2 1 1 3 2-.5L7.5 17l-.5 2 3 1 1-2h2l1 2 3-1-.5-2 1.5-1.5 2 .5 1-3z",
    folder: "M3 6h6l2 2h10v11H3z", play: "M7 5l12 7-12 7z", stop: "M6 6h12v12H6z", lock: "M6 11h12v9H6zM8 11V8a4 4 0 0 1 8 0v3",
    save: "M5 3h11l3 3v15H5zM8 3v6h7V3M8 21v-7h8v7", layers: "M12 3l9 5-9 5-9-5zM3 13l9 5 9-5", close: "M6 6l12 12M18 6L6 18"
  };
  function icon(name, size) {
    var s = size || 16, ns = "http://www.w3.org/2000/svg", svg = document.createElementNS(ns, "svg"), p = document.createElementNS(ns, "path");
    svg.setAttribute("viewBox", "0 0 24 24"); svg.setAttribute("width", s); svg.setAttribute("height", s);
    svg.setAttribute("fill", "none"); svg.setAttribute("stroke", "currentColor"); svg.setAttribute("stroke-width", "1.8");
    svg.setAttribute("stroke-linecap", "round"); svg.setAttribute("stroke-linejoin", "round"); svg.setAttribute("aria-hidden", "true");
    p.setAttribute("d", ICONS[name] || ICONS.rect); svg.appendChild(p); return svg;
  }
  function ibtn(name, label, onclick, cls) {
    var b = h("button", { type: "button", class: "ibtn " + (cls || ""), title: label, "aria-label": label, onclick: onclick }, [icon(name)]);
    return b;
  }

  // ---- generic UI helpers -------------------------------------------------
  var toastBox = $("#ws-toasts");
  function toast(msg, kind) {
    var t = h("div", { class: "toast " + (kind || "info"), role: kind === "error" ? "alert" : "status", text: msg });
    toastBox.appendChild(t); setTimeout(function () { t.remove(); }, kind === "error" ? 9000 : 4000);
  }
  function dialog(title, body, buttons, wide) {
    return new Promise(function (resolve) {
      var prev = document.activeElement, back = h("div", { class: "sdlg-back" });
      var dlg = h("div", { class: "sdlg" + (wide ? " sdlg-wide" : ""), role: "dialog", "aria-modal": "true", "aria-label": title });
      var foot = h("div", { class: "sdlg-foot" });
      function close(v) { back.remove(); document.removeEventListener("keydown", onKey, true); if (prev && prev.focus) { prev.focus(); } resolve(v); }
      function onKey(e) { if (e.key === "Escape") { e.stopPropagation(); close(null); } }
      document.addEventListener("keydown", onKey, true);
      buttons.forEach(function (b) {
        foot.appendChild(h("button", { type: "button", class: "btn" + (b.secondary ? " btn-secondary" : "") + (b.danger ? " btn-danger" : ""), text: b.label,
          onclick: function () { if (b.validate && !b.validate()) { return; } close(b.value); } }));
      });
      dlg.appendChild(h("div", { class: "sdlg-head" }, [h("h2", { text: title })]));
      dlg.appendChild(h("div", { class: "sdlg-body" }, [body])); dlg.appendChild(foot); back.appendChild(dlg);
      back.addEventListener("mousedown", function (e) { if (e.target === back) { close(null); } });
      document.body.appendChild(back);
      var f = dlg.querySelector("input,select,button.btn"); if (f) { f.focus(); }
    });
  }
  function confirmDialog(title, message, okLabel, danger) {
    return dialog(title, h("p", { text: message }), [{ label: "Cancel", value: false, secondary: true }, { label: okLabel || "OK", value: true, danger: !!danger }])
      .then(function (v) { return v === true; });
  }
  function api(url, body, method) {
    var opt = { method: method || (body === undefined ? "GET" : "POST"), credentials: "same-origin", headers: { "Accept": "application/json" } };
    if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.headers["X-CSRF-Token"] = csrf; opt.body = JSON.stringify(body); }
    return fetch(url, opt).then(function (r) {
      return r.json().catch(function () { return { ok: false, error: "Unexpected response from the server (" + r.status + ")." }; })
        .then(function (d) { if (r.status === 401) { d.error = "Your session has ended. Reload the page and sign in again."; } return d; });
    }).catch(function () { return { ok: false, error: "Could not reach the server." }; });
  }
  function clone(o) { return JSON.parse(JSON.stringify(o)); }
  function clamp(v, lo, hi) { return Math.min(hi, Math.max(lo, v)); }
  function r4(v) { return Math.round(v * 10000) / 10000; }
  function snapshot() { var c = clone(P); delete c.revision; return JSON.stringify(c); }
  function pct(v) { return Math.round(v * 1000) / 10; }
  var canEdit = !!S.can_edit;

  // ---- dirty state ---------------------------------------------------------
  var lastDirty = false;
  function touch() {
    dirty = snapshot() !== saved;
    if (dirty !== lastDirty) { lastDirty = dirty; if (root.dataset.ready) { renderStatus(); } }
    var badge = $("#ws-dirty"); badge.hidden = !dirty; $("#ws-save").disabled = !dirty || !canEdit || busy;
    root.classList.toggle("is-dirty", dirty);
  }
  window.addEventListener("beforeunload", function (e) { if (dirty) { e.preventDefault(); e.returnValue = ""; } });

  // ---- layer helpers -------------------------------------------------------
  var LOCKED = { image: 1, video: 1, browser: 1 };
  var DYNAMIC = { clock: 1, countdown: 1, countup: 1, pomodoro: 1, ticker: 1, nowplaying: 1, data: 1, video: 1, browser: 1 };
  var TEXTY = { text: 1, clock: 1, countdown: 1, countup: 1, pomodoro: 1, ticker: 1, nowplaying: 1, data: 1 };
  var NAMES = { text: "Text", image: "Image", rect: "Color", clock: "Clock", countdown: "Countdown", countup: "Countup", pomodoro: "Pomodoro",
                ticker: "Ticker", nowplaying: "Now Playing", data: "Data", video: "Video", browser: "Browser" };
  function layerName(o) { return o.label || ((NAMES[o.kind] || o.kind) + ((o.kind === "image" || o.kind === "video") && o.asset ? ": " + o.asset : "")); }
  function outW() { return P.profile.width || (playerInfo && playerInfo.width) || 1280; }
  function outH() { return P.profile.height || (playerInfo && playerInfo.height) || 720; }
  function canvasAspect() { return outW() / outH(); }
  function aspectOf(o) {
    if (o.kind === "browser") { return o.view_h / o.view_w; }
    var a = natural[o.asset]; return a ? a * (o.crop_h / o.crop_w) : null;
  }
  /* Image/video layers are scaled by WIDTH only in the FFmpeg graph, so the box height follows the picture. */
  function fit(o) {
    var a = aspectOf(o); if (!LOCKED[o.kind] || !a) { return; }
    o.w = clamp(o.w, 0.02, 1);
    var hh = o.w * canvasAspect() * a;
    if (hh > 1) { o.w = o.w / hh; hh = 1; }
    o.h = r4(hh); o.w = r4(o.w);
    o.x = r4(clamp(o.x, 0, 1 - o.w)); o.y = r4(clamp(o.y, 0, 1 - o.h));
  }
  function box(o) { return { x: o.x, y: o.y, w: o.w, h: o.h }; }
  function putBox(o, b) {
    b.w = clamp(b.w, 0.02, 1); b.h = clamp(b.h, 0.02, 1); b.x = clamp(b.x, 0, 1 - b.w); b.y = clamp(b.y, 0, 1 - b.h);
    o.x = r4(b.x); o.y = r4(b.y); o.w = r4(b.w); o.h = r4(b.h);
  }
  function active() { return P; }
  function shown() { return active().overlays; }

  // ---- canvas ----------------------------------------------------------------
  var canvas = $("#ws-canvas"), guides = $("#ws-guides");
  function srcFrameUrl(p) {
    var q = "?kind=" + encodeURIComponent(p.source_kind) + "&ref=" + encodeURIComponent(p.source_ref) + "&bg=" + encodeURIComponent(p.background);
    return U.sourceFrame + q;
  }
  var bgKey = "", mon = "source", live = { relays: [], playlists: [] }, playerInfo = { mode: "none", state: "idle" };
  var player = StudioPlayer.create(canvas, $("#ws-vbar"), { onState: function (i) {
    var dimsChanged = playerInfo && (playerInfo.width !== i.width || playerInfo.height !== i.height);
    playerInfo = i; if (sel === -2) { refreshSourceInfo(); }
    if (dimsChanged && !P.profile.width) { canvas.style.aspectRatio = outW() + " / " + outH(); $$(".ly", canvas).forEach(function (n) { paintLayer(+n.dataset.i); }); }
  } });
  function hlsFor(kind, ref) {
    var list = kind === "relay" ? live.relays : live.playlists, row = list.filter(function (r) { return r.name === ref; })[0];
    return row || null;
  }
  function bgSpec() {
    var p = P;
    if (mon === "output") {
      return stream.hls_url && stream.active ? { mode: "hls", url: stream.hls_url, label: "Studio output" } : { mode: "none", message: "The Studio stream is not running, so there is no live output to watch." };
    }
    if (INPLACE) {
      var ip = stream.inplace || {}, comp = ip.marker === "on" && stream.plan === "overlay-live-composition";
      if (stream.exists && !stream.active) { return { mode: "none", message: "'" + p.source_ref + "' is stopped, so there is no live picture. Start it on the Streams page; Studio will not start it for you." }; }
      if (comp && inplaceRaw) { return { mode: "file", url: inplaceRaw }; }          // the loop file WITHOUT the layers that are burned into the stream
      var u = stream.hls_url || (hlsFor("relay", p.source_ref) || {}).hls_url;
      return u ? { mode: "hls", url: u, label: p.source_ref + (comp ? " (on air, with the published layers)" : "") } : { mode: "none", message: "Looking for the stream\u2026" };
    }
    if (p.source_kind === "media") { return p.source_ref ? { mode: "file", url: U.play.replace("__A__", encodeURIComponent(p.source_ref)) } : { mode: "none", message: "Choose a video from the Media panel." }; }
    if (p.source_kind === "relay" || p.source_kind === "playlist") {
      var row = hlsFor(p.source_kind, p.source_ref);
      if (!p.source_ref) { return { mode: "none", message: "Choose a live source." }; }
      if (!row) { return { mode: "none", message: "That " + p.source_kind + " was not found. It may have been deleted." }; }
      if (!row.active) { return { mode: "none", message: "'" + row.name + "' is stopped: there is no live picture. Start it on the Streams page; Studio will not start it for you." }; }
      return { mode: "hls", url: row.hls_url, label: row.name };
    }
    return { mode: "still" };
  }
  function paintBackground() {
    var p = active(), spec = bgSpec(), key = JSON.stringify(spec) + "|" + p.background + "|" + p.source_ref, bg = $("#ws-bg"), note = $("#ws-bgnote");
    if (key === bgKey) { return; }
    bgKey = key;
    note.hidden = true; canvas.style.background = "";
    if (spec.mode === "still") {
      player.load({ mode: "none" });
      if (p.source_kind === "audio" && /^#[0-9a-fA-F]{6}$/.test(p.background)) { bg.removeAttribute("src"); bg.hidden = true; canvas.style.background = p.background; return; }
      bg.hidden = false;
      bg.onerror = function () { bg.hidden = true; note.hidden = false; note.textContent = "No picture is available for this source."; };
      bg.onload = function () { bg.hidden = false; note.hidden = true; };
      if (p.source_ref) { bg.src = srcFrameUrl(p); } else { bg.hidden = true; note.hidden = false; note.textContent = "Choose a source."; }
      return;
    }
    bg.hidden = true; bg.removeAttribute("src");
    player.load(spec);
  }
  function refreshSourceInfo() { if (sel === -2 && mon === "source") { buildProps(); } }
  var scaleFont = function (o) { return (o.size / outH() * 100).toFixed(3) + "cqh"; };

  function layerEl(o, i, live) {
    var kind = o.kind, e = h("div", { class: "ly ly-" + kind + (LOCKED[kind] ? " locked" : ""), "data-i": i, tabindex: live ? "0" : "-1", role: "button",
      "aria-label": "Layer " + (i + 1) + ": " + layerName(o) });
    if (kind === "image" || kind === "video") {
      var img = h("img", { class: "ly-img", alt: "", draggable: "false" });
      img.addEventListener("load", function () {
        if (img.naturalWidth) { natural[o.asset] = img.naturalHeight / img.naturalWidth; }
        if (live) { var before = JSON.stringify(box(o)), wasDirty = dirty; fit(o); if (JSON.stringify(box(o)) !== before) { paintLayer(i); syncBox(); if (!wasDirty) { saved = snapshot(); } touch(); } }
      });
      img.addEventListener("error", function () { e.classList.add("broken"); e.setAttribute("data-missing", o.asset); });
      img.src = o.asset ? (kind === "image" ? U.asset : U.thumb).replace("__A__", encodeURIComponent(o.asset)) : "";
      e.appendChild(img);
      if (kind === "video") { e.appendChild(h("span", { class: "ly-badge" }, [icon("video", 12)])); }
    } else if (kind === "browser") {
      e.appendChild(h("span", { class: "ly-ph", text: "Browser: " + (o.url || "") }));
    } else if (kind !== "rect") { e.appendChild(h("span", { class: "ly-label" })); }
    if (live) { ["nw", "ne", "sw", "se", "n", "s", "e", "w"].forEach(function (d) { e.appendChild(h("i", { class: "hnd hnd-" + d, "data-h": d, "aria-hidden": "true" })); }); }
    return e;
  }

  function paintLayer(i) {
    var o = shown()[i], e = $('.ly[data-i="' + i + '"]', canvas); if (!o || !e) { return; }
    e.style.left = pct(o.x) + "%"; e.style.top = pct(o.y) + "%"; e.style.width = pct(o.w) + "%"; e.style.height = pct(o.h) + "%";
    e.style.opacity = o.opacity; e.style.zIndex = i + 1; e.style.color = o.color;
    e.classList.toggle("hidden", !o.visible);
    if (o.kind === "rect") { e.style.background = o.color; }
    else if (TEXTY[o.kind]) {
      var t = $(".ly-label", e); e.style.fontSize = scaleFont(o);
      t.textContent = (o.kind === "text" || o.kind === "ticker") ? o.text : ((view === "program" ? ptexts : texts)[i] || "");
    } else if (o.kind === "image" || o.kind === "video") {
      var im = $("img", e);
      var cw = clamp(o.crop_w, 0.05, 1), ch = clamp(o.crop_h, 0.05, 1);
      im.style.width = (100 / cw) + "%"; im.style.height = (100 / ch) + "%"; im.style.left = (-o.crop_x / cw * 100) + "%"; im.style.top = (-o.crop_y / ch * 100) + "%";
    }
    var lbl = e.getAttribute("aria-label"); if (lbl) { e.setAttribute("aria-label", "Layer " + (i + 1) + ": " + layerName(o)); }
    e.classList.toggle("sel", view === "draft" && i === sel);
  }
  function renderCanvas() {
    var ro = mon === "output";
    canvas.classList.toggle("program", ro); canvas.style.aspectRatio = outW() + " / " + outH();
    $$(".ly", canvas).forEach(function (n) { n.remove(); });
    var ipn = stream.inplace || {}, dupHidden = INPLACE && ipn.marker === "on" && stream.plan === "overlay-live-composition" && !inplaceRaw;
    if (!ro) {
      shown().forEach(function (o, i) {
        // the on-air picture already contains the PUBLISHED layers: draw only what differs, or they would appear twice
        if (dupHidden && published && published.overlays[i] && JSON.stringify(published.overlays[i]) === JSON.stringify(o)) { return; }
        var e = layerEl(o, i, canEdit && !o.locked); canvas.insertBefore(e, guides); paintLayer(i);
      });
    }
    paintBackground();
    var note = $("#ws-monnote");
    note.textContent = ro ? "Actual encoded output of the Studio stream, about 10 s behind. Layers are already burned into this video." : "";
    canvas.classList.toggle("safe", !!settings.general.safe_areas); canvas.classList.toggle("grid", !!settings.general.show_grid);
  }
  function syncBox() { if (sel >= 0) { $$("[data-key]", $("#ws-props")).forEach(syncInput); } }

  // ---- canvas interaction -------------------------------------------------
  var drag = null;
  function snapDistance() { return settings.general.snap_sensitivity / 1280; }
  function snapped(i, b, mode, edgesMoved) {
    var g = settings.general; guides.innerHTML = "";
    if (!g.snap_enabled) { return b; }
    var xs = [], ys = [], d = snapDistance(), dy = d * 1280 / 720;
    if (g.snap_edges) { xs.push(0, 1); ys.push(0, 1); }
    if (g.snap_center) { xs.push(0.5); ys.push(0.5); }
    if (g.snap_sources) {
      P.overlays.forEach(function (o, j) { if (j !== i && o.visible) { xs.push(o.x, o.x + o.w / 2, o.x + o.w); ys.push(o.y, o.y + o.h / 2, o.y + o.h); } });
    }
    function near(v, list, tol) { var best = null; list.forEach(function (c) { if (Math.abs(v - c) < tol && (best === null || Math.abs(v - c) < Math.abs(v - best))) { best = c; } }); return best; }
    function guide(axis, v) { guides.appendChild(h("i", { class: "guide g-" + axis, style: (axis === "x" ? "left:" : "top:") + (v * 100) + "%" })); }
    if (mode === "move") {
      var cx = [[b.x, 0], [b.x + b.w / 2, b.w / 2], [b.x + b.w, b.w]], cy = [[b.y, 0], [b.y + b.h / 2, b.h / 2], [b.y + b.h, b.h]];
      cx.some(function (p) { var s = near(p[0], xs, d); if (s !== null) { b.x = s - p[1]; guide("x", s); return true; } });
      cy.some(function (p) { var s = near(p[0], ys, dy); if (s !== null) { b.y = s - p[1]; guide("y", s); return true; } });
      return b;
    }
    return b;
  }
  canvas.addEventListener("pointerdown", function (ev) {
    if (view !== "draft" || !canEdit) { return; }
    var e = ev.target.closest(".ly"); if (!e || e.classList.contains("hidden")) { select(-2, true); return; }
    var i = +e.dataset.i, o = P.overlays[i], hnd = ev.target.closest(".hnd");
    select(i, false);
    if (o.locked) { say("Layer " + (i + 1) + " is locked. Unlock it in the layer list to move it."); return; }
    drag = { i: i, mode: hnd ? "resize:" + hnd.dataset.h : "move", sx: ev.clientX, sy: ev.clientY, rect: canvas.getBoundingClientRect(), start: box(o), id: ev.pointerId, moved: false };
    try { e.setPointerCapture(ev.pointerId); } catch (x) { /* ignore */ }
    ev.preventDefault();
  });
  canvas.addEventListener("pointermove", function (ev) {
    if (!drag || ev.pointerId !== drag.id) { return; }
    var o = P.overlays[drag.i], dx = (ev.clientX - drag.sx) / drag.rect.width, dy = (ev.clientY - drag.sy) / drag.rect.height;
    var b = { x: drag.start.x, y: drag.start.y, w: drag.start.w, h: drag.start.h };
    drag.moved = true;
    if (drag.mode === "move") {
      b.x = clamp(b.x + dx, 0, 1 - b.w); b.y = clamp(b.y + dy, 0, 1 - b.h);
      if (!ev.altKey) { b = snapped(drag.i, b, "move"); }
    } else {
      var hd = drag.mode.split(":")[1], x2 = b.x + b.w, y2 = b.y + b.h, MIN = 0.02;
      if (LOCKED[o.kind] && aspectOf(o)) {
        // corner/edge drag keeps the picture's shape: the width decides, the height follows
        var grow = hd.indexOf("e") >= 0 ? dx : hd.indexOf("w") >= 0 ? -dx : (hd.indexOf("s") >= 0 ? dy : -dy) / (canvasAspect() * aspectOf(o));
        var w = clamp(drag.start.w + grow, MIN, 1), hh = w * canvasAspect() * aspectOf(o);
        if (hh > 1) { w = w / hh; hh = 1; }
        b.w = w; b.h = hh;
        if (hd.indexOf("w") >= 0) { b.x = x2 - w; } if (hd.indexOf("n") >= 0) { b.y = y2 - hh; }
        if (b.x < 0) { b.x = 0; b.w = x2; b.h = b.w * canvasAspect() * aspectOf(o); }
        if (b.y < 0) { b.y = 0; }
      } else {
        if (hd.indexOf("e") >= 0) { x2 = clamp(x2 + dx, b.x + MIN, 1); }
        if (hd.indexOf("w") >= 0) { b.x = clamp(b.x + dx, 0, x2 - MIN); }
        if (hd.indexOf("s") >= 0) { y2 = clamp(y2 + dy, b.y + MIN, 1); }
        if (hd.indexOf("n") >= 0) { b.y = clamp(b.y + dy, 0, y2 - MIN); }
        b.w = x2 - b.x; b.h = y2 - b.y;
      }
    }
    putBox(o, b); paintLayer(drag.i); syncBox(); touch(); ev.preventDefault();
  });
  function endDrag(ev) {
    if (!drag || (ev && ev.pointerId !== drag.id)) { return; }
    guides.innerHTML = ""; var o = P.overlays[drag.i];
    say("Layer " + (drag.i + 1) + " at " + Math.round(o.x * 100) + "%, " + Math.round(o.y * 100) + "%, size " + Math.round(o.w * 100) + " by " + Math.round(o.h * 100) + " percent");
    drag = null; renderLayers();
  }
  canvas.addEventListener("pointerup", endDrag); canvas.addEventListener("pointercancel", endDrag);
  canvas.addEventListener("keydown", function (ev) {
    var e = ev.target.closest(".ly"); if (!e || view !== "draft" || !canEdit) { return; }
    var i = +e.dataset.i, o = P.overlays[i], step = ev.shiftKey ? 0.05 : 0.005, b = box(o);
    if (o.locked) { return; }
    var dx = ev.key === "ArrowRight" ? step : ev.key === "ArrowLeft" ? -step : 0, dy = ev.key === "ArrowDown" ? step : ev.key === "ArrowUp" ? -step : 0;
    if (ev.ctrlKey || ev.metaKey) { return; }
    if (!dx && !dy) { if (ev.key === "Enter" || ev.key === " ") { select(i, false); ev.preventDefault(); } return; }
    if (ev.altKey) { b.w += dx; b.h += dy; } else { b.x += dx; b.y += dy; }
    putBox(o, b); if (ev.altKey && LOCKED[o.kind]) { fit(o); } paintLayer(i); syncBox(); touch(); select(i, false); ev.preventDefault();
  });
  function say(msg) { $("#ws-live").textContent = msg; }

  // ---- selection ----------------------------------------------------------
  function select(i, rebuild) {
    sel = i;
    $$(".ly", canvas).forEach(function (n) { n.classList.toggle("sel", view === "draft" && +n.dataset.i === i); });
    renderLayers(); if (rebuild !== false || true) { buildProps(); }
    if (i >= 0) { var n = $('.ly[data-i="' + i + '"]', canvas); if (n && document.activeElement !== n && !drag) { try { n.focus({ preventScroll: true }); } catch (x) { /* ignore */ } } }
  }

  // ---- mutations --------------------------------------------------------------
  function setLayer(i, key, value, rebuild) {
    var o = P.overlays[i]; if (!o) { return; }
    o[key] = value;
    if (key === "x" || key === "y" || key === "w" || key === "h") { putBox(o, box(o)); }
    if (key === "crop_x") { o.crop_w = r4(Math.min(o.crop_w, 1 - o.crop_x)); }
    if (key === "crop_y") { o.crop_h = r4(Math.min(o.crop_h, 1 - o.crop_y)); }
    if (key === "crop_w") { o.crop_w = r4(Math.min(o.crop_w, 1 - o.crop_x)); }
    if (key === "crop_h") { o.crop_h = r4(Math.min(o.crop_h, 1 - o.crop_y)); }
    if (key === "w" || key.indexOf("crop_") === 0 || key === "view_w" || key === "view_h" || key === "asset") { fit(o); }
    if (key === "asset") { delete natural[o.asset]; renderCanvas(); } else { paintLayer(i); }
    touch(); renderLayers(); if (TEXTY[o.kind]) { queueTexts(); } if (rebuild) { buildProps(); } else { syncBox(); }
  }
  function addLayer(kind, patch) {
    if (P.overlays.length >= 24) { toast("A project can have at most 24 layers.", "error"); return; }
    var d = Object.assign({}, S.defaults[kind], patch || {});
    P.overlays.push(d); texts.push(""); sel = P.overlays.length - 1; touch(); renderCanvas(); select(sel); queueTexts();
    if (LOCKED[kind]) { fit(d); }
    say(NAMES[kind] + " layer added");
  }
  function removeLayer(i) {
    P.overlays.splice(i, 1); texts.splice(i, 1); sel = Math.min(sel, P.overlays.length - 1); if (sel < 0) { sel = -2; }
    touch(); renderCanvas(); select(sel); say("Layer removed");
  }
  function moveLayer(i, d) {
    var j = i + d; if (j < 0 || j >= P.overlays.length) { return; }
    var t = P.overlays[i]; P.overlays[i] = P.overlays[j]; P.overlays[j] = t;
    var x = texts[i]; texts[i] = texts[j]; texts[j] = x; sel = j; touch(); renderCanvas(); select(j); say("Layer moved " + (d < 0 ? "down" : "up") + " in the stack");
  }
  function dupLayer(i) {
    if (P.overlays.length >= 24) { toast("A project can have at most 24 layers.", "error"); return; }
    var c = clone(P.overlays[i]); c.x = r4(Math.min(c.x + 0.02, 1 - c.w)); c.y = r4(Math.min(c.y + 0.02, 1 - c.h)); c.label = (layerName(P.overlays[i]) + " copy").slice(0, 40);
    P.overlays.splice(i + 1, 0, c); texts.splice(i + 1, 0, texts[i]); sel = i + 1; touch(); renderCanvas(); select(sel);
  }
  function setSource(kind, ref, extra) {
    P.source_kind = kind; P.source_ref = ref; Object.assign(P, extra || {});
    bgKey = ""; touch(); renderCanvas(); renderLayers(); select(-2); toast("Background source set. Save the draft to keep it.", "info");
  }

  // ---- layers list ------------------------------------------------------------
  function renameLayer(i, btn) {
    var o = P.overlays[i]; if (!o || !canEdit) { return; }
    var inp = h("input", { type: "text", class: "lrename", value: o.label || "", maxlength: 40, "aria-label": "Layer name", placeholder: NAMES[o.kind] || o.kind });
    function done(commit) { if (commit && inp.value !== (o.label || "")) { setLayer(i, "label", inp.value.slice(0, 40), false); } renderLayers(); }
    inp.addEventListener("keydown", function (e) { if (e.key === "Enter") { done(true); } else if (e.key === "Escape") { done(false); } e.stopPropagation(); });
    inp.addEventListener("blur", function () { done(true); });
    btn.replaceWith(inp); inp.focus(); inp.select();
  }
  function renderLayers() {
    var ul = $("#ws-layers"); ul.innerHTML = "";
    var arr = shown();
    for (var k = arr.length - 1; k >= 0; k--) {
      (function (i) {
        var o = arr[i], row = h("li", { class: "lrow" + (i === sel && view === "draft" ? " sel" : "") + (o.visible ? "" : " off"), "data-i": i });
        var eye = ibtn(o.visible ? "eye" : "eyeoff", o.visible ? "Hide layer" : "Show layer", function (e) { e.stopPropagation(); if (view !== "draft" || !canEdit) { return; } setLayer(i, "visible", !o.visible); renderLayers(); });
        eye.setAttribute("aria-pressed", o.visible ? "true" : "false");
        row.appendChild(eye); row.appendChild(h("span", { class: "lic" }, [icon(o.kind === "rect" ? "rect" : o.kind)]));
        var nm = h("button", { type: "button", class: "lname", text: layerName(o), title: "Click to select, double-click to rename", onclick: function () { if (sel !== i) { select(i); } },
          ondblclick: function () { renameLayer(i, nm); } });
        var lk = ibtn(o.locked ? "lock" : "unlock", o.locked ? "Unlock layer" : "Lock layer", function (e) { e.stopPropagation(); if (!canEdit) { return; } setLayer(i, "locked", !o.locked, true); renderCanvas(); renderLayers(); });
        lk.setAttribute("aria-pressed", o.locked ? "true" : "false");
        row.appendChild(nm); row.appendChild(lk);
        if (view === "draft" && canEdit) {
          var act = h("span", { class: "lact" }, [
            ibtn("up", "Move layer up (in front)", function () { moveLayer(i, 1); }), ibtn("down", "Move layer down (behind)", function () { moveLayer(i, -1); }),
            ibtn("pencil", "Rename layer", function () { renameLayer(i, nm); }), ibtn("copy", "Duplicate layer", function () { dupLayer(i); }), ibtn("trash", "Remove layer", function () { removeLayer(i); }, "danger")]);
          row.appendChild(act);
        }
        ul.appendChild(row);
      })(k);
    }
    var p = active(), base = h("li", { class: "lrow base" + (sel === -2 && view === "draft" ? " sel" : "") }, [
      h("span", { class: "ibtn ghost" }, [icon("lock")]), h("span", { class: "lic" }, [icon(p.source_kind === "audio" ? "audio" : p.source_kind === "slideshow" ? "slideshow" : p.source_kind === "playlist" ? "playlist" : p.source_kind === "relay" ? "stream" : "video")]),
      h("button", { type: "button", class: "lname", text: "Background: " + (p.source_ref ? (p.source_kind === "slideshow" ? p.slides_n || p.source_ref.split(",").length + " images" : p.source_ref) : "not chosen"),
        onclick: function () { if (view === "draft") { select(-2); } } })]);
    ul.appendChild(base);
    $("#ws-layercount").textContent = "(" + arr.length + ")";
  }

  // ---- properties -----------------------------------------------------------------
  var props = $("#ws-props");
  function syncInput(inp) {
    var key = inp.dataset.key, o = P.overlays[sel]; if (!o || !(key in o)) { return; }
    var v = o[key];
    if (inp.dataset.scale) { v = pct(v); }
    if (inp.type === "checkbox") { inp.checked = !!v; } else if (document.activeElement !== inp) { inp.value = v; }
    var rng = inp.nextElementSibling; if (rng && rng.dataset && rng.dataset.pair === key) { rng.value = v; }
  }
  function fld(label, control, hint) {
    return h("label", { class: "fld" }, [h("span", { class: "fl", text: label }), control, hint ? h("span", { class: "fh", text: hint }) : null]);
  }
  function numInput(key, opts) {
    var o = P.overlays[sel], scale = opts.scale ? 100 : 1;
    var inp = h("input", { type: "number", "data-key": key, "data-scale": opts.scale ? "1" : null, min: opts.min, max: opts.max, step: opts.step || 1, value: opts.scale ? pct(o[key]) : o[key], inputmode: "decimal" });
    inp.addEventListener("input", function () {
      var v = parseFloat(inp.value); if (isNaN(v)) { return; }
      var val = opts.scale ? v / 100 : v;
      if (opts.int) { val = Math.round(val); }
      setLayer(sel, key, val, false);
    });
    inp.addEventListener("change", function () { syncInput(inp); });
    return inp;
  }
  function textInput(key, opts) {
    var o = P.overlays[sel], inp = h(opts && opts.area ? "textarea" : "input", Object.assign({ type: "text", "data-key": key, value: o[key], maxlength: (opts && opts.max) || 500, rows: opts && opts.area ? 3 : null, spellcheck: "false", autocomplete: "off" }, (opts && opts.attrs) || {}));
    if (opts && opts.area) { inp.textContent = o[key]; inp.value = o[key]; }
    inp.addEventListener("input", function () { setLayer(sel, key, inp.value, false); });
    return inp;
  }
  function colorInput(key) {
    var o = P.overlays[sel];
    var c = h("input", { type: "color", value: /^#[0-9a-fA-F]{6}$/.test(o[key]) ? o[key] : "#ffffff", "aria-label": "Colour picker" });
    var t = h("input", { type: "text", "data-key": key, value: o[key], maxlength: 7, size: 8, spellcheck: "false", "aria-label": "Colour hex" });
    c.addEventListener("input", function () { t.value = c.value; setLayer(sel, key, c.value, false); });
    t.addEventListener("input", function () { if (/^#[0-9a-fA-F]{6}$/.test(t.value)) { c.value = t.value; setLayer(sel, key, t.value, false); } });
    return h("span", { class: "colorpair" }, [c, t]);
  }
  function section(title, kids) { return h("fieldset", { class: "psec" }, [h("legend", { text: title })].concat(kids)); }
  function row(kids) { return h("div", { class: "prow" }, kids); }
  function opacityControl() {
    var o = P.overlays[sel];
    var n = h("input", { type: "number", "data-key": "opacity", min: 0, max: 1, step: 0.05, value: o.opacity });
    var r = h("input", { type: "range", min: 0, max: 1, step: 0.01, value: o.opacity, "data-pair": "opacity", "aria-label": "Opacity" });
    n.addEventListener("input", function () { var v = parseFloat(n.value); if (!isNaN(v)) { r.value = v; setLayer(sel, "opacity", clamp(v, 0, 1), false); } });
    r.addEventListener("input", function () { n.value = r.value; setLayer(sel, "opacity", parseFloat(r.value), false); });
    return h("span", { class: "rangepair" }, [n, r]);
  }
  function mediaField(kind, key) {
    var o = P.overlays[sel], thumb = h("span", { class: "mthumb" }), name = h("span", { class: "mname", text: o[key] || "none chosen" });
    if (o[key]) { var im = h("img", { alt: "", src: (kind === "image" ? U.asset : U.thumb).replace("__A__", encodeURIComponent(o[key])) }); im.addEventListener("error", function () { im.remove(); }); thumb.appendChild(im); }
    var choose = h("button", { type: "button", class: "btn btn-sm", text: "Choose from Media Library", onclick: function () {
      StudioMedia.pick({ title: kind === "image" ? "Choose an image" : "Choose a video", kinds: [kind], urls: U, csrf: csrf, thumbs: settings.advanced.video_thumbs }).then(function (items) {
        if (items && items[0]) { setLayer(sel, key, items[0].name, true); }
      }); } });
    return h("div", { class: "mfield" }, [thumb, h("div", { class: "mcol" }, [name, choose])]);
  }

  function buildProps() {
    props.innerHTML = "";
    if (view === "program") { props.appendChild(h("p", { class: "muted pad", text: "Program view is read-only. Switch to Preview to edit the draft." })); return; }
    if (sel === -2 || !P.overlays[sel]) { return buildSourceProps(); }
    var o = P.overlays[sel], k = o.kind, ro = !canEdit;
    props.appendChild(h("div", { class: "phead" }, [h("span", { class: "lic" }, [icon(k)]), h("strong", { text: NAMES[k] || k }), h("span", { class: "muted", text: " layer " + (sel + 1) })]));
    props.appendChild(section("Layer", [fld("Name", textInput("label", { max: 40, attrs: { placeholder: NAMES[k] } }), "Shown in the layer list only.")]));
    var geo = [row([fld("X %", numInput("x", { scale: 1, min: 0, max: 100, step: 0.5 })), fld("Y %", numInput("y", { scale: 1, min: 0, max: 100, step: 0.5 }))]),
               row([fld("Width %", numInput("w", { scale: 1, min: 2, max: 100, step: 0.5 })), fld("Height %", LOCKED[k] ? (function () { var i = numInput("h", { scale: 1, min: 2, max: 100, step: 0.5 }); i.readOnly = true; i.title = "Follows the picture's shape"; return i; })() : numInput("h", { scale: 1, min: 2, max: 100, step: 0.5 }))])];
    var al = h("div", { class: "aligns" }, [["Left", function (o) { o.x = 0; }], ["Centre", function (o) { o.x = r4((1 - o.w) / 2); }], ["Right", function (o) { o.x = r4(1 - o.w); }],
      ["Top", function (o) { o.y = 0; }], ["Middle", function (o) { o.y = r4((1 - o.h) / 2); }], ["Bottom", function (o) { o.y = r4(1 - o.h); }]].map(function (a) {
      return h("button", { type: "button", class: "btn btn-sm btn-secondary", text: a[0], onclick: function () { a[1](o); paintLayer(sel); syncBox(); touch(); } }); }));
    geo.push(al);
    props.appendChild(section("Position and size", geo));
    props.appendChild(section("Appearance", [fld("Opacity", opacityControl())].concat(k === "image" || k === "video" || k === "browser" ? [] : [fld("Colour", colorInput("color"))])));
    if (TEXTY[k]) {
      var tf = [];
      if (k === "text" || k === "ticker" || k === "nowplaying" || k === "data") { tf.push(fld(k === "data" ? "Text (use {value})" : k === "nowplaying" ? "Prefix text" : "Text", textInput("text", { area: k === "text" || k === "ticker", max: 500 }))); }
      tf.push(fld("Size (px at output height)", numInput("size", { min: 8, max: 400, step: 1, int: true })));
      var fonts = S.fonts && S.fonts.length ? S.fonts : ["DejaVu Sans"];
      var fsel = h("select", { "data-key": "font" }, fonts.map(function (f) { return h("option", { value: f, text: f, selected: f === o.font }); }));
      fsel.addEventListener("change", function () { setLayer(sel, "font", fsel.value, false); });
      tf.push(fld("Font", fsel, "Fonts installed on the server. The live text is rendered with these."));
      tf.push(h("div", { class: "aligns", role: "group", "aria-label": "Text alignment" }, ["left", "center", "right"].map(function (a) {
        var b = h("button", { type: "button", class: "btn btn-sm " + (o.align === a ? "" : "btn-secondary"), text: a.charAt(0).toUpperCase() + a.slice(1), "aria-pressed": o.align === a ? "true" : "false",
          onclick: function () { setLayer(sel, "align", a, true); } }); return b; })));
      tf.push(fld("Background box colour", colorInput("bg_color")));
      tf.push(fld("Background opacity (0 = transparent)", (function () {
        var n2 = h("input", { type: "number", "data-key": "bg_opacity", min: 0, max: 1, step: 0.05, value: o.bg_opacity });
        n2.addEventListener("input", function () { var v = parseFloat(n2.value); if (!isNaN(v)) { setLayer(sel, "bg_opacity", clamp(v, 0, 1), false); } }); return n2; })()));
      props.appendChild(section("Text", tf));
    }
    if (k === "image" || k === "video") {
      props.appendChild(section(k === "image" ? "Image" : "Video", [mediaField(k, "asset"),
        k === "video" ? h("p", { class: "fh", text: "Plays looped and muted as a picture-in-picture. Costs a live encode." }) : null]));
      props.appendChild(section("Crop (percent of the source)", [row([fld("Left", numInput("crop_x", { scale: 1, min: 0, max: 95, step: 1 })), fld("Top", numInput("crop_y", { scale: 1, min: 0, max: 95, step: 1 }))]),
        row([fld("Width", numInput("crop_w", { scale: 1, min: 5, max: 100, step: 1 })), fld("Height", numInput("crop_h", { scale: 1, min: 5, max: 100, step: 1 }))])]));
    }
    if (k === "clock" || k === "countdown" || k === "countup" || k === "pomodoro") {
      var tz = textInput("tz", { max: 64, attrs: { list: "ws-tzs" } });
      var tkids = [fld("Time zone", tz, "e.g. UTC, Europe/London, America/New_York")];
      if (k !== "clock") {
        var ti = h("input", { type: "datetime-local", step: 1, "data-key": "target", value: o.target.slice(0, 19) });
        ti.addEventListener("input", function () { if (ti.value) { setLayer(sel, "target", ti.value.length === 16 ? ti.value + ":00" : ti.value, false); } });
        tkids.push(fld(k === "pomodoro" ? "Cycle starts at" : k === "countdown" ? "Counts down to" : "Counts up from", ti));
      }
      if (k === "countdown") {
        tkids.push(fld("When finished show", textInput("finished_text", { max: 100 })));
        var dur = { d: 0, h: 0, m: 10, s: 0 };
        function durInput(key, label, max) { var i = h("input", { type: "number", min: 0, max: max, value: dur[key], "aria-label": label }); i.addEventListener("input", function () { dur[key] = Math.max(0, parseInt(i.value, 10) || 0); }); return fld(label, i); }
        tkids.push(h("p", { class: "fh", text: "Or count down a fixed duration. The end time is stored as an exact moment on the server, so it stays correct if you close the browser or the stream restarts." }));
        tkids.push(row([durInput("d", "Days", 365), durInput("h", "Hours", 23), durInput("m", "Min", 59), durInput("s", "Sec", 59)]));
        tkids.push(h("div", { class: "aligns" }, [h("button", { type: "button", class: "btn btn-sm", text: "Start from now", onclick: function () {
          var secs = dur.d * 86400 + dur.h * 3600 + dur.m * 60 + dur.s; if (secs < 1) { toast("Enter a duration first.", "error"); return; }
          var t = new Date(Date.now() + secs * 1000), pad = function (v) { return (v < 10 ? "0" : "") + v; };
          var iso = t.getUTCFullYear() + "-" + pad(t.getUTCMonth() + 1) + "-" + pad(t.getUTCDate()) + "T" + pad(t.getUTCHours()) + ":" + pad(t.getUTCMinutes()) + ":" + pad(t.getUTCSeconds());
          setLayer(sel, "tz", "UTC", false); setLayer(sel, "target", iso, true); toast("Countdown set to end in " + secs + " s. Save, then Apply to Live to start it on air.", "info"); } }),
          h("button", { type: "button", class: "btn btn-sm btn-secondary", text: "Reset (same duration)", onclick: function () {
          var secs = dur.d * 86400 + dur.h * 3600 + dur.m * 60 + dur.s; if (secs < 1) { return; }
          var t = new Date(Date.now() + secs * 1000), pad = function (v) { return (v < 10 ? "0" : "") + v; };
          setLayer(sel, "tz", "UTC", false); setLayer(sel, "target", t.getUTCFullYear() + "-" + pad(t.getUTCMonth() + 1) + "-" + pad(t.getUTCDate()) + "T" + pad(t.getUTCHours()) + ":" + pad(t.getUTCMinutes()) + ":" + pad(t.getUTCSeconds()), true); } })]));
      }
      if (k === "pomodoro") { tkids.push(row([fld("Focus minutes", numInput("work_min", { min: 1, max: 600, int: true })), fld("Break minutes", numInput("break_min", { min: 1, max: 600, int: true }))])); }
      props.appendChild(section("Time", tkids));
    }
    if (k === "data") {
      var fm = h("select", { "data-key": "value_format" }, [["raw", "as is"], ["int", "whole number"], ["thousands", "1,234"], ["decimal1", "1 decimal"], ["percent", "percent"]].map(function (p) { return h("option", { value: p[0], text: p[1], selected: o.value_format === p[0] }); }));
      fm.addEventListener("change", function () { setLayer(sel, "value_format", fm.value, false); });
      props.appendChild(section("Data source", [fld("Public JSON URL", textInput("url", { max: 500 })), fld("JSON path", textInput("json_path", { max: 100 }), "e.g. items.0.count"),
        row([fld("Refresh (s)", numInput("refresh_s", { min: 30, max: 86400, int: true })), fld("Format", fm)]), h("p", { class: "fh", text: "Only public internet addresses are fetched; the last good value is kept on errors." })]));
    }
    if (k === "browser") {
      props.appendChild(section("Browser page", [fld("Page URL (https)", textInput("url", { max: 500 })), fld("CSS selector (optional)", textInput("selector", { max: 200 })),
        row([fld("Viewport width", numInput("view_w", { min: 320, max: 1920, int: true })), fld("Viewport height", numInput("view_h", { min: 240, max: 1080, int: true }))]),
        fld("Capture every (s)", numInput("refresh_s", { min: 15, max: 3600, int: true })), fld("Extra allowed hosts", textInput("allowed_hosts", { max: 200 }))]));
    }
    if (o.locked) { ["x", "y", "w", "h"].forEach(function (kk) { var f = $('[data-key="' + kk + '"]', props); if (f) { f.disabled = true; f.title = "Unlock the layer to change its position"; } }); props.insertBefore(h("p", { class: "fh pad", text: "This layer is locked: it cannot be moved or resized until you unlock it in the layer list." }), props.children[1] || null); }
    if (ro) { $$("input,select,textarea,button", props).forEach(function (n) { n.disabled = true; }); }
  }

  function buildSourceProps() {
    props.innerHTML = "";
    if (INPLACE) {
      var ipp = stream.inplace || {};
      props.appendChild(h("div", { class: "phead" }, [h("span", { class: "lic" }, [icon("stream")]), h("strong", { text: "Existing stream: " + P.target_relay })]));
      props.appendChild(section("What this edits", [
        h("p", { class: "fh", text: "You are adding overlays to the broadcast of '" + P.target_relay + "' itself. Its name, source, destinations and stream keys are not changed by Studio." }),
        h("p", { class: "fh", text: "Everything you do here is a DRAFT until you press Activate. While the overlays are off, the stream keeps its plain stream-copy mode (no encoding, almost no CPU)." }),
        h("p", { class: "fh", text: "The picture keeps the stream's own size and frame rate; its audio is passed through untouched." }),
        h("p", { class: "fh", text: ipp.marker === "on" ? "Overlays are ON AIR. Text changes apply live without interruption; moving or adding layers switches the picture source (a pause of about 2 seconds; destinations stay connected)." : "Overlays are not on air." })]));
      return;
    }
    var kindSel = h("select", {}, [["media", "Video file (loops)"], ["slideshow", "Image slideshow"], ["audio", "Audio file with background"], ["relay", "Existing BlueStream stream"], ["playlist", "Existing playlist"]].map(function (p) {
      return h("option", { value: p[0], text: p[1], selected: P.source_kind === p[0] }); }));
    props.appendChild(h("div", { class: "phead" }, [h("span", { class: "lic" }, [icon("lock")]), h("strong", { text: "Background source" })]));
    var body = [fld("Source type", kindSel), h("div", { id: "ws-srcbody" })];
    var sec = section("Source", body); props.appendChild(sec);
    kindSel.addEventListener("change", function () { chooseSource(kindSel.value, true).then(function (ok) { if (!ok) { kindSel.value = P.source_kind; } }); });
    var b = $("#ws-srcbody", sec), k = P.source_kind;
    if (k === "relay" || k === "playlist") {
      var names = S.sources[k] || [], s = h("select", {}, [h("option", { value: "", text: names.length ? "Choose..." : "No " + (k === "relay" ? "streams" : "playlists") + " available" })].concat(names.map(function (n) { return h("option", { value: n, text: n, selected: n === P.source_ref }); })));
      s.addEventListener("change", function () { if (s.value) { setSource(k, s.value); } });
      b.appendChild(fld(k === "relay" ? "Stream" : "Playlist", s));
    } else {
      b.appendChild(h("div", { class: "mfield" }, [h("span", { class: "mthumb" }, [P.source_ref && k !== "slideshow" && k !== "audio" ? h("img", { alt: "", src: U.thumb.replace("__A__", encodeURIComponent(P.source_ref)) }) : icon(k === "audio" ? "audio" : "slideshow", 24)]),
        h("div", { class: "mcol" }, [h("span", { class: "mname", text: P.source_ref ? (k === "slideshow" ? P.source_ref.split(",").length + " images" : P.source_ref) : "none chosen" }),
          h("button", { type: "button", class: "btn btn-sm", text: "Choose from Media Library", onclick: function () { chooseSource(k, false); } })])]));
      if (k === "slideshow") {
        b.appendChild(h("ol", { class: "slidelist" }, P.source_ref.split(",").filter(Boolean).map(function (n) { return h("li", { text: n }); })));
        b.appendChild(fld("Seconds per image", (function () { var i = h("input", { type: "number", min: 2, max: 3600, value: P.slide_seconds }); i.addEventListener("input", function () { var v = parseInt(i.value, 10); if (v) { P.slide_seconds = v; touch(); } }); return i; })()));
        b.appendChild(h("label", { class: "chk" }, [(function () { var c = h("input", { type: "checkbox", checked: P.slide_shuffle }); c.addEventListener("change", function () { P.slide_shuffle = c.checked; touch(); }); return c; })(), " Shuffle order (the same list always gives the same order)"]));
      }
      if (k === "audio") {
        var isCol = /^#[0-9a-fA-F]{6}$/.test(P.background), bgc = h("input", { type: "color", value: isCol ? P.background : "#0b1220", "aria-label": "Background colour" });
        bgc.addEventListener("input", function () { P.background = bgc.value; bgKey = ""; touch(); renderCanvas(); });
        b.appendChild(fld("Background colour", bgc));
        b.appendChild(h("button", { type: "button", class: "btn btn-sm btn-secondary", text: isCol ? "Use an image as background" : "Background image: " + P.background + " (change)", onclick: function () {
          StudioMedia.pick({ title: "Choose a background image", kinds: ["image"], urls: U, csrf: csrf }).then(function (it) { if (it && it[0]) { P.background = it[0].name; bgKey = ""; touch(); renderCanvas(); buildSourceProps(); } }); } }));
        b.appendChild(h("label", { class: "chk" }, [(function () { var c = h("input", { type: "checkbox", checked: P.visualizer }); c.addEventListener("change", function () { P.visualizer = c.checked; touch(); }); return c; })(), " Live waveform (needs a live encode)"]));
      }
    }
    var k2 = P.source_kind;
    if (k2 === "media" && P.source_ref) {
      var pi = playerInfo || {};
      props.appendChild(section("Playback (preview only)", [h("p", { class: "fh", text: "File: " + P.source_ref }),
        h("p", { class: "fh", text: "Size: " + (pi.width ? pi.width + "\u00d7" + pi.height : "unknown until it starts") + " \u00b7 State: " + (pi.state || "idle") + (pi.detail ? " (" + pi.detail + ")" : "") }),
        h("p", { class: "fh", text: "Use the controls under the canvas to pause, restart, loop or mute the preview. They never affect a live stream. The broadcast loops this file continuously." })]));
    }
    if (k2 === "relay" || k2 === "playlist") {
      var rw = hlsFor(k2, P.source_ref);
      props.appendChild(section("Monitoring (read-only)", [h("p", { class: "fh", text: rw ? ("'" + rw.name + "' is " + (rw.active ? "running" : "stopped") + ". Studio is only WATCHING it: nothing about it changes.") : "Choose a stream in the Live sources tab." }),
        h("p", { class: "fh", text: "Your overlays are burned into a separate Studio output stream, never into '" + P.source_ref + "'. To put them on air for its viewers: Create the Studio stream (Controls), start it, then use the explicit \u201cSwitch to this stream\u201d on the destination. Studio never does that for you." })]));
    }
    if (!canEdit) { $$("input,select,textarea,button", props).forEach(function (n) { n.disabled = true; }); }
  }

  /* Choose (or change to) a background source. Resolves true when the source was set. */
  function chooseSource(kind, fromTypeChange) {
    function replaceOk() {
      return (P.source_ref && !fromTypeChange) ? confirmDialog("Replace the background source?", "The current background (" + P.source_ref.split(",")[0] + ") will be replaced in the draft.", "Replace") : Promise.resolve(true);
    }
    if (kind === "relay" || kind === "playlist") {
      var names = S.sources[kind] || [];
      if (!names.length) { toast("There is no existing " + (kind === "relay" ? "stream" : "playlist") + " to use. Create one on the " + (kind === "relay" ? "Streams" : "Playlists") + " page first.", "error"); return Promise.resolve(false); }
      var s = h("select", {}, names.map(function (n) { return h("option", { value: n, text: n }); }));
      return dialog(kind === "relay" ? "Use an existing stream" : "Use an existing playlist", fld(kind === "relay" ? "Stream" : "Playlist", s),
        [{ label: "Cancel", value: null, secondary: true }, { label: "Use this", value: "ok" }]).then(function (v) { if (v === "ok") { setSource(kind, s.value); return true; } return false; });
    }
    var kinds = kind === "media" ? ["video"] : kind === "audio" ? ["audio"] : ["image"];
    return replaceOk().then(function (go) {
      if (!go) { return false; }
      return StudioMedia.pick({ title: kind === "slideshow" ? "Choose slideshow images" : kind === "audio" ? "Choose an audio file" : "Choose a background video", kinds: kinds, multi: kind === "slideshow", urls: U, csrf: csrf, thumbs: settings.advanced.video_thumbs }).then(function (items) {
        if (!items || !items.length) { return false; }
        if (kind === "slideshow") { setSource("slideshow", items.map(function (i) { return i.name; }).join(","), { slide_seconds: P.slide_seconds || 10 }); }
        else if (kind === "audio") { setSource("audio", items[0].name, { background: /^#/.test(P.background) ? P.background : "#0b1220" }); }
        else { setSource("media", items[0].name); }
        return true;
      });
    });
  }

  // ---- add source menu -----------------------------------------------------------
  function addPicked(kind) {
    StudioMedia.pick({ title: kind === "image" ? "Choose an image" : "Choose a video overlay", kinds: [kind], urls: U, csrf: csrf, thumbs: settings.advanced.video_thumbs }).then(function (items) {
      if (items && items[0]) { addLayer(kind, { asset: items[0].name, x: 0.35, y: 0.2, w: kind === "image" ? 0.3 : 0.25 }); }
    });
  }
  var MENU = [
    { g: "Media", items: [["video", "Video (picture-in-picture)", function () { addPicked("video"); }], ["image", "Image", function () { addPicked("image"); }],
      ["video", "Background video", function () { chooseSource("media"); }], ["slideshow", "Image slideshow (background)", function () { chooseSource("slideshow"); }]] },
    { g: "Graphics", items: [["text", "Text", function () { addLayer("text"); }], ["rect", "Colour block", function () { addLayer("rect"); }], ["clock", "Clock", function () { addLayer("clock"); }],
      ["countdown", "Countdown", function () { addLayer("countdown"); }], ["countup", "Countup", function () { addLayer("countup"); }], ["pomodoro", "Pomodoro timer", function () { addLayer("pomodoro"); }],
      ["ticker", "Ticker", function () { addLayer("ticker"); }], ["nowplaying", "Now Playing", function () { addLayer("nowplaying"); }], ["data", "Live data (JSON)", function () { addLayer("data"); }]] },
    { g: "Streaming", items: [["stream", "Existing BlueStream stream", function () { chooseSource("relay"); }], ["playlist", "Existing playlist", function () { chooseSource("playlist"); }],
      ["audio", "Audio file (with background)", function () { chooseSource("audio"); }]] }
  ];
  MENU.push({ g: "Advanced", items: [["browser", "Browser source", function () { addLayer("browser"); }, S.kinds.indexOf("browser") >= 0 ? null : (S.browser_gate || "Not available on this server.")]] });
  var menuBtn = $("#ws-add"), menu = $("#ws-menu");
  function buildMenu() {
    menu.innerHTML = "";
    MENU.forEach(function (grp) {
      menu.appendChild(h("div", { class: "mg", text: grp.g, role: "presentation" }));
      grp.items.forEach(function (it) {
        var off = !!it[3];
        menu.appendChild(h("button", { type: "button", role: "menuitem", class: "mi" + (off ? " off" : ""), "aria-disabled": off ? "true" : null, title: off ? it[3] : null,
          onclick: function () { if (off) { toast("Browser source is not available: " + it[3] + ".", "error"); return; } closeMenu(); it[2](); } }, [icon(it[0], 18), h("span", { text: it[1] + (off ? " (not available)" : "") })]));
      });
    });
    menu.appendChild(h("p", { class: "mnote", text: "Screen, window, game and camera capture are not offered: this server has no display or capture devices." }));
  }
  function closeMenu() { menu.hidden = true; menuBtn.setAttribute("aria-expanded", "false"); }
  function openMenu() { menu.hidden = false; menuBtn.setAttribute("aria-expanded", "true"); var f = $(".mi", menu); if (f) { f.focus(); } }
  menuBtn.addEventListener("click", function (e) { e.stopPropagation(); if (menu.hidden) { openMenu(); } else { closeMenu(); } });
  document.addEventListener("click", function (e) { if (!menu.hidden && !menu.contains(e.target)) { closeMenu(); } });
  menu.addEventListener("keydown", function (e) {
    var items = $$(".mi", menu), i = items.indexOf(document.activeElement);
    if (e.key === "Escape") { closeMenu(); menuBtn.focus(); } else if (e.key === "ArrowDown") { items[(i + 1) % items.length].focus(); e.preventDefault(); } else if (e.key === "ArrowUp") { items[(i - 1 + items.length) % items.length].focus(); e.preventDefault(); }
  });

  // ---- media panel (left) ---------------------------------------------------------
  var mediaCtl = null, mediaSel = [];
  function mediaActions(items) {
    var box = $("#ws-mactions"); box.innerHTML = ""; mediaSel = items;
    if (!items.length || !canEdit) { return; }
    var it = items[0], k = it.kind, cur = sel >= 0 ? P.overlays[sel] : null;
    box.appendChild(h("div", { class: "mname2", text: it.name + " · " + it.ext + " · " + it.size_text }));
    if (k === "image" || k === "video") {
      box.appendChild(h("button", { type: "button", class: "btn btn-sm", text: "Add as " + (k === "image" ? "image layer" : "video overlay"), onclick: function () { addLayer(k, { asset: it.name, x: 0.35, y: 0.2, w: k === "image" ? 0.3 : 0.25 }); } }));
      if (cur && cur.kind === k) { box.appendChild(h("button", { type: "button", class: "btn btn-sm btn-secondary", text: "Use for selected layer", onclick: function () { setLayer(sel, "asset", it.name, true); } })); }
    }
    if (k === "video") { box.appendChild(h("button", { type: "button", class: "btn btn-sm btn-secondary", text: "Use as background", onclick: function () { setSource("media", it.name); } })); }
    if (k === "audio") { box.appendChild(h("button", { type: "button", class: "btn btn-sm", text: "Use as audio source", onclick: function () { setSource("audio", it.name, { background: /^#/.test(P.background) ? P.background : "#0b1220" }); } })); }
    if (k === "image" && items.length >= 1) { box.appendChild(h("button", { type: "button", class: "btn btn-sm btn-secondary", text: "Use as slideshow background", onclick: function () { setSource("slideshow", items.map(function (i) { return i.name; }).join(","), {}); } })); }
  }
  function mountMedia() {
    mediaCtl = StudioMedia.mount($("#ws-media"), { urls: U, csrf: csrf, kinds: ["video", "image", "audio"], multi: false, thumbs: settings.advanced.video_thumbs, onSelect: mediaActions });
  }

  // ---- live sources (existing relays and playlists) ----------------------------------------------------------------
  function loadSources() {
    var box = $("#ws-sources"); if (!live.loaded) { box.textContent = "Loading live sources\u2026"; }
    return api(U.sources).then(function (d) {
      if (d && d.ok) {
        live = { relays: d.relays, playlists: d.playlists, loaded: true };
        S.sources = { relay: d.relays.map(function (r) { return r.name; }), playlist: d.playlists.map(function (r) { return r.name; }) };
        bgKey = ""; paintBackground();
      } else { live.error = (d && d.error) || "Live sources could not be listed."; }
      renderSources();
    });
  }
  function editExisting(relayName) {
    api(U.overlayNew, { relay: relayName }).then(function (d) {
      if (!d || !d.ok) { toast((d && d.error) || "Could not open that stream.", "error"); return; }
      if (dirty && !window.confirm("You have unsaved changes in this project. Leave without saving?")) { return; }
      dirty = false; window.location.href = U.projects + "/" + encodeURIComponent(d.name);
    });
  }
  function srcRow(kind, r) {
    var cur = P.source_kind === kind && P.source_ref === r.name;
    var row = h("div", { class: "srcrow" + (cur ? " cur" : "") }, [h("span", { class: "badge2 " + (r.active ? "ok" : "off"), text: r.active ? "RUNNING" : "STOPPED" }), h("span", { class: "sn", text: r.name })]);
    if (kind === "relay" && !INPLACE) {
      row.appendChild(h("button", { type: "button", class: "btn btn-sm", disabled: !canEdit, text: "Edit this stream", title: "Open it here with its picture and add overlays to ITS broadcast (nothing changes until you activate)",
        onclick: function () { editExisting(r.name); } }));
    }
    if (!INPLACE) {
      row.appendChild(h("button", { type: "button", class: "btn btn-sm btn-secondary", disabled: !canEdit || cur, text: cur ? "Current source" : (kind === "relay" ? "Source for a new stream" : "Use as source"),
        title: kind === "relay" ? "Make a SEPARATE Studio stream that reads this one" : "", onclick: function () { setSource(kind, r.name); } }));
    }
    return row;
  }
  function renderSources() {
    var box = $("#ws-sources"); box.innerHTML = "";
    if (live.error) { box.appendChild(h("p", { class: "smb-status err", text: live.error })); }
    box.appendChild(h("p", { class: "muted", text: "Choose what Studio reads. Watching a stream is read-only: it is not restarted or changed. Your overlays are published on a separate Studio output stream." }));
    box.appendChild(h("h3", { class: "ws-h3", text: "Live streams" }));
    if (!live.relays.length) { box.appendChild(h("p", { class: "muted", text: live.loaded ? "No other streams exist yet." : "" })); }
    live.relays.forEach(function (r) { box.appendChild(srcRow("relay", r)); });
    box.appendChild(h("h3", { class: "ws-h3", text: "Playlists" }));
    if (!live.playlists.length) { box.appendChild(h("p", { class: "muted", text: live.loaded ? "No playlists yet." : "" })); }
    live.playlists.forEach(function (r) { box.appendChild(srcRow("playlist", r)); });
    box.appendChild(h("button", { type: "button", class: "btn btn-sm btn-secondary", disabled: !canEdit, text: "Create a playlist from Media Library videos\u2026", onclick: createPlaylist }));
    box.appendChild(h("p", { class: "fh", text: "Playlists play their videos in order and loop (the Playlists page has start/stop and scheduling). Studio reads a running playlist like any other live stream." }));
  }
  function createPlaylist() {
    StudioMedia.pick({ title: "Choose at least two videos for the playlist", kinds: ["video"], multi: true, urls: U, csrf: csrf, thumbs: settings.advanced.video_thumbs }).then(function (items) {
      if (!items || items.length < 2) { if (items) { toast("A playlist needs at least two videos.", "error"); } return; }
      var order = items.map(function (i) { return i.name; }), nameIn = h("input", { type: "text", maxlength: 48, placeholder: "my-playlist", "aria-label": "Playlist name" }), list = h("ol", { class: "plist" });
      function draw() {
        list.innerHTML = "";
        order.forEach(function (n, i) {
          list.appendChild(h("li", {}, [h("span", { class: "pn", text: n }), ibtn("up", "Move earlier", function () { if (i > 0) { var t = order[i - 1]; order[i - 1] = order[i]; order[i] = t; draw(); } }),
            ibtn("down", "Move later", function () { if (i < order.length - 1) { var t = order[i + 1]; order[i + 1] = order[i]; order[i] = t; draw(); } }), ibtn("trash", "Remove", function () { order.splice(i, 1); draw(); }, "danger")]));
        });
      }
      draw();
      dialog("New playlist", h("div", {}, [fld("Name", nameIn, "Letters, digits, - and _ only."), h("p", { class: "fh", text: "Order = playback order. The playlist loops. Files are checked when the playlist starts." }), list]),
        [{ label: "Cancel", value: null, secondary: true }, { label: "Create playlist", value: "ok", validate: function () { if (order.length < 2) { toast("A playlist needs at least two videos.", "error"); return false; } return true; } }]).then(function (v) {
        if (v !== "ok") { return; }
        api(U.playlistCreate, { name: nameIn.value.trim(), items: order }).then(function (d) { toast(d.ok ? d.message : (d.error || "Playlist not created."), d.ok ? "success" : "error"); if (d.ok) { loadSources(); } });
      });
    });
  }
  $$("[data-ltab]").forEach(function (b) {
    b.addEventListener("click", function () {
      var t = b.dataset.ltab; $$("[data-ltab]").forEach(function (x) { x.classList.toggle("on", x === b); x.setAttribute("aria-selected", x === b ? "true" : "false"); });
      $("#ws-media").hidden = t !== "media"; $("#ws-mactions").hidden = t !== "media"; $("#ws-sources").hidden = t !== "sources";
      if (t === "sources") { loadSources(); }
    });
  });

  // ---- live text previews ---------------------------------------------------------
  var textTimer = null;
  function queueTexts() { clearTimeout(textTimer); textTimer = setTimeout(refreshTexts, 350); }
  function refreshTexts() {
    if (!P.overlays.some(function (o) { return TEXTY[o.kind] && o.kind !== "text" && o.kind !== "ticker"; })) { return; }
    api(U.previewText, { layers: P.overlays }).then(function (d) {
      if (d && d.ok) { texts = d.texts; P.overlays.forEach(function (o, i) { if (TEXTY[o.kind]) { paintLayer(i); } }); }
    });
  }

  // ---- save / apply -----------------------------------------------------------------
  function setBusy(b) { busy = b; $("#ws-save").disabled = b || !dirty || !canEdit; $("#ws-apply").disabled = b || !canEdit; }
  function save() {
    if (!canEdit) { return Promise.resolve(false); }
    setBusy(true);
    var body = clone(P); delete body.revision;
    return api(U.save, { project: body }).then(function (d) {
      setBusy(false);
      if (!d.ok) { toast(d.error || "Not saved.", "error"); return false; }
      saved = snapshot(); unpublished = d.unpublished; capacity = d.capacity; missing = d.missing || []; touch(); renderStatus();
      toast(d.message, "success"); if (missing.length) { toast("Missing media: " + missing.map(function (m) { return m.name; }).join(", ") + ". Replace it before applying.", "error"); }
      return true;
    });
  }
  function applyLive() {
    if (!canEdit) { return; }
    Promise.resolve(dirty ? save() : true).then(function (ok) {
      if (!ok) { return; }
      setBusy(true);
      return api(U.publish, { restart: false }).then(function (d) {
        if (d.ok) { return d; }
        if (!d.needs_restart) { return d; }
        setBusy(false);
        var ask = settings.general.confirm_apply;
        return (ask ? confirmDialog(d.kind === "swap" ? "Switch the picture on air" : d.kind === "unit-restart" ? "One-time restart needed" : "This changes the video pipeline",
          d.error || "Applying restarts this Studio stream (viewers see a short interruption, about 10-20 s). Only this stream restarts; every other stream is untouched. If the new pipeline does not become healthy, the previous layout is put back automatically.",
          d.kind === "swap" ? "Switch now" : "Apply and restart", true) : Promise.resolve(true)).then(function (go) {
          if (!go) { return { cancelled: true }; }
          setBusy(true);
          return api(U.publish, { restart: true });
        });
      }).then(function (d) {
        setBusy(false);
        if (!d || d.cancelled) { return; }
        if (!d.ok) { toast(d.error || "Apply failed.", "error"); return; }
        published = clone(P); published.revision = d.revision; unpublished = false; toast(d.message, "success"); renderStatus(); pollStatus();
        if (d.mode === "restart" || d.mode === "swap" || d.mode === "unit-restart") { verifyRestart(d); }
      });
    });
  }
  /* After a staged restart: wait for the NEW revision to run healthily; otherwise put the previous one back. */
  function verifyRestart(d) {
    var tries = 0, banner = toastSticky("Restarting the Studio stream with revision " + d.revision + "\u2026");
    (function check() {
      tries++;
      api(U.status).then(function (r) {
        if (r && r.ok) { stream = Object.assign({ exists: false, active: false, publishers: [] }, r.stream); renderStatus(); }
        var okNow = stream.active && String(stream.revision) === String(d.revision) && (stream.state === "HEALTHY" || stream.state === "DEGRADED");
        if (okNow) { banner.remove(); toast("Revision " + d.revision + " is running (" + stream.plan + "). Output healthy.", "success"); return; }
        if (tries >= (window.__bsVerifyTries || 22)) {
          banner.remove();
          if (!d.previous) { toast("The new pipeline did not become healthy and there is no earlier revision to restore. Check the Streams page.", "error"); return; }
          api(U.revert, { revision: d.previous }).then(function (rv) {
            toast(rv.ok ? "The new pipeline did not become healthy within 45 s. The previous layout was put back on air automatically." : ("Automatic rollback failed: " + (rv.error || "see the Streams page") + "."), rv.ok ? "error" : "error");
            pollStatus();
          });
          return;
        }
        setTimeout(check, window.__bsVerifyMs || 2000);
      });
    })();
  }
  function toastSticky(msg) { var t = h("div", { class: "toast info", role: "status", text: msg }); toastBox.appendChild(t); return t; }

  // ---- stream status and controls ----------------------------------------------------
  function badgeClass(s) { return { HEALTHY: "ok", LIVE: "ok", DEGRADED: "warn", STARTING: "warn", RECONNECTING: "warn", STALLED: "bad", FAILED: "bad", BLOCKED: "bad" }[s] || "off"; }
  function renderStatus() {
    var p = P.profile, box = $("#ws-status"); box.innerHTML = "";
    var state = !stream.exists ? "NO STREAM" : stream.active ? (stream.state || "RUNNING") : "STOPPED";
    box.appendChild(h("div", { class: "srow" }, [h("span", { class: "badge2 " + (stream.exists && stream.active ? badgeClass(stream.state || "HEALTHY") : "off"), text: state }),
      h("span", { class: "muted", text: stream.detail || (stream.exists ? "" : "Press Create Stream to make this project streamable.") })]));
    var chain = h("ul", { class: "chain", "aria-label": "From your edit to the picture viewers see" });
    function link(ok, label, detail) { chain.appendChild(h("li", { class: ok === true ? "ok" : ok === false ? "no" : "wait" }, [h("strong", { text: label }), h("span", { text: " " + detail })])); }
    link(!dirty, "Draft", dirty ? "has unsaved edits" : "saved");
    var pubRev = published ? published.revision : 0;
    link(!unpublished && pubRev > 0, "Applied", pubRev ? (unpublished ? "revision " + pubRev + " is live; your draft differs" : "revision " + pubRev) : "nothing applied yet");
    if (INPLACE) { inplaceChain(link, pubRev); }
    else if (!stream.exists) { link(null, "Pipeline", "no Studio stream yet"); }
    else if (!stream.active) { link(null, "Pipeline", "stream stopped"); }
    else { var running = stream.revision ? "running revision " + stream.revision + (stream.plan ? " (" + stream.plan + ")" : "") : "starting";
      link(!stream.pending_restart && (!pubRev || String(stream.revision) === String(pubRev)), "Pipeline", running + (stream.pending_restart ? " - applied revision " + pubRev + " is waiting for a restart" : "")); }
    link(stream.exists && stream.active ? (stream.state === "HEALTHY") : null, "Output", stream.exists && stream.active ? (stream.state || "starting") + (stream.speed ? ", encoder " + stream.speed + "x" : "") : "not on air");
    box.appendChild(chain);
    var mb = $('[data-mon="output"]'); if (mb) { mb.disabled = !(stream.active && stream.hls_url); if (mb.disabled && mon === "output") { mon = "source"; $$("[data-mon]").forEach(function (x) { x.setAttribute("aria-pressed", x.dataset.mon === "source" ? "true" : "false"); }); bgKey = ""; renderCanvas(); renderLayers(); } }
    var live = P.overlays.some(function (o) { return o.visible && DYNAMIC[o.kind]; }) || (P.source_kind === "audio" && P.visualizer);
    var tb = Math.round((p.video_kbps + p.audio_kbps) * 1000 * 86400 * 30 / 8 / 1e10) / 100;
    var kv = [["Encoder", "x264 · " + p.preset], ["Output", p.width + "×" + p.height + " @ " + p.fps + " fps"], ["Bitrate", p.video_kbps + " kbps video / " + p.audio_kbps + " kbps audio"],
      ["Pipeline", live ? "Live composition (changing overlays need one live encode)" : P.overlays.some(function (o) { return o.visible; }) ? "Pre-render, then stream copy" : "Direct copy when compatible"],
      ["Transfer", "about " + tb + " TB / 30 days per destination"]];
    if (stream.exists && stream.active) {
      kv = kv.concat([["Encoder speed", (stream.speed ? stream.speed + "x (1.00x = real time)" : "-")], ["Frame rate", stream.fps || "-"], ["Encoder CPU", stream.cpu_cores ? stream.cpu_cores + " cores" : "-"],
        ["Encoder RAM", stream.rss_mb ? stream.rss_mb + " MB" : "-"], ["Dropped frames", stream.dropped || "0"], ["Restarts", stream.restarts || "0"], ["Output (HLS)", (stream.health || "-") + (stream.health_detail ? " " + stream.health_detail : "")]]);
    }
    var dl = h("dl", { class: "kv" }); kv.forEach(function (r) { dl.appendChild(h("dt", { text: r[0] })); dl.appendChild(h("dd", { text: r[1] })); }); box.appendChild(dl);
    if (stream.last_failure) { box.appendChild(h("p", { class: "cap warn", text: "Last failure: " + stream.last_failure })); }
    if (capacity && capacity.level !== "ok") { box.appendChild(h("p", { class: "cap " + capacity.level, text: capacity.message })); }
    var pubs = h("div", { class: "pubs" });
    if (stream.publishers.length) {
      stream.publishers.forEach(function (d) {
        var r = h("div", { class: "pub" }, [h("span", { class: "badge2 " + badgeClass(d.state), text: d.state || "-" }), h("span", { text: d.display_name }), d.detail ? h("span", { class: "muted", text: d.detail + (d.blocked_by ? " (" + d.blocked_by + ")" : "") }) : null]);
        if (d.state === "BLOCKED" && canEdit) { r.appendChild(h("button", { type: "button", class: "btn btn-sm", text: "Switch to this stream", onclick: function () { takeover(d); } })); }
        pubs.appendChild(r);
      });
    }
    else if (stream.exists) { pubs.appendChild(h("p", { class: "muted", text: "No destinations attached. Open Settings › Stream to attach one." })); }
    box.appendChild(pubs);
    // controls
    var c = $("#ws-ctl"); c.innerHTML = "";
    if (INPLACE) { inplaceControls(c); }
    else if (!stream.exists) { c.appendChild(h("button", { type: "button", class: "btn ctl", disabled: !canEdit, onclick: createStream }, [icon("plus"), " Create Stream"])); }
    else if (stream.active) { c.appendChild(h("button", { type: "button", id: "ws-stop", class: "btn btn-danger ctl", disabled: !canEdit, onclick: stopStream }, [icon("stop"), " Stop Streaming"])); }
    else { c.appendChild(h("button", { type: "button", id: "ws-start", class: "btn ctl go", disabled: !canEdit, onclick: startStream }, [icon("play"), " Start Streaming"])); }
    c.appendChild(h("button", { type: "button", class: "btn btn-secondary ctl", onclick: function () { openSettings("stream"); } }, [icon("gear"), " Settings"]));
    if (!INPLACE && !stream.exists) { c.appendChild(h("p", { class: "fh", text: "Source (left) is what Studio reads; the output stream is what viewers see. Creating it never changes the source stream." })); }
    c.appendChild(h("p", { class: "fh", text: unpublished ? "Unapplied changes: the stream keeps using the last applied revision." : "Draft matches the applied revision." }));
    var apply = $("#ws-apply"); apply.classList.toggle("pending", !!unpublished);
    if (INPLACE) { apply.textContent = (stream.inplace && stream.inplace.marker === "on") ? "Apply to Live" : "Save as ready"; apply.title = (stream.inplace && stream.inplace.marker === "on") ? "Apply the saved draft to the overlays that are on air" : "Save the layout as ready. It is NOT on air until you press Activate."; }
    var banner = $("#ws-inplace");
    if (banner) {
      banner.hidden = !INPLACE;
      if (INPLACE) {
        var onAir = stream.inplace && stream.inplace.marker === "on";
        banner.className = "ws-inplace " + (onAir ? "air" : "draft");
        banner.textContent = onAir ? "EDITING '" + P.target_relay + "' \u00b7 overlays are ON AIR. Changes here are drafts until you apply them." :
          "EDITING '" + P.target_relay + "' \u00b7 DRAFT ONLY. Nothing is on air until you press Activate; the stream is unchanged.";
      }
    }
  }
  function inplaceChain(link, pubRev) {
    var ip = stream.inplace || { marker: "none", swap: {} }, comp = stream.active && stream.plan === "overlay-live-composition";
    if (ip.marker === "on") {
      link(comp, "On air", comp ? "overlays are on air (revision " + stream.revision + ")" : ip.overlay === "tripped" ? "SWITCHED OFF automatically: the compositor kept failing and the plain stream was restored" : stream.active ? "switch is on; the stream is changing over\u2026" : "switch is on; overlays start with the stream");
    } else { link(null, "On air", "no overlays: the stream is unchanged (stream copy)"); }
    link(stream.exists ? (stream.active ? true : null) : false, "Stream '" + P.target_relay + "'", !stream.exists ? "not found" : stream.active ? (comp ? "encoding with overlays" : "running in stream-copy mode") : "stopped");
    link(stream.active ? stream.state === "HEALTHY" || (!comp && stream.health === "HEALTHY") : null, "Output", stream.active ? (comp ? (stream.state || "starting") + (stream.speed ? ", encoder " + stream.speed + "x" : "") : (stream.health || "running")) : "not on air");
  }
  function inplaceControls(c) {
    var ip = stream.inplace || { marker: "none", swap: {} }, on = ip.marker === "on", relay = P.target_relay;
    c.appendChild(h("div", { class: "srow" }, [h("span", { class: "badge2 " + (on ? "bad" : "off"), text: on ? "OVERLAYS ON AIR" : "DRAFT ONLY" })]));
    if (on) { c.appendChild(h("button", { type: "button", class: "btn btn-danger ctl", disabled: !canEdit, onclick: function () { switchOverlays(false); } }, [icon("stop"), " Remove overlays"])); }
    else { c.appendChild(h("button", { type: "button", id: "ws-activate", class: "btn ctl go", disabled: !canEdit, onclick: function () { switchOverlays(true); } }, [icon("play"), " Activate overlays on '" + relay + "'"])); }
    c.appendChild(h("button", { type: "button", class: "btn btn-secondary ctl", onclick: function () { openSettings("output"); } }, [icon("gear"), " Output settings"]));
    c.appendChild(h("p", { class: "fh", text: ip.swap && ip.swap.ready ? "Live switching is available: the destination connections stay up while the picture is replaced (a pause of about 2 s)." :
      ip.swap && ip.swap.reason === "OLD_SUPERVISOR" ? "'" + relay + "' was started before this update: the FIRST activation needs one restart (you are asked first). Later changes will not." :
      ip.swap && ip.swap.reason === "NOT_RUNNING" ? "'" + relay + "' is stopped: overlays will be on air when it starts." : "" }));
    c.appendChild(h("p", { class: "fh", text: "Start and stop '" + relay + "' on the Streams page. Its destinations and keys are managed on the Destinations page." }));
  }
  /* Activate / remove overlays on the existing stream. Saves and applies first so what goes on air is exactly what you see; any failure
     restores the previous state on the server, and a compositor that cannot stay up is switched off automatically (plain stream restored). */
  function switchOverlays(on) {
    if (!canEdit) { return; }
    Promise.resolve(dirty ? save() : true).then(function (ok) {
      if (!ok) { return; }
      return Promise.resolve(on && unpublished ? api(U.publish, { restart: false }).then(function (d) { if (d.ok) { published = clone(P); published.revision = d.revision; unpublished = false; } return d.ok ? true : (toast(d.error || "Could not save the layout as ready.", "error"), false); }) : true);
    }).then(function (ok) {
      if (!ok) { return; }
      return api(on ? U.overlayActivate : U.overlayDeactivate, {}).then(function (d) {
        if (d.ok || !d.needs_restart) { return d; }
        return confirmDialog("One-time restart needed", d.error, "Restart '" + P.target_relay + "' and continue", true).then(function (go) { return go ? api(on ? U.overlayActivate : U.overlayDeactivate, { confirm_restart: true }) : { cancelled: true }; });
      }).then(function (d) {
        if (!d || d.cancelled) { return; }
        if (!d.ok) { toast(d.error || "That did not complete.", "error"); pollStatus(); return; }
        toast(d.message, "success");
        var t0 = 0, banner = toastSticky(on ? "Putting the overlays on air\u2026" : "Removing the overlays\u2026");
        (function check() {
          t0++;
          api(U.status).then(function (r) {
            if (r && r.ok) { stream = Object.assign({ exists: false, active: false, publishers: [] }, r.stream); renderStatus(); bgKey = ""; renderCanvas(); }
            var comp = stream.plan === "overlay-live-composition" && stream.active, ipo = (stream.inplace || {}).overlay;
            if (on && comp && (stream.state === "HEALTHY" || stream.state === "DEGRADED")) { banner.remove(); toast("The overlays are on air.", "success"); return; }
            if (on && ipo === "tripped") { banner.remove(); toast("The compositor could not stay up, so the overlays were switched off automatically and the plain stream was restored. The stream itself was not interrupted for good.", "error"); return; }
            if (!on && stream.active && !comp) { banner.remove(); toast("The overlays are off: '" + P.target_relay + "' is back in stream-copy mode.", "success"); return; }
            if (t0 >= (window.__bsVerifyTries || 22)) { banner.remove(); toast("Still waiting for '" + P.target_relay + "' to change over. Check its state on the Streams page.", "error"); return; }
            setTimeout(check, window.__bsVerifyMs || 2000);
          });
        })();
      });
    });
  }
  function streamAction(action, okMsg) {
    return api(U.stream, { action: action }).then(function (d) {
      if (!d.ok) { toast(d.error || "That did not complete.", "error"); } else { toast(d.message || okMsg, "success"); }
      return pollStatus().then(function () { return d.ok; });
    });
  }
  function takeover(d) {
    confirmDialog("Switch destination", "Switch '" + d.display_name + "' to this stream? The stream currently using it (" + (d.blocked_by || "another stream") + ") stops publishing to it.", "Switch", true).then(function (go) {
      if (!go) { return; }
      api(U.takeover, { destination: d.id, confirm: true }).then(function (r) { toast(r.ok ? r.message : (r.error || "The switch did not complete."), r.ok ? "success" : "error"); pollStatus(); });
    });
  }
  function createStream() { Promise.resolve(dirty ? save() : true).then(function (ok) { if (ok) { streamAction("create").then(function (done) { if (done) { stream.exists = true; renderStatus(); } }); } }); }
  function startStream() { streamAction("start"); }
  function stopStream() {
    (settings.general.confirm_stop ? confirmDialog("Stop streaming", "Viewers will see the stream end. Stop this stream now?", "Stop streaming", true) : Promise.resolve(true)).then(function (go) { if (go) { streamAction("stop"); } });
  }
  var pollTimer = null;
  function pollStatus() {
    return api(U.status).then(function (d) {
      if (d && d.ok) {
        stream = Object.assign({ exists: false, active: false, publishers: [] }, d.stream); unpublished = d.unpublished; if (d.capacity) { capacity = d.capacity; }
        renderStatus();
      } else if (d && d.error && !stream.error) { stream.error = d.error; }
    });
  }
  function schedulePoll() {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(function () { (document.hidden ? Promise.resolve() : pollStatus()).then(schedulePoll); }, Math.max(2, settings.advanced.status_poll_s) * (stream.active ? 1 : 2) * 1000);   // every poll costs privileged bridge calls: idle streams half as often
  }
  var textPoll = null;
  function scheduleTextPoll() { clearInterval(textPoll); textPoll = setInterval(function () { if (!document.hidden) { refreshTexts(); } }, Math.max(2, settings.advanced.text_preview_s) * 1000); }

  // ---- settings dialog ------------------------------------------------------------------
  var TABS = [["general", "General"], ["stream", "Stream"], ["output", "Output"], ["audio", "Audio"], ["video", "Video"], ["hotkeys", "Hotkeys"], ["advanced", "Advanced"]];
  var HOTKEY_LABELS = { save: "Save draft", apply: "Apply to Live", duplicate: "Duplicate layer", delete: "Remove layer", toggle_visible: "Show / hide layer", layer_up: "Bring layer forward", layer_down: "Send layer backward" };
  function openSettings(start) {
    var draftSettings = clone(settings), draftProfile = clone(P.profile), destSel = null, dests = null, tab = start || "general";
    var wrap = h("div", { class: "sset" }), nav = h("div", { class: "sset-nav", role: "tablist" }), pane = h("div", { class: "sset-pane", role: "tabpanel" });
    wrap.appendChild(nav); wrap.appendChild(pane);
    function chk(sec, key, label) { var c = h("input", { type: "checkbox", checked: draftSettings[sec][key] }); c.addEventListener("change", function () { draftSettings[sec][key] = c.checked; }); return h("label", { class: "chk" }, [c, " " + label]); }
    function selOf(opts, cur, cb) { var s = h("select", {}, opts.map(function (o) { return h("option", { value: o[0], text: o[1], selected: String(o[0]) === String(cur) }); })); s.addEventListener("change", function () { cb(s.value); }); return s; }
    var capNote = h("p", { class: "cap", "aria-live": "polite" }); var capTimer = null;
    function assess() {
      clearTimeout(capTimer); capTimer = setTimeout(function () { api(U.assess, { profile: draftProfile }).then(function (d) {
        if (d && d.ok) { capNote.className = "cap " + d.capacity.level; capNote.textContent = d.capacity.message; } else if (d) { capNote.className = "cap bad"; capNote.textContent = d.error || ""; } }); }, 250);
    }
    function numField(label, get, set, min, max, step, hint) {
      var i = h("input", { type: "number", min: min, max: max, step: step || 1, value: get() }); i.addEventListener("input", function () { var v = parseInt(i.value, 10); if (!isNaN(v)) { set(v); assess(); } });
      return fld(label, i, hint);
    }
    function render() {
      nav.innerHTML = ""; pane.innerHTML = "";
      TABS.forEach(function (t) { nav.appendChild(h("button", { type: "button", role: "tab", class: "sset-tab" + (t[0] === tab ? " on" : ""), "aria-selected": t[0] === tab ? "true" : "false", text: t[1], onclick: function () { tab = t[0]; render(); } })); });
      var caps = S.capabilities;
      if (tab === "general") {
        pane.appendChild(section("Interface", [fld("Theme", selOf([["dark", "Dark"], ["light", "Light"]], draftSettings.general.theme, function (v) { draftSettings.general.theme = v; })),
          chk("general", "confirm_stop", "Ask before stopping a stream"), chk("general", "confirm_apply", "Ask before applying changes to a running stream")]));
        pane.appendChild(section("Source alignment snapping", [chk("general", "snap_enabled", "Enable"),
          fld("Snap sensitivity", (function () { var r = h("input", { type: "number", min: 1, max: 40, value: draftSettings.general.snap_sensitivity }); r.addEventListener("input", function () { var v = parseInt(r.value, 10); if (v) { draftSettings.general.snap_sensitivity = v; } }); return r; })(), "1-40 (about this many pixels on a 1280 px wide preview)"),
          chk("general", "snap_edges", "Snap sources to the edge of the canvas"), chk("general", "snap_sources", "Snap sources to other sources"), chk("general", "snap_center", "Snap sources to horizontal and vertical centre")]));
        pane.appendChild(section("Preview", [chk("general", "safe_areas", "Draw safe areas (EBU R 95)"), chk("general", "show_grid", "Show a thirds grid")]));
      } else if (tab === "stream") {
        pane.appendChild(h("p", { class: "muted", text: "Streams publish to the destinations you manage on the Destinations page. Stream keys are stored on the server and are never shown here." }));
        var area = h("div", { class: "destlist", "aria-live": "polite" }); pane.appendChild(area);
        pane.appendChild(h("p", {}, [h("a", { class: "btn btn-sm btn-secondary", href: U.destinationsList, text: "Manage destinations (add YouTube RTMP / RTMPS)" })]));
        if (!stream.exists) { area.appendChild(h("p", { class: "muted", text: "Create the stream first (Create Stream in the controls), then attach destinations here." })); }
        else if (dests) { listDests(area); }
        else { area.textContent = "Loading destinations..."; api(U.destinations).then(function (d) { if (d.ok) { dests = d; destSel = d.attached.slice(); area.innerHTML = ""; listDests(area); } else { area.textContent = d.error || "Destinations could not be read."; } }); }
      } else if (tab === "output") {
        var enc = h("select", { "aria-label": "Encoder" }), encNote = h("p", { class: "fh", "aria-live": "polite" }), testBtn = h("button", { type: "button", class: "btn btn-sm btn-secondary", text: "Test NVIDIA encoding" });
        function fillEnc(list) {
          enc.innerHTML = "";
          list.forEach(function (e) {
            enc.appendChild(h("option", { value: e.id, text: e.label + (e.untested ? " (not tested yet)" : e.selectable ? "" : " (unavailable)"), disabled: !e.selectable && e.id !== draftProfile.encoder, selected: e.id === draftProfile.encoder }));
          });
          encNote.textContent = list.filter(function (e) { return !e.selectable; }).map(function (e) { return e.label + ": " + e.why; }).join(" ") + " Stream copy is used automatically when no overlays or conversion are needed.";
        }
        fillEnc(caps.encoders || []);
        enc.addEventListener("change", function () { draftProfile.encoder = enc.value; assess(); });
        testBtn.addEventListener("click", function () {
          testBtn.disabled = true; testBtn.textContent = "Testing (a few seconds)\u2026";
          api(U.capabilities + "?probe=1").then(function (d) { testBtn.disabled = false; testBtn.textContent = "Test NVIDIA encoding"; if (d && d.ok) { caps.encoders = d.encoders; fillEnc(d.encoders); toast(d.encoders.filter(function (e) { return e.id === "h264_nvenc"; })[0].selectable ? "NVENC works on this server." : "NVENC is not usable here: see the note.", "info"); } });
        });
        pane.appendChild(section("Streaming", [fld("Output mode", h("input", { value: "Simple", disabled: true })), fld("Encoder", enc), encNote, testBtn,
          fld("Encoder preset (software only)", selOf([["ultrafast", "ultrafast (lowest CPU)"], ["superfast", "superfast"], ["veryfast", "veryfast (default)"], ["faster", "faster (better quality)"]], draftProfile.preset, function (v) { draftProfile.preset = v; assess(); })),
          numField("Video bitrate (kbps)", function () { return draftProfile.video_kbps; }, function (v) { draftProfile.video_kbps = v; }, 1000, 12000, 100, "1000-12000. YouTube recommends 4500 kbps for 720p30 and 9000 kbps for 1080p30."), capNote]));
        pane.appendChild(h("p", { class: "muted", text: "This server: " + caps.cores + " CPU cores" + (caps.mem_mb ? ", " + Math.round(caps.mem_mb / 102.4) / 10 + " GB RAM" : "") + (caps.load != null ? ", load " + caps.load : "") + "." }));
        assess();
      } else if (tab === "audio") {
        pane.appendChild(section("Audio", [fld("Audio bitrate (kbps)", selOf([64, 96, 128, 160, 192, 256, 320].map(function (b) { return [b, b + " kbps"]; }), draftProfile.audio_kbps, function (v) { draftProfile.audio_kbps = parseInt(v, 10); })),
          h("p", { class: "muted", text: "Output audio is AAC, 44.1 kHz stereo. A source without sound gets a silent track so platforms accept the stream. There is no mixer: Studio plays the source's own audio." })]));
      } else if (tab === "video") {
        pane.appendChild(section("Canvas and output", [fld("Output resolution (canvas)", selOf((INPLACE ? ["0x0"] : []).concat(caps.resolutions || []).map(function (r) { return [r, r === "0x0" ? "Same as the stream (recommended)" : r.replace("x", " × ")]; }), draftProfile.width + "x" + draftProfile.height, function (v) { var p = v.split("x"); draftProfile.width = +p[0]; draftProfile.height = +p[1]; assess(); })),
          fld("Frame rate", selOf((INPLACE ? [0] : []).concat([24, 25, 30, 60]).map(function (f) { return [f, f === 0 ? "Same as the stream (recommended)" : f + " fps"]; }), draftProfile.fps, function (v) { draftProfile.fps = parseInt(v, 10); assess(); })),
          fld("Scaling filter", selOf([["bilinear", "Bilinear (fastest)"], ["bicubic", "Bicubic (default)"], ["lanczos", "Lanczos (sharpest)"]], draftProfile.scaler, function (v) { draftProfile.scaler = v; }), "Used when the source is a different size from the canvas."), capNote]));
        pane.appendChild(h("p", { class: "muted", text: "The canvas is the output size. Layer positions are stored as percentages, so changing resolution never moves them. Settings are checked against this server's CPU." }));
        assess();
      } else if (tab === "hotkeys") {
        var tbl = h("div", { class: "hk" });
        Object.keys(HOTKEY_LABELS).forEach(function (a) {
          var inp = h("input", { type: "text", readonly: true, value: draftSettings.hotkeys[a], "aria-label": HOTKEY_LABELS[a] + " shortcut" });
          inp.addEventListener("keydown", function (e) {
            if (e.key === "Tab") { return; } e.preventDefault();
            var c = comboOf(e); if (c) { inp.value = c; draftSettings.hotkeys[a] = c; }
          });
          tbl.appendChild(h("label", { class: "hkrow" }, [h("span", { text: HOTKEY_LABELS[a] }), inp]));
        });
        pane.appendChild(section("Keyboard shortcuts", [tbl, h("p", { class: "muted", text: "Click a box and press the keys. Arrow keys on a selected layer always move it (Shift = larger steps, Alt = resize)." })]));
      } else if (tab === "advanced") {
        pane.appendChild(section("Advanced", [fld("Stream status refresh (seconds)", (function () { var i = h("input", { type: "number", min: 2, max: 60, value: draftSettings.advanced.status_poll_s }); i.addEventListener("input", function () { var v = parseInt(i.value, 10); if (v) { draftSettings.advanced.status_poll_s = v; } }); return i; })()),
          fld("Live text preview refresh (seconds)", (function () { var i = h("input", { type: "number", min: 2, max: 60, value: draftSettings.advanced.text_preview_s }); i.addEventListener("input", function () { var v = parseInt(i.value, 10); if (v) { draftSettings.advanced.text_preview_s = v; } }); return i; })()),
          chk("advanced", "video_thumbs", "Show video thumbnails in the media browser")]));
        pane.appendChild(section("Project", [h("p", { class: "muted", text: "Project: " + S.name + " · last applied revision: " + (published ? published.revision : "none") + " · keyframe interval: " + P.profile.fps * 2 + " frames (2 s)" })]));
      }
    }
    function listDests(area) {
      if (!dests.destinations.length) { area.appendChild(h("p", { class: "muted", text: "No destinations exist yet. Add one on the Destinations page." })); return; }
      dests.destinations.forEach(function (d) {
        var c = h("input", { type: "checkbox", checked: destSel.indexOf(d.name) >= 0, disabled: !d.enabled || !canEdit || INPLACE });
        c.addEventListener("change", function () { var i = destSel.indexOf(d.name); if (c.checked && i < 0) { destSel.push(d.name); } if (!c.checked && i >= 0) { destSel.splice(i, 1); } });
        area.appendChild(h("label", { class: "dest" }, [c, h("span", { class: "dn", text: d.display_name }), h("span", { class: "dp", text: d.platform }), h("span", { class: "dk " + (d.has_stream_key ? "ok" : "bad"), text: d.has_stream_key ? "key saved" : "no key" }),
          h("span", { class: "du", text: d.enabled ? d.server_url : "disabled" })]));
      });
      area.appendChild(h("p", { class: "fh", text: "Attaching saves the choice now; publishing to a newly attached destination starts the next time the stream is started." }));
    }
    render();
    dialog("Studio Settings", wrap, [{ label: "Cancel", value: null, secondary: true }, { label: "Apply", value: "apply" }, { label: "OK", value: "ok" }], true).then(function (v) {
      if (v !== "ok" && v !== "apply") { return; }
      // per-project output settings go into the draft; workspace preferences are saved on the server
      var profChanged = JSON.stringify(draftProfile) !== JSON.stringify(P.profile);
      if (profChanged) { P.profile = draftProfile; touch(); renderCanvas(); renderStatus(); }
      var pr = api(U.settings, { settings: draftSettings }).then(function (d) {
        if (!d.ok) { toast(d.error || "Settings not saved.", "error"); return; }
        settings = d.settings; applySettings(); toast("Settings saved.", "success");
      });
      var dp = (dests && stream.exists && canEdit && !INPLACE) ? api(U.destinations, { ids: destSel }).then(function (d) { toast(d.ok ? "Destinations saved." : (d.error || "Destinations not saved."), d.ok ? "success" : "error"); pollStatus(); }) : Promise.resolve();
      Promise.all([pr, dp]).then(function () { if (v === "apply") { openSettings(tab); } });
    });
  }

  // ---- hotkeys -------------------------------------------------------------------------
  function comboOf(e) {
    var k = e.key; if (["Control", "Shift", "Alt", "Meta"].indexOf(k) >= 0) { return ""; }
    var key = k.length === 1 ? k.toUpperCase() : k; if (!/^([A-Z0-9]|Arrow(Up|Down|Left|Right)|Enter|Delete|Backspace|F([1-9]|1[0-2]))$/.test(key)) { return ""; }
    return (e.ctrlKey || e.metaKey ? "Ctrl+" : "") + (e.altKey ? "Alt+" : "") + (e.shiftKey ? "Shift+" : "") + key;
  }
  document.addEventListener("keydown", function (e) {
    if (document.querySelector(".sdlg-back")) { return; }
    var tag = (e.target.tagName || "").toLowerCase(), typing = tag === "input" || tag === "textarea" || tag === "select" || e.target.isContentEditable;
    var c = comboOf(e); if (!c) { return; }
    var hk = settings.hotkeys, act = Object.keys(hk).filter(function (a) { return hk[a] === c; })[0]; if (!act) { return; }
    var global = act === "save" || act === "apply"; if (typing && !global) { return; }
    if (view !== "draft" || !canEdit) { return; }
    e.preventDefault();
    if (act === "save") { if (dirty) { save(); } } else if (act === "apply") { applyLive(); }
    else if (sel >= 0) {
      if (act === "duplicate") { dupLayer(sel); } else if (act === "delete") { removeLayer(sel); } else if (act === "toggle_visible") { setLayer(sel, "visible", !P.overlays[sel].visible); renderLayers(); }
      else if (act === "layer_up") { moveLayer(sel, 1); } else if (act === "layer_down") { moveLayer(sel, -1); }
    }
  });

  // ---- panels ---------------------------------------------------------------------------
  function store(k, v) { try { if (v === undefined) { return window.localStorage.getItem("bs-ws-" + k); } window.localStorage.setItem("bs-ws-" + k, v); } catch (e) { return null; } return null; }
  function applyLayoutPrefs() {
    ["left", "right", "bottom"].forEach(function (p) { if (store("hide-" + p) === "1") { root.classList.add("hide-" + p); } });
    ["wl", "wr", "hb"].forEach(function (v) { var x = store(v); if (x && /^\d+$/.test(x)) { root.style.setProperty("--" + v, x + "px"); } });
  }
  $$("[data-toggle]").forEach(function (b) {
    b.addEventListener("click", function () { var p = b.dataset.toggle, on = root.classList.toggle("hide-" + p); store("hide-" + p, on ? "1" : "0"); b.setAttribute("aria-expanded", on ? "false" : "true"); });
  });
  $$("[data-split]").forEach(function (sp) {
    sp.addEventListener("pointerdown", function (e) {
      var v = sp.dataset.split, sx = e.clientX, sy = e.clientY, cs = getComputedStyle(root), start = parseInt(cs.getPropertyValue("--" + v), 10) || 280;
      sp.setPointerCapture(e.pointerId);
      function mv(ev) { var d = v === "wl" ? ev.clientX - sx : v === "wr" ? sx - ev.clientX : sy - ev.clientY; var n = clamp(start + d, v === "hb" ? 140 : 200, v === "hb" ? 420 : 560); root.style.setProperty("--" + v, n + "px"); store(v, String(Math.round(n))); }
      function up() { sp.removeEventListener("pointermove", mv); sp.removeEventListener("pointerup", up); }
      sp.addEventListener("pointermove", mv); sp.addEventListener("pointerup", up); e.preventDefault();
    });
  });

  // ---- toolbar wiring ------------------------------------------------------------------------
  $("#ws-save").addEventListener("click", function () { save(); });
  $("#ws-apply").addEventListener("click", applyLive);
  $("#ws-settings").addEventListener("click", function () { openSettings("general"); });
  $$("[data-mon]").forEach(function (b) {
    b.addEventListener("click", function () {
      if (b.disabled) { return; }
      mon = b.dataset.mon; $$("[data-mon]").forEach(function (x) { x.setAttribute("aria-pressed", x === b ? "true" : "false"); });
      bgKey = ""; renderCanvas(); renderLayers(); buildProps();
    });
  });
  var rb = $("#ws-rollback"); if (rb) {
    rb.addEventListener("click", function () {
      var f = $("#ws-rollform"), v = $("#ws-rev").value; if (!v) { return; }
      confirmDialog("Load an earlier revision", "Revision " + v + " replaces your current draft. The live stream is not changed.", "Load into draft").then(function (go) { if (go) { f.revision.value = v; f.submit(); } });
    });
  }
  var del = $("#ws-delete"); if (del) {
    del.addEventListener("click", function () { confirmDialog("Delete project", "Delete this project and its revisions?", "Delete", true).then(function (go) { if (go) { $("#ws-delform").submit(); } }); });
  }
  function applySettings() {
    var t = settings.general.theme; document.documentElement.setAttribute("data-theme", t); try { window.localStorage.setItem("bs-theme", t); } catch (e) { /* ignore */ }
    renderCanvas(); scheduleTextPoll(); schedulePoll(); if (mediaCtl) { mediaCtl.refresh(); }
  }

  // ---- start ------------------------------------------------------------------------------
  buildMenu(); applyLayoutPrefs(); mountMedia();
  root.dataset.ready = "1";
  document.documentElement.setAttribute("data-theme", settings.general.theme); try { window.localStorage.setItem("bs-theme", settings.general.theme); } catch (e) { /* ignore */ }
  saved = snapshot();
  renderCanvas(); renderLayers(); buildProps(); renderStatus(); touch();
  if (!canEdit) { toast("Studio is read-only: " + (S.licence_reason || "editing is not available") + ".", "error"); }
  pollStatus().then(schedulePoll); scheduleTextPoll(); refreshTexts();
  if (P.source_kind === "relay" || P.source_kind === "playlist") { loadSources(); }
  if (INPLACE) {
    api(U.monitor).then(function (d) { if (d && d.ok && d.source && d.source.raw_url) { inplaceRaw = d.source.raw_url; bgKey = ""; renderCanvas(); } });
    var lt = $("#tab-sources"); if (lt) { lt.hidden = true; }
  }
  var tz = h("datalist", { id: "ws-tzs" }, ["UTC", "Europe/London", "Europe/Paris", "Europe/Berlin", "America/New_York", "America/Chicago", "America/Los_Angeles", "Africa/Nairobi", "Africa/Lagos", "Asia/Dubai", "Asia/Kolkata", "Asia/Tokyo", "Australia/Sydney"].map(function (z) { return h("option", { value: z }); }));
  root.appendChild(tz);
  window.studioWorkspace = { get project() { return P; }, select: select, setLayer: setLayer, addLayer: addLayer, save: save, isDirty: function () { return dirty; }, box: function (i) { return box(P.overlays[i]); }, natural: natural };
})();
