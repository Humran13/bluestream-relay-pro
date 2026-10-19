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
  // "&" separator, e.g. "?relay=foo" becomes "?relay=foo?relay=foo". Since
  // only the first "?" in a URL starts its query string, URLSearchParams
  // then parses this as a single malformed value "foo?relay=foo" for the
  // "relay" key instead of two params.
  //
  // Recover ONLY the exact recognizable pattern "<value>?<key>=<value>"
  // (the same key and same value repeated verbatim after an embedded "?").
  // Anything else - a value that merely contains a "?", a different key, a
  // different value - is left untouched and allowed to fail validName()
  // honestly, rather than guessing at a truncation that could silently
  // accept the wrong channel.
  function recoverDuplicatedParam(key, raw) {
    if (!raw) return { value: raw, recovered: false };
    var idx = raw.indexOf("?");
    if (idx === -1) return { value: raw, recovered: false };
    var head = raw.slice(0, idx);
    var tail = raw.slice(idx + 1);
    if (head && tail === key + "=" + head) {
      return { value: head, recovered: true };
    }
    return { value: raw, recovered: false };
  }

  function params() {
    var q = new URLSearchParams(window.location.search);
    var relay = recoverDuplicatedParam("relay", q.get("relay"));
    var playlist = recoverDuplicatedParam("playlist", q.get("playlist"));
    return {
      relay: relay.value,
      playlist: playlist.value,
      src: q.get("src"), // src is itself a URL and may legitimately contain "?"
      recovered: relay.recovered || playlist.recovered
    };
  }

  // If a duplicated param was actually recovered above, rewrite the address
  // bar to the canonical single-parameter form so refresh, copy, and browser
  // history all carry the corrected URL instead of the broken one. Every
  // other valid query parameter present on the page is preserved verbatim.
  function canonicalizeAddressBar(p) {
    if (!p.recovered) return;
    var q = new URLSearchParams(window.location.search);
    if (p.relay && validName(p.relay)) q.set("relay", p.relay);
    if (p.playlist && validName(p.playlist)) q.set("playlist", p.playlist);
    var clean = window.location.pathname + "?" + q.toString() + window.location.hash;
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
