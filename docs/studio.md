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

## Editor

Drag layers on the 16:9 canvas, drag the handles to resize, use the arrow keys to nudge (Shift = coarse,
Alt+arrows = resize). Layers snap to the canvas edges, centre and other layers (hold Alt to disable).
Crop an image overlay with the crop fields; the canvas shows the cropped result. List order is stacking order
(last = on top); use the arrows to reorder and the eye/visible box to hide a layer. The canvas background is a
real frame of the selected source (media file, slideshow image, or the newest HLS segment of a Relay/Playlist);
if none is available it stays neutral. Works with touch and on phones.

## Sources and overlays

| Source / overlay | Notes |
|---|---|
| Media file | looped 24/7 |
| Image slideshow | seconds per image, optional shuffle (same list = same order); rendered once, then copied |
| Audio file | background colour or image, optional live waveform strip (the waveform needs a live encode) |
| Existing Relay / Playlist | read from the private local bus |
| Text, rect, image (+crop), video picture-in-picture | PiP is looped and muted and needs a live encode |
| Clock, countdown, count-up, Pomodoro, ticker | absolute times + timezone, survive restarts |
| Now Playing | real current item of a Playlist source; empty when unknown (never guessed) |
| Data | public JSON URL + path + refresh + number format |
| Browser (advanced, OFF by default) | see below |

## Destination ownership

A destination can be attached to several streams but only ONE publisher may push to it at a time, enforced by a
kernel lock (released automatically if the owner dies). A stream that cannot publish shows **BLOCKED** with the owner's
name; **Switch to this stream** stops the current owner's publisher for that destination, starts yours, and reinstates
the previous owner if yours fails. Switching to go back is the same button on the other stream. Switching interrupts
viewers briefly: it is controlled and recoverable, not seamless.

## Health

The Health panel shows encoder speed (1.00x = real time), frame rate, dropped frames, encoder CPU and RAM, restarts,
the last failure, HLS output state and per-destination publisher state. The verdict comes from FFmpeg's own progress
reports: a running process with frozen frames is reported STALLED, not healthy.

## Data overlays and security

* Data overlays fetch a **public** HTTP(S) JSON URL. Requests are SSRF-guarded at
  connect time: private/loopback/link-local/metadata/CGNAT addresses, odd IP
  encodings, unsafe schemes/ports and redirects to any of those are refused, and
  the connection is pinned to the validated IP (DNS-rebinding safe).
* On a fetch failure the **last good value** keeps showing.
* User text never reaches the FFmpeg command line; it is delivered through
  atomically replaced text files.

## Browser source (advanced; disabled by default)

Cropping a screenshot does **not** make a page cheap: the browser still loads and runs the whole page. Prefer a Data
overlay whenever the value is available from an API. When you really need a page: one bounded capture every 15 s -
1 h (no browser kept running), only https pages on public hostnames, only hosts you list may load (each pinned to a
validated public IP; every request is checked inside the browser too), memory/CPU/task caps and a kernel IP deny list
via a dedicated systemd unit, the last good image stays on screen when a capture fails, unchanged captures are not
rewritten, and a hung page is killed together with all its processes. Chromium's sandbox is required: if it cannot start
(for example Ubuntu 24.04 AppArmor blocks user namespaces) the feature refuses to run instead of dropping the sandbox.
It stays off unless `BLUESTREAM_STUDIO_BROWSER=1` is set **and** Chromium is installed **and** the sandbox self-test passes.
**It has not been verified on a real host yet** - treat it as unsupported until the staging checklist is complete.

## Bandwidth

One destination at 4 Mbps video + 128 kbps audio is about **1.34 TB / 30 days**;
at 8 Mbps about **2.63 TB / 30 days**. Each extra destination adds the same again.
YouTube lists 8 Mbps as recommended for 720p30 and 3 Mbps as the minimum; 4 Mbps
is offered as an economical choice, not an official recommendation.

## Known limits (honest)

* Now Playing is accurate to about a second or two (it is reconstructed from the playlist's start time and item durations).
* Playlist scheduling stays in the existing Playlist feature; Studio adds none.
* Switching a stream between Relay and Studio is a normal stop/start (a short
  interruption); it is not seamless.
* 1080p is accepted by the model but not benchmarked; do not assume a cheap VPS can do it.
* Free-tier concurrency, 72-hour soak and 4-vCPU numbers: see `docs/bluestream/TESTS.md`.
