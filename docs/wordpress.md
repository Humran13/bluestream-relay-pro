# WordPress / Website Integration

Every BlueStream relay and playlist exposes two stable URLs:

```text
M3U8:
https://example.com/hls/relay/<name>/index.m3u8
https://example.com/hls/playlist/<name>/index.m3u8

Player:
https://example.com/player/?relay=<name>
https://example.com/player/?playlist=<name>
```

The M3U8 URL is stable even when the source restarts or reconnects, so you
can embed it once and leave it.

## 1. Embed the bundled player (simplest)

Use an iframe on any page:

```html
<iframe
  src="https://example.com/player/?relay=news"
  width="960"
  height="540"
  frameborder="0"
  allowfullscreen>
</iframe>
```

This works in any modern browser. hls.js is loaded from a CDN; native HLS
(Safari) works without it.

## 2. Generic HTML5 + hls.js example

```html
<video id="player" controls playsinline></video>

<script src="https://cdn.jsdelivr.net/npm/hls.js@1"></script>
<script>
  var video = document.getElementById("player");
  var m3u8Url = "https://example.com/hls/relay/news/index.m3u8";

  if (Hls.isSupported()) {
    var hls = new Hls();
    hls.loadSource(m3u8Url);
    hls.attachMedia(video);
  } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
    video.src = m3u8Url;   // native HLS (Safari)
  }
</script>
```

## 3. WordPress without a plugin

- **Classic editor / block editor (HTML block):** paste the iframe or the
  `<video>` + `<script>` snippet into a Custom HTML block.
- **Theme widget:** use a "Custom HTML" widget in a sidebar or footer.
- **PHP template / page template:** echo the snippet inside the page loop.

Example shortcode-style PHP for a template:

```php
<?php echo '<iframe src="https://example.com/player/?relay=news" width="960" height="540" allowfullscreen></iframe>'; ?>
```

No proprietary WordPress plugin is required for version 1.

## 4. VLC / IPTV players

- VLC: Media > Open Network Stream, paste the M3U8 URL.
- IPTV-compatible players: use the M3U8 URL directly as a channel source.

## CORS and caching notes

- The HLS endpoints send `Access-Control-Allow-Origin: *`, so cross-origin
  embedding works.
- `.m3u8` playlists are served with `Cache-Control: no-cache`.
- `.ts` segments are served with a 24-hour cache and `expires 1d`.

## HTTPS and mixed content

Use HTTPS URLs everywhere. If your site is HTTPS, an HTTP M3U8 URL will be
blocked as mixed content. BlueStream enables HTTPS via Let's Encrypt
(certbot) — see `installation.md`.
