/* BlueStream Studio visual editor.
 * Progressive enhancement: without JS the numeric fields still work.
 * The canvas items are views of the real form inputs (x_N, y_N, w_N, h_N, visible_N ...);
 * dragging/resizing/keyboard only rewrites those inputs, so the server validates exactly
 * what the user sees. Pointer Events => mouse, pen and touch share one code path.
 */
(function () {
  "use strict";
  var canvas = document.getElementById("studio-canvas");
  if (!canvas) { return; }
  var live = document.getElementById("studio-live");
  var MIN = 0.02;            // smallest overlay edge (fraction of canvas)
  var SNAP = 0.012;          // snap distance to edges/centre/other overlays
  var selected = null;

  function field(name, i) { return document.querySelector('[name="' + name + "_" + i + '"]'); }
  function num(el, d) { var v = parseFloat(el && el.value); return isNaN(v) ? d : v; }
  function clamp(v, lo, hi) { return Math.min(hi, Math.max(lo, v)); }
  function r4(v) { return Math.round(v * 10000) / 10000; }
  function items() { return Array.prototype.slice.call(canvas.querySelectorAll(".studio-item")); }
  function say(msg) { if (live) { live.textContent = msg; } }

  function getBox(i) {
    return { x: num(field("x", i), 0), y: num(field("y", i), 0), w: num(field("w", i), 0.2), h: num(field("h", i), 0.1) };
  }
  function setBox(i, b) {
    b.w = clamp(b.w, MIN, 1); b.h = clamp(b.h, MIN, 1);
    b.x = clamp(b.x, 0, 1 - b.w); b.y = clamp(b.y, 0, 1 - b.h);
    ["x", "y", "w", "h"].forEach(function (k) { var f = field(k, i); if (f) { f.value = r4(b[k]); } });
    paint(i);
  }
  function itemFor(i) { return canvas.querySelector('.studio-item[data-i="' + i + '"]'); }

  function paint(i) {
    var el = itemFor(i), b = getBox(i);
    if (!el) { return; }
    el.style.left = (b.x * 100) + "%"; el.style.top = (b.y * 100) + "%";
    el.style.width = (b.w * 100) + "%"; el.style.height = (b.h * 100) + "%";
    var op = field("opacity", i); if (op) { el.style.opacity = num(op, 1); }
    var col = field("color", i); if (col && /^#[0-9a-fA-F]{6}$/.test(col.value)) {
      el.style.color = col.value; if (el.classList.contains("studio-rect")) { el.style.background = col.value; }
    }
    var vis = field("visible", i); el.classList.toggle("studio-hidden", !!vis && !vis.checked);
    var txt = field("text", i);
    if (txt && el.dataset.static === "1") { el.querySelector(".studio-label").textContent = txt.value; }
    paintCrop(i, el);
  }

  function paintCrop(i, el) {
    var img = el.querySelector("img.studio-asset");
    if (!img) { return; }
    var cx = num(field("crop_x", i), 0), cy = num(field("crop_y", i), 0);
    var cw = clamp(num(field("crop_w", i), 1), 0.05, 1), ch = clamp(num(field("crop_h", i), 1), 0.05, 1);
    // Show only the cropped part of the source: scale the image up and offset it.
    img.style.width = (100 / cw) + "%"; img.style.height = (100 / ch) + "%";
    img.style.left = (-cx / cw * 100) + "%"; img.style.top = (-cy / ch * 100) + "%";
  }

  function snapBox(i, b, mode) {
    var xs = [0, 0.5, 1], ys = [0, 0.5, 1];
    items().forEach(function (el) {
      var j = +el.dataset.i; if (j === i) { return; }
      var o = getBox(j); xs.push(o.x, o.x + o.w / 2, o.x + o.w); ys.push(o.y, o.y + o.h / 2, o.y + o.h);
    });
    function near(v, list) { for (var k = 0; k < list.length; k++) { if (Math.abs(v - list[k]) < SNAP) { return list[k]; } } return null; }
    if (mode === "move") {
      [[b.x, 0], [b.x + b.w / 2, b.w / 2], [b.x + b.w, b.w]].some(function (p) { var s = near(p[0], xs); if (s !== null) { b.x = s - p[1]; return true; } });
      [[b.y, 0], [b.y + b.h / 2, b.h / 2], [b.y + b.h, b.h]].some(function (p) { var s = near(p[0], ys); if (s !== null) { b.y = s - p[1]; return true; } });
    }
    return b;
  }

  function select(i, openPanel) {
    selected = i;
    items().forEach(function (el) { el.classList.toggle("studio-selected", +el.dataset.i === i); });
    Array.prototype.forEach.call(document.querySelectorAll("details.overlay-box"), function (d) {
      var on = +d.dataset.i === i; d.classList.toggle("selected", on);
      // never scroll: the canvas must stay under the pointer. A summary click lets the browser toggle itself.
      if (on && openPanel !== false) { d.open = true; }
    });
    var el = itemFor(i);
    if (el) { say("Selected layer " + (i + 1) + ", " + (el.getAttribute("aria-label") || "")); }
  }

  // ---- pointer interaction -------------------------------------------------
  var drag = null;
  canvas.addEventListener("pointerdown", function (ev) {
    var handle = ev.target.closest(".studio-handle");
    var el = ev.target.closest(".studio-item");
    if (!el || el.classList.contains("studio-hidden")) { return; }
    var i = +el.dataset.i;
    select(i);
    var rect = canvas.getBoundingClientRect();
    drag = { i: i, mode: handle ? "resize:" + handle.dataset.h : "move", sx: ev.clientX, sy: ev.clientY,
             rect: rect, start: getBox(i), id: ev.pointerId };
    try { el.setPointerCapture(ev.pointerId); } catch (e) { /* older browsers */ }
    ev.preventDefault();
  });
  canvas.addEventListener("pointermove", function (ev) {
    if (!drag || ev.pointerId !== drag.id) { return; }
    var dx = (ev.clientX - drag.sx) / drag.rect.width, dy = (ev.clientY - drag.sy) / drag.rect.height;
    var b = { x: drag.start.x, y: drag.start.y, w: drag.start.w, h: drag.start.h };
    if (drag.mode === "move") {
      b.x += dx; b.y += dy; b.x = clamp(b.x, 0, 1 - b.w); b.y = clamp(b.y, 0, 1 - b.h);
      if (!ev.altKey) { b = snapBox(drag.i, b, "move"); }
    } else {
      var h = drag.mode.split(":")[1];
      var x2 = b.x + b.w, y2 = b.y + b.h;
      if (h.indexOf("e") >= 0) { x2 = clamp(x2 + dx, b.x + MIN, 1); }
      if (h.indexOf("w") >= 0) { b.x = clamp(b.x + dx, 0, x2 - MIN); }
      if (h.indexOf("s") >= 0) { y2 = clamp(y2 + dy, b.y + MIN, 1); }
      if (h.indexOf("n") >= 0) { b.y = clamp(b.y + dy, 0, y2 - MIN); }
      b.w = x2 - b.x; b.h = y2 - b.y;
    }
    setBox(drag.i, b);
    ev.preventDefault();
  });
  function endDrag(ev) {
    if (!drag || (ev && ev.pointerId !== drag.id)) { return; }
    var b = getBox(drag.i);
    say("Layer " + (drag.i + 1) + " at " + Math.round(b.x * 100) + "%, " + Math.round(b.y * 100) + "%, size " +
        Math.round(b.w * 100) + " by " + Math.round(b.h * 100) + " percent");
    drag = null;
  }
  canvas.addEventListener("pointerup", endDrag);
  canvas.addEventListener("pointercancel", endDrag);

  // ---- keyboard: arrows move, Alt+arrows resize, Shift = coarse ------------------
  canvas.addEventListener("keydown", function (ev) {
    var el = ev.target.closest(".studio-item"); if (!el) { return; }
    var i = +el.dataset.i, step = ev.shiftKey ? 0.05 : 0.005, b = getBox(i);
    var dx = ev.key === "ArrowRight" ? step : ev.key === "ArrowLeft" ? -step : 0;
    var dy = ev.key === "ArrowDown" ? step : ev.key === "ArrowUp" ? -step : 0;
    if (!dx && !dy) { if (ev.key === "Enter" || ev.key === " ") { select(i); ev.preventDefault(); } return; }
    if (ev.altKey) { b.w += dx; b.h += dy; } else { b.x += dx; b.y += dy; }
    setBox(i, b); select(i); ev.preventDefault();
  });

  // numeric/other fields -> canvas
  document.addEventListener("input", function (ev) {
    var m = /_(\d+)$/.exec(ev.target.name || ""); if (!m) { return; }
    paint(+m[1]);
  });
  document.addEventListener("change", function (ev) {
    var m = /^visible_(\d+)$/.exec(ev.target.name || ""); if (m) { paint(+m[1]); }
  });
  Array.prototype.forEach.call(document.querySelectorAll("details.overlay-box summary"), function (s) {
    s.addEventListener("click", function () { select(+s.parentNode.dataset.i, false); });
  });

  // Dirty-state guard: warn before leaving with unsaved geometry edits.
  var dirty = false;
  document.addEventListener("input", function () { dirty = true; });
  window.addEventListener("beforeunload", function (ev) { if (dirty) { ev.preventDefault(); ev.returnValue = ""; } });
  Array.prototype.forEach.call(document.querySelectorAll("form"), function (f) {
    f.addEventListener("submit", function () { dirty = false; });
  });
  canvas.addEventListener("pointerup", function () { dirty = true; });

  items().forEach(function (el) { paint(+el.dataset.i); });
  var first = items()[0]; if (first) { select(+first.dataset.i); }
  window.studioEditor = { select: select, getBox: getBox, setBox: setBox };
})();
