/* BlueStream Studio media browser: browse, search, filter, upload (with progress) and pick files from the
 * EXISTING Media Library. Used both as a side panel in the workspace and as a modal picker.
 * It only talks to the Studio JSON endpoints (list, thumbnail, upload); the browser never names a path.
 */
(function () {
  "use strict";
  var VIDEO = ["mp4", "mkv", "mov", "webm", "m4v", "ts"], IMAGE = ["png", "jpg", "jpeg", "webp", "gif"],
      AUDIO = ["mp3", "wav", "flac", "ogg", "m4a", "aac", "opus"];
  var MAX_IMAGE = 10 * 1024 * 1024;
  var KIND_EXT = { video: VIDEO, image: IMAGE, audio: AUDIO };
  var KIND_LABEL = { video: "Video", image: "Image", audio: "Audio" };

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) { e.className = cls; }
    if (text != null) { e.textContent = text; }
    return e;
  }
  function ext(name) { var m = /\.([^.]+)$/.exec(name || ""); return m ? m[1].toLowerCase() : ""; }
  function kindOf(name) {
    var x = ext(name);
    return VIDEO.indexOf(x) >= 0 ? "video" : IMAGE.indexOf(x) >= 0 ? "image" : AUDIO.indexOf(x) >= 0 ? "audio" : "other";
  }
  function human(n) {
    var u = ["B", "KB", "MB", "GB", "TB"], i = 0; n = +n || 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i === 0 ? n : n.toFixed(1)) + " " + u[i];
  }
  function thumbUrl(urls, name) { return urls.thumb.replace("__A__", encodeURIComponent(name)); }
  function assetUrl(urls, name) { return urls.asset.replace("__A__", encodeURIComponent(name)); }
  function icon(kind) {
    var s = { video: "M3 6h13v12H3zM16 10l5-3v10l-5-3", image: "M3 4h18v16H3zM3 16l5-5 4 4 3-3 6 6",
              audio: "M4 9v6h4l5 4V5L8 9zM16 9a4 4 0 0 1 0 6" }[kind] || "M5 3h10l4 4v14H5z";
    return '<svg viewBox="0 0 24 24" width="28" height="28" fill="none" stroke="currentColor" stroke-width="1.6" ' +
           'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="' + s + '"/></svg>';
  }

  /* opts: {urls:{media,upload,thumb,asset}, csrf, kinds:[...allowed kinds], multi:bool, thumbs:bool,
   *        onSelect(items[]), onUploaded(item)} */
  function mount(root, opts) {
    var urls = opts.urls, allowed = (opts.kinds && opts.kinds.length) ? opts.kinds.slice() : ["video", "image", "audio"];
    var state = { items: [], filter: allowed.length === 1 ? allowed[0] : "", q: "", selected: [], loading: false, error: "" };
    root.classList.add("smb");
    root.innerHTML = "";
    var bar = el("div", "smb-bar"), search = el("input", "smb-search");
    search.type = "search"; search.placeholder = "Search media"; search.setAttribute("aria-label", "Search media");
    var chips = el("div", "smb-chips", null), up = el("button", "btn btn-sm smb-upload", "Upload");
    up.type = "button"; up.title = "Upload from this computer";
    var file = el("input"); file.type = "file"; file.multiple = true; file.hidden = true;
    file.accept = allowed.reduce(function (a, k) { return a.concat(KIND_EXT[k].map(function (x) { return "." + x; })); }, []).join(",");
    bar.appendChild(search); bar.appendChild(up); bar.appendChild(file);
    var status = el("div", "smb-status"); status.setAttribute("aria-live", "polite");
    var uploads = el("div", "smb-uploads"), grid = el("div", "smb-grid");
    grid.setAttribute("role", "listbox"); grid.setAttribute("aria-label", "Media Library files");
    grid.setAttribute("aria-multiselectable", opts.multi ? "true" : "false");
    var drop = el("div", "smb-drop", "Drop files here to upload");
    root.appendChild(bar); if (allowed.length > 1) { root.appendChild(chips); }
    root.appendChild(status); root.appendChild(uploads); root.appendChild(grid); root.appendChild(drop);

    function renderChips() {
      chips.innerHTML = "";
      [["", "All"]].concat(allowed.map(function (k) { return [k, KIND_LABEL[k]]; })).forEach(function (p) {
        var b = el("button", "smb-chip" + (state.filter === p[0] ? " on" : ""), p[1]);
        b.type = "button"; b.setAttribute("aria-pressed", state.filter === p[0] ? "true" : "false");
        b.addEventListener("click", function () { state.filter = p[0]; renderChips(); load(); });
        chips.appendChild(b);
      });
    }

    function visible() { return state.items; }
    function isSel(n) { return state.selected.indexOf(n) >= 0; }
    function fire() {
      if (opts.onSelect) { opts.onSelect(state.selected.map(function (n) {
        return state.items.filter(function (i) { return i.name === n; })[0] || { name: n, kind: kindOf(n) }; })); }
    }
    function toggle(name) {
      if (opts.multi) {
        var i = state.selected.indexOf(name);
        if (i >= 0) { state.selected.splice(i, 1); } else { state.selected.push(name); }
      } else { state.selected = [name]; }
      render(); fire();
    }

    function render() {
      grid.innerHTML = "";
      var items = visible();
      if (state.loading && !items.length) { status.textContent = "Loading media..."; }
      else if (state.error) { status.textContent = state.error; status.className = "smb-status err"; }
      else {
        status.className = "smb-status";
        status.textContent = items.length ? items.length + (items.length === 1 ? " file" : " files") :
          (state.q || state.filter ? "No media matches your search." : "The Media Library has no matching files yet. Upload one.");
      }
      items.forEach(function (it) {
        var card = el("button", "smb-card" + (isSel(it.name) ? " sel" : ""));
        card.type = "button"; card.setAttribute("role", "option"); card.setAttribute("aria-selected", isSel(it.name) ? "true" : "false");
        card.dataset.name = it.name; card.dataset.kind = it.kind; card.title = it.name;
        var th = el("span", "smb-thumb");
        if (it.kind === "audio" || (it.kind === "video" && opts.thumbs === false)) { th.innerHTML = icon(it.kind); }
        else {
          var img = el("img"); img.loading = "lazy"; img.alt = ""; img.decoding = "async";
          img.src = it.kind === "image" ? assetUrl(urls, it.name) : thumbUrl(urls, it.name);
          img.addEventListener("error", function () { th.innerHTML = icon(it.kind); });
          th.appendChild(img);
        }
        var tag = el("span", "smb-tag", it.kind === "image" ? it.ext : (it.kind === "video" ? it.ext : it.ext));
        th.appendChild(tag);
        card.appendChild(th); card.appendChild(el("span", "smb-name", it.name));
        card.appendChild(el("span", "smb-meta", KIND_LABEL[it.kind] + " · " + it.size_text));
        card.addEventListener("click", function () { toggle(it.name); });
        card.addEventListener("dblclick", function () {
          state.selected = [it.name]; render(); fire(); if (opts.onChoose) { opts.onChoose([it]); }
        });
        grid.appendChild(card);
      });
    }

    var seq = 0;
    function load() {
      var my = ++seq; state.loading = true; state.error = "";
      var qs = "?q=" + encodeURIComponent(state.q) + (state.filter ? "&kind=" + state.filter : "");
      return fetch(urls.media + qs, { credentials: "same-origin", headers: { "Accept": "application/json" } })
        .then(function (r) { return r.json().catch(function () { return { ok: false, error: "Unexpected response." }; }); })
        .then(function (d) {
          if (my !== seq) { return; }
          state.loading = false;
          if (!d.ok) { state.error = d.error || "The Media Library could not be read."; state.items = []; }
          else { state.items = d.items.filter(function (i) { return allowed.indexOf(i.kind) >= 0; }); }
          state.selected = state.selected.filter(function (n) { return state.items.some(function (i) { return i.name === n; }); });
          render();
        })
        .catch(function () { if (my === seq) { state.loading = false; state.error = "Could not reach the server."; render(); } });
    }

    var timer = null;
    search.addEventListener("input", function () {
      clearTimeout(timer); timer = setTimeout(function () { state.q = search.value; load(); }, 200);
    });

    // ---- uploads ----------------------------------------------------------------
    function checkFile(f) {
      var x = ext(f.name), k = kindOf(f.name);
      if (k === "other" || allowed.indexOf(k) < 0) {
        return "'" + f.name + "' is not a supported " + allowed.map(function (a) { return KIND_LABEL[a].toLowerCase(); }).join("/") + " file.";
      }
      if (k === "image" && f.size > MAX_IMAGE) { return "'" + f.name + "' is larger than 10 MB. Resize the image first."; }
      if (!f.size) { return "'" + f.name + "' is empty."; }
      return x ? "" : "'" + f.name + "' has no file extension.";
    }
    function uploadOne(f, done) {
      var row = el("div", "smb-up"), nm = el("span", "smb-up-name", f.name), pr = document.createElement("progress");
      pr.max = 100; pr.value = 0; var msg = el("span", "smb-up-msg");
      row.appendChild(nm); row.appendChild(pr); row.appendChild(msg); uploads.appendChild(row);
      var bad = checkFile(f);
      if (bad) { msg.textContent = bad; row.classList.add("err"); pr.remove(); return done(false); }
      var fd = new FormData(); fd.append("media", f, f.name);
      var x = new XMLHttpRequest(); x.open("POST", urls.upload);
      x.setRequestHeader("X-CSRF-Token", opts.csrf); x.setRequestHeader("X-Requested-With", "XMLHttpRequest");
      x.upload.onprogress = function (e) { if (e.lengthComputable) { pr.value = Math.round(e.loaded / e.total * 100); } };
      x.onload = function () {
        var d = {}; try { d = JSON.parse(x.responseText); } catch (e) { d = { ok: false, error: "Unexpected response (" + x.status + ")." }; }
        if (d.ok) {
          pr.value = 100; msg.textContent = "Added"; row.classList.add("ok");
          setTimeout(function () { row.remove(); }, 4000); done(true, d);
        } else {
          pr.remove(); msg.textContent = d.error || "Upload failed."; row.classList.add("err");
          var close = el("button", "smb-x", "×"); close.type = "button"; close.setAttribute("aria-label", "Dismiss");
          close.addEventListener("click", function () { row.remove(); }); row.appendChild(close); done(false);
        }
      };
      x.onerror = function () { pr.remove(); msg.textContent = "Upload failed: the connection was interrupted."; row.classList.add("err"); done(false); };
      x.send(fd);
    }
    function uploadAll(files) {
      var list = Array.prototype.slice.call(files), added = [];
      (function next() {
        var f = list.shift();
        if (!f) {
          load().then(function () {
            if (added.length) {
              state.selected = opts.multi ? state.selected.concat(added) : [added[added.length - 1]];
              state.selected = state.selected.filter(function (n) { return state.items.some(function (i) { return i.name === n; }); });
              render(); fire();
            }
          });
          return;
        }
        uploadOne(f, function (ok, d) { if (ok) { added.push(d.name); if (opts.onUploaded) { opts.onUploaded(d); } } next(); });
      })();
    }
    up.addEventListener("click", function () { file.click(); });
    file.addEventListener("change", function () { if (file.files.length) { uploadAll(file.files); } file.value = ""; });
    ["dragenter", "dragover"].forEach(function (t) { root.addEventListener(t, function (e) {
      if (e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types || [], "Files") >= 0) { e.preventDefault(); root.classList.add("dragging"); } }); });
    ["dragleave", "drop"].forEach(function (t) { root.addEventListener(t, function (e) {
      if (t === "dragleave" && root.contains(e.relatedTarget)) { return; }
      root.classList.remove("dragging");
      if (t === "drop" && e.dataTransfer && e.dataTransfer.files.length) { e.preventDefault(); uploadAll(e.dataTransfer.files); } }); });

    renderChips(); render(); load();
    return {
      refresh: load,
      selected: function () { return state.selected.slice(); },
      clear: function () { state.selected = []; render(); },
      setKinds: function (k) { allowed = k.slice(); state.filter = allowed.length === 1 ? allowed[0] : ""; renderChips(); load(); }
    };
  }

  /* Modal picker -> Promise of the chosen items (array) or null when cancelled. */
  function pick(opts) {
    return new Promise(function (resolve) {
      var prev = document.activeElement;
      var back = el("div", "sdlg-back"), dlg = el("div", "sdlg sdlg-wide");
      dlg.setAttribute("role", "dialog"); dlg.setAttribute("aria-modal", "true"); dlg.setAttribute("aria-label", opts.title || "Choose media");
      var head = el("div", "sdlg-head"); head.appendChild(el("h2", "", opts.title || "Choose media"));
      var body = el("div", "sdlg-body"), foot = el("div", "sdlg-foot");
      var ok = el("button", "btn", opts.multi ? "Use selected" : "Use this file"), cancel = el("button", "btn btn-secondary", "Cancel");
      ok.type = cancel.type = "button"; ok.disabled = true;
      var hint = el("span", "muted", opts.multi ? "Select one or more files." : "Select a file (double-click to choose).");
      foot.appendChild(hint); foot.appendChild(cancel); foot.appendChild(ok);
      dlg.appendChild(head); dlg.appendChild(body); dlg.appendChild(foot); back.appendChild(dlg); document.body.appendChild(back);
      var chosen = [];
      function close(v) { back.remove(); document.removeEventListener("keydown", onKey, true); if (prev && prev.focus) { prev.focus(); } resolve(v); }
      function onKey(e) { if (e.key === "Escape") { e.stopPropagation(); close(null); } }
      document.addEventListener("keydown", onKey, true);
      cancel.addEventListener("click", function () { close(null); });
      back.addEventListener("mousedown", function (e) { if (e.target === back) { close(null); } });
      ok.addEventListener("click", function () { close(chosen); });
      mount(body, { urls: opts.urls, csrf: opts.csrf, kinds: opts.kinds, multi: !!opts.multi, thumbs: opts.thumbs,
                    onSelect: function (items) { chosen = items; ok.disabled = !items.length; },
                    onChoose: function (items) { close(items); } });
      var s = body.querySelector("input"); if (s) { s.focus(); }
    });
  }

  window.StudioMedia = { mount: mount, pick: pick, kindOf: kindOf, human: human, thumbUrl: thumbUrl, assetUrl: assetUrl, icon: icon };
})();
