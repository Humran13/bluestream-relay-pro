/* BlueStream Studio live canvas player: REAL moving video behind the overlay editor.
 *  - "file": an uploaded Media Library video, streamed on demand by the server (copy or one light 540p transcode while watching)
 *  - "hls":  an existing relay / playlist / the Studio output, read-only through its public HLS (hls.js, vendored; Safari native)
 * Local pause / restart / loop / mute only touch THIS <video> element: they can never pause or restart a broadcast.
 * Every failure is shown (loading / disconnected / error + retry): a still is never passed off as live video.
 */
(function () {
  "use strict";
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) { e.className = cls; } if (text != null) { e.textContent = text; } return e; }

  function create(canvas, bar, opts) {
    opts = opts || {};
    var video = el("video", "ws-video"), badge = el("div", "ws-vstate"), hls = null, retryTimer = null, resumeTimer = null, playAttempt = 0, retries = 0, spec = { mode: "none" };
    video.setAttribute("playsinline", ""); video.muted = true; video.autoplay = true; video.preload = "auto";
    video.hidden = true; badge.setAttribute("aria-live", "polite");
    canvas.insertBefore(video, canvas.firstChild); canvas.appendChild(badge);
    var st = { loop: true, muted: true, state: "idle", detail: "" };

    // ---- control bar -----------------------------------------------------------
    var bPlay = el("button", "btn btn-sm btn-secondary", "Pause"), bRestart = el("button", "btn btn-sm btn-secondary", "Restart"),
        bLoop = el("button", "btn btn-sm btn-secondary", "Loop: on"), bMute = el("button", "btn btn-sm btn-secondary", "Preview muted"),
        bRetry = el("button", "btn btn-sm", "Retry"), info = el("span", "ws-vinfo");
    [bPlay, bRestart, bLoop, bMute, bRetry].forEach(function (b) { b.type = "button"; });
    bRetry.hidden = true; bLoop.setAttribute("aria-pressed", "true"); bMute.setAttribute("aria-pressed", "true");
    bar.innerHTML = ""; [bPlay, bRestart, bLoop, bMute, bRetry, info].forEach(function (n) { bar.appendChild(n); });

    function setState(s, detail) {
      st.state = s; st.detail = detail || "";
      var live = spec.mode === "hls";
      var txt = { loading: "Loading video…", playing: live ? ("LIVE" + (spec.label ? " · " + spec.label : "") + " · about " + (opts.hlsDelay || "10") + " s behind") : "Playing",
                  paused: "Paused (preview only)", error: detail || "The video cannot be played.", disconnected: detail || "Disconnected", idle: "", ended: "Finished" }[s] || "";
      badge.textContent = txt; badge.className = "ws-vstate " + s + (live ? " live" : "");
      badge.hidden = !txt || (s === "playing" && !live && spec.quiet);
      bRetry.hidden = !(s === "error" || s === "disconnected");
      bPlay.textContent = video.paused ? "Play" : "Pause";
      var dims = video.videoWidth ? video.videoWidth + "×" + video.videoHeight : "";
      info.textContent = [dims, spec.mode === "file" ? "file preview" : spec.mode === "hls" ? "live stream (read-only)" : ""].filter(Boolean).join(" · ");
      var local = spec.mode === "file", any = spec.mode === "file" || spec.mode === "hls";
      bPlay.hidden = !any; bMute.hidden = !any; bRestart.hidden = !local; bLoop.hidden = !local; info.hidden = !any;
      if (opts.onState) { opts.onState({ mode: spec.mode, state: s, detail: detail || "", width: video.videoWidth, height: video.videoHeight }); }
    }

    function teardown() {
      playAttempt++; // invalidate a pending play() rejection from the old source
      clearTimeout(retryTimer); clearTimeout(resumeTimer);
      if (hls) { try { hls.destroy(); } catch (e) { /* ignore */ } hls = null; }
      try { video.pause(); } catch (e) { /* ignore */ }
      video.removeAttribute("src"); try { video.load(); } catch (e) { /* ignore */ }
    }
    function tryPlay() {
      var attempt = ++playAttempt, p = video.play();
      if (p && p.catch) {
        p.catch(function (err) {
          // Firefox may reject the old source's play promise after load() has
          // already selected a stopped relay. Never let that stale callback
          // overwrite the new source's authoritative status.
          if (attempt !== playAttempt || !(spec.mode === "file" || spec.mode === "hls")) { return; }
          if (st.state === "error" || st.state === "disconnected") { return; }
          // A source the browser cannot open (a 4xx/5xx answer, a file that is not a video) rejects play() with NotSupportedError
          // and sets video.error; Edge reports that here BEFORE (or without) an error event. That is an ERROR, not a "paused" video.
          if (video.error || (err && err.name === "NotSupportedError")) { onMediaError(); return; }
          setState("paused");
        });
      }
    }

    function startFile(url) {
      clearTimeout(resumeTimer);
      setState("loading");
      video.src = url + (url.indexOf("?") < 0 ? "?" : "&") + "t=" + Date.now();
      tryPlay();
    }

    function recoverStalledFile() {
      if (spec.mode !== "file" || video.paused) { return; }
      var stalledAt = video.currentTime;
      clearTimeout(resumeTimer);
      resumeTimer = setTimeout(function () {
        if (spec.mode === "file" && !video.paused && video.currentTime <= stalledAt + 0.05) { startFile(spec.url); }
      }, 1500);
    }
    function startHls(url) {
      setState("loading");
      if (window.Hls && window.Hls.isSupported()) {
        hls = new window.Hls({ liveSyncDurationCount: 3, maxLiveSyncPlaybackRate: 1.15, manifestLoadingMaxRetry: 1, levelLoadingMaxRetry: 2, enableWorker: true });
        hls.on(window.Hls.Events.MANIFEST_PARSED, function () { retries = 0; tryPlay(); });
        hls.on(window.Hls.Events.ERROR, function (evt, data) {
          if (!data || !data.fatal) { return; }
          if (data.type === window.Hls.ErrorTypes.MEDIA_ERROR && retries < 2) { retries++; try { hls.recoverMediaError(); } catch (e) { /* ignore */ } return; }
          var why = data.type === window.Hls.ErrorTypes.NETWORK_ERROR ? "The stream is not reachable (stopped, not started yet, or blocked from this address)." : "The stream could not be decoded.";
          scheduleRetry(why);
        });
        hls.loadSource(url); hls.attachMedia(video);
      } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
        video.src = url; tryPlay();
      } else { setState("error", "This browser cannot play HLS video."); }
    }
    function scheduleRetry(why) {
      var wait = [3, 5, 10, 15][Math.min(retries, 3)]; retries++;
      if (hls) { try { hls.destroy(); } catch (e) { /* ignore */ } hls = null; }
      setState("disconnected", why + " Retrying in " + wait + " s.");
      clearTimeout(retryTimer);
      retryTimer = setTimeout(function () { if (spec.mode === "hls" && spec.url) { startHls(spec.url); } }, wait * 1000);
    }

    video.addEventListener("playing", function () { setState("playing"); });
    video.addEventListener("pause", function () { if (!video.ended && st.state === "playing") { setState("paused"); } });
    video.addEventListener("waiting", function () { if (st.state === "playing") { setState("loading"); } recoverStalledFile(); });
    video.addEventListener("stalled", recoverStalledFile);
    video.addEventListener("loadedmetadata", function () { setState(st.state, st.detail); });
    video.addEventListener("ended", function () {
      if (spec.mode === "file" && st.loop) { startFile(spec.url); } else { setState("ended"); }
    });
    function onMediaError() {
      if (spec.mode === "file") { setState("error", "This video cannot be previewed here (unreadable, unsupported, or too many previews are open). The stream itself is not affected."); }
      else if (spec.mode === "hls" && !hls) { scheduleRetry("The stream is not reachable."); }
    }
    video.addEventListener("error", onMediaError);
    bPlay.addEventListener("click", function () {
      if (video.paused) {
        var resumeAt = video.currentTime;
        tryPlay();
        // Some browsers cancel an unknown-length streaming response while a
        // file preview is paused. If Play unpauses the element but no media
        // time advances, reopen this preview (with startFile's cache buster)
        // instead of leaving a misleading, permanently stalled Play state.
        if (spec.mode === "file") {
          clearTimeout(resumeTimer);
          resumeTimer = setTimeout(function () {
            if (spec.mode === "file" && !video.paused && video.currentTime <= resumeAt + 0.05) { startFile(spec.url); }
          }, 1000);
        }
      }
      else {
        video.pause();
        // WebKit can emit "waiting" immediately before "pause". In that case
        // the pause event's conservative state guard does not change the label,
        // even though the element really is paused. A direct user action is
        // authoritative, so reflect it synchronously in the local controls.
        setState("paused");
      }
    });
    bRestart.addEventListener("click", function () { if (spec.mode === "file") { startFile(spec.url); } });
    bLoop.addEventListener("click", function () { st.loop = !st.loop; bLoop.textContent = "Loop: " + (st.loop ? "on" : "off"); bLoop.setAttribute("aria-pressed", st.loop ? "true" : "false"); });
    bMute.addEventListener("click", function () { st.muted = !st.muted; video.muted = st.muted; bMute.textContent = st.muted ? "Preview muted" : "Preview sound on"; bMute.setAttribute("aria-pressed", st.muted ? "true" : "false"); });
    bRetry.addEventListener("click", function () { retries = 0; load(spec, true); });
    document.addEventListener("visibilitychange", function () { if (document.hidden && spec.mode === "hls") { try { video.pause(); } catch (e) { /* ignore */ } } else if (spec.mode === "hls" && st.state !== "error") { tryPlay(); } });

    /* spec: {mode: "file"|"hls"|"still"|"none", url, label, message, quiet} */
    function load(next, force) {
      var same = spec.mode === next.mode && spec.url === next.url;
      if (same && !force) { return; }
      teardown(); spec = next; retries = 0;
      if (spec.mode === "file" && spec.url) { video.hidden = false; startFile(spec.url); }
      else if (spec.mode === "hls" && spec.url) { video.hidden = false; startHls(spec.url); }
      else {
        video.hidden = true;
        setState(spec.mode === "hls" ? "error" : "idle", spec.mode === "hls" ? (spec.message || "No live stream address is available (the server's public address is not configured).") : (spec.message || ""));
        if (spec.mode === "none" && spec.message) { badge.textContent = spec.message; badge.hidden = false; badge.className = "ws-vstate idle"; }
      }
    }
    return { load: load, state: function () { return { mode: spec.mode, state: st.state, loop: st.loop, muted: st.muted, w: video.videoWidth, h: video.videoHeight, paused: video.paused, t: video.currentTime }; },
             video: video, destroy: function () { teardown(); } };
  }
  window.StudioPlayer = { create: create };
})();
