/* New-project form: choose the source from the Media Library instead of typing a file name. */
(function () {
  "use strict";
  var form = document.getElementById("studio-new");
  if (!form || !window.StudioMedia) { return; }
  var kind = document.getElementById("sk"), ref = document.getElementById("sr"), btn = document.getElementById("sr-browse"),
      list = document.getElementById("sources"), help = document.getElementById("sr-help");
  var urls = { media: form.dataset.mediaUrl, upload: form.dataset.uploadUrl, thumb: form.dataset.thumbUrl, asset: form.dataset.assetUrl };
  var lists = {};
  try { lists = JSON.parse(document.getElementById("source-lists").textContent) || {}; } catch (e) { lists = {}; }
  var KINDS = { media: ["video"], slideshow: ["image"], audio: ["audio"] };
  var TITLE = { media: "Choose a video", slideshow: "Choose slideshow images", audio: "Choose an audio file" };

  function sync() {
    var k = kind.value, media = !!KINDS[k];
    btn.hidden = !media;
    ref.readOnly = false;
    ref.placeholder = media ? "choose from the Media Library" : (k === "relay" ? "name of an existing stream" : "name of an existing playlist");
    list.innerHTML = "";
    (lists[k] || []).forEach(function (n) { var o = document.createElement("option"); o.value = n; list.appendChild(o); });
    ref.setAttribute("list", media ? "" : "sources");
    help.textContent = media ? "Pick a file with Browse (you can upload from your computer there). Nothing has to be typed."
      : "Choose one of your existing " + (k === "relay" ? "streams" : "playlists") + ".";
  }
  kind.addEventListener("change", function () { ref.value = ""; sync(); });
  btn.addEventListener("click", function () {
    var k = kind.value;
    StudioMedia.pick({ title: TITLE[k], kinds: KINDS[k], multi: k === "slideshow", urls: urls, csrf: form.dataset.csrf }).then(function (items) {
      if (items && items.length) { ref.value = items.map(function (i) { return i.name; }).join(","); ref.dispatchEvent(new Event("input", { bubbles: true })); }
    });
  });
  sync();
})();
