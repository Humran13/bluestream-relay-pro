# BlueStream Studio (beta candidate)

Studio adds branded overlays and live widgets (countdowns, clocks, Pomodoro,
tickers, Now Playing, API-backed numbers) to a 24/7 stream, running **on your
own VPS**. It is **opt-in** and Premium; Free Relay and Playlists are unchanged.

## How it fits

A Studio project becomes a normal stream of type **Studio**. It publishes into
the same private local RTMP bus as every Relay, so **destinations, HLS/web
player, Start/Stop, boot persistence and health work exactly as before**. There
is no second publishing path, so two publishers can never push the same key.

```
media / relay / playlist -> [Studio pipeline] -> local RTMP bus -> HLS + your destinations
```

## The three pipelines (chosen automatically, shown in the editor)

| Pipeline | When | CPU |
|---|---|---|
| Direct copy | no overlays and the source already matches 720p30 H.264/AAC with <=4 s keyframes | ~0 |
| Pre-render, then copy | finite media + only **static** overlays (text, logo, bar) | one-time encode, then ~0 |
| Live composition | any changing overlay (clock, countdown, ticker, data, Now Playing) or a live source | one x264 encode (~1.5-2.5 cores at 720p30 on a typical 4-vCPU VPS - **measure on yours**) |

Only one Live composition runs at a time by default (`BLUESTREAM_STUDIO_MAX_LIVE`).
Starting another is **refused with a clear message**, never silently degraded.

## Using it

1. Enable the feature (opt-in; off by default) with a systemd drop-in, then restart
   **only** the console: `systemctl edit bluestream-web` and add
   `[Service]` / `Environment=BLUESTREAM_STUDIO_ENABLED=1`, then `systemctl restart bluestream-web`.
   Running streams are not touched. The vendor public key must also be present
   (see `docs/bluestream/COMMERCIAL.md`) or Studio stays locked.
2. **Studio -> Licence**: paste your licence key.
3. **New project**: pick media/relay/playlist, choose a template.
4. Edit overlays (position as fractions of the canvas, so layouts survive a
   later move to 1080p), **Save draft**, check the preview, **Apply to Live**.
   A draft never touches the running stream. If the stream is running, Apply
   asks for confirmation and restarts **only that stream**. Any earlier revision
   (last 10 kept) can be loaded back into the draft.
5. **Create stream**, attach destinations on the normal Destinations page, **Start**.

Timers survive browser closure, logout and restarts: they are computed from an
absolute deadline/anchor and a timezone (DST-correct), not from a counter.

## Data overlays and security

* Data overlays fetch a **public** HTTP(S) JSON URL. Requests are SSRF-guarded at
  connect time: private/loopback/link-local/metadata/CGNAT addresses, odd IP
  encodings, unsafe schemes/ports and redirects to any of those are refused, and
  the connection is pinned to the validated IP (DNS-rebinding safe).
* On a fetch failure the **last good value** keeps showing.
* **Browser source**: *not enabled in this release* (see Known limits).
* User text never reaches the FFmpeg command line; it is delivered through
  atomically replaced text files.

## Bandwidth

One destination at 4 Mbps video + 128 kbps audio is about **1.34 TB / 30 days**;
at 8 Mbps about **2.63 TB / 30 days**. Each extra destination adds the same again.
YouTube lists 8 Mbps as recommended for 720p30 and 3 Mbps as the minimum; 4 Mbps
is offered as an economical choice, not an official recommendation.

## Known limits (honest)

* **Browser source is not implemented in this release.** Use Data overlays. It is
  deliberately absent rather than shipped unsandboxed.
* Now Playing reads `<run>/<playlist>.nowplaying` if present; the playlist engine
  does not write that file yet, so it shows only your prefix text for now.
* Slideshow / playlist scheduling beyond the existing Playlist features are not part of this release.
* Switching a stream between Relay and Studio is a normal stop/start (a short
  interruption); it is not seamless.
* 1080p is accepted by the model but not benchmarked; do not assume a cheap VPS can do it.
* Free-tier concurrency, 72-hour soak and 4-vCPU numbers: see `docs/bluestream/TESTS.md`.
