/* BlueStream Relay Pro - bundled hls.js player.
 *
 * Usage:
 *   /player/?relay=<name>
 *   /player/?playlist=<name>
 *   /player/?src=<full-hls-url>
 */
(function () {
  "use strict";

  var video = document.getElementById("video");
  var info = document.getElementById("info");
  var errorBox = document.getElementById("error");
  var label = document.getElementById("channel-label");

  function validName(name) {
    return /^[a-z0-9][a-z0-9_-]{0,47}$/.test(name || "");
  }

  // Some browsers' address bars can duplicate a pasted query string with no
  // "&" separator (e.g. "?relay=foo?relay=foo"), which URLSearchParams then
  // parses as a single malformed value "foo?relay=foo" for the first key.
  // Recover the intended value by cutting at the embedded "?" and re-parsing
  // whatever follows it as a second, redundant query string.
  function dequalify(raw) {
    if (!raw) return raw;
    var cut = raw.indexOf("?");
    if (cut === -1) return raw;
    return raw.slice(0, cut);
  }

  function params() {
    var q = new URLSearchParams(window.location.search);
    return {
      relay: dequalify(q.get("relay")),
      playlist: dequalify(q.get("playlist")),
      src: q.get("src") // src is itself a URL and may legitimately contain "?"
    };
  }

  // If the address bar query string was malformed (duplicated with no "&"),
  // rewrite it to the canonical single-parameter form so refresh, copy, and
  // browser history all carry the corrected URL instead of the broken one.
  function canonicalizeAddressBar(p) {
    var raw = window.location.search;
    if (raw.indexOf("?", 1) === -1) return; // no embedded extra "?", nothing to fix
    var qs = null;
    if (p.relay && validName(p.relay)) qs = "relay=" + encodeURIComponent(p.relay);
    else if (p.playlist && validName(p.playlist)) qs = "playlist=" + encodeURIComponent(p.playlist);
    if (qs === null) return;
    var clean = window.location.pathname + "?" + qs + window.location.hash;
    if (window.history && window.history.replaceState) {
      window.history.replaceState(null, "", clean);
    }
  }

  function showError(msg) {
    errorBox.textContent = msg;
    errorBox.hidden = false;
  }

  function play(url) {
    if (window.Hls && Hls.isSupported()) {
      var hls = new Hls({ maxLiveSyncPlaybackRate: 1.5 });
      hls.loadSource(url);
      hls.attachMedia(video);
      hls.on(Hls.Events.ERROR, function (evt, data) {
        if (data && data.fatal) {
          switch (data.type) {
            case Hls.ErrorTypes.NETWORK_ERROR:
              showError("Network error - retrying...");
              hls.startLoad();
              break;
            case Hls.ErrorTypes.MEDIA_ERROR:
              showError("Media error - recovering...");
              hls.recoverMediaError();
              break;
            default:
              showError("Playback error: " + (data.details || "unknown"));
              hls.destroy();
          }
        }
      });
      info.textContent = "hls.js playback: " + url;
      video.play().catch(function () { /* autoplay policy */ });
    } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
      video.src = url;
      info.textContent = "Native HLS playback: " + url;
    } else {
      showError("This browser cannot play HLS. The page loads hls.js from a CDN - check network access.");
    }
  }

  var p = params();
  canonicalizeAddressBar(p);
  var url = null;
  if (p.relay && validName(p.relay)) {
    label.textContent = "Relay: " + p.relay;
    url = "/hls/relay/" + encodeURIComponent(p.relay) + "/index.m3u8";
  } else if (p.playlist && validName(p.playlist)) {
    label.textContent = "Playlist: " + p.playlist;
    url = "/hls/playlist/" + encodeURIComponent(p.playlist) + "/index.m3u8";
  } else if (p.src) {
    label.textContent = "Custom source";
    url = p.src;
  } else {
    showError("No channel specified. Use ?relay=name or ?playlist=name.");
  }

  if (url) {
    play(new URL(url, window.location.origin).href);
  }
})();
