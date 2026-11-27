# Studio live-production architecture (diagnosis and chosen design)

Branch `feature/studio-live-compositor` (from `feature/bluestream-studio`). Nothing here was deployed or run against the production VPS.

## 1. How the streaming stack really works (read from the code, not assumed)

```
 source ──► relay unit (run-relay.sh → supervise.sh → one FFmpeg) ──► private RTMP bus 127.0.0.1:1935/bluestream-relay/<name>
                                                                          ├─► nginx-rtmp ──► HLS  /hls/relay/<name>/index.m3u8   (public player + M3U8)
                                                                          └─► publisher@relay:<name>:<dest>  (one FFmpeg per destination, `-c copy`) ──► YouTube etc.
```
* A normal **relay** is `-c copy` where the source allows it (cheap). A looped local file uses FFmpeg's own `-stream_loop -1`; a **playlist** uses the concat demuxer with stream copy.
* A **Studio stream** is just another relay (`type=studio`). `run-relay.sh` asks `studio.cli argv` for the FFmpeg arguments and the planner (`planner.py`) picks the cheapest valid path:
  1. `direct-copy` – compatible source, no overlays → stream copy (no decode/encode);
  2. `prerender-then-copy` – finite source + STATIC overlays → one encode into a cache file, then stream copy forever;
  3. `live-composition` – changing overlays (clock, countdown, ticker, data, video PiP, browser), a live source with overlays, or the audio visualizer → **one** decode+compose+encode FFmpeg per output (libx264). Text/clock/countdown are `drawtext` filters that re-read small files (`reload=1`) written once a second by the `widgets` sidecar, so they are authoritative server-side timers that survive browser refreshes and restarts.
* **A compositor therefore already exists on the server**: published overlays are burned into the Studio stream's output, not drawn only in the browser. What was missing was proof (frame-level tests: added), a live picture in the editor, and a clear distinction between *watching* a stream and *changing what viewers see*.
* An existing relay (e.g. Dino) is never modified by Studio. A Studio project with source `relay:<name>` reads Dino's bus **as a second consumer** into its own separate stream (`bluestream-relay/<studio-name>`), so no loop is possible (a project cannot read from itself) and Dino keeps running untouched. Putting overlays on what viewers see requires moving the destination from Dino to the Studio stream: the existing, explicit, confirmed, rollback-protected **"Switch to this stream"** (kernel `flock` per destination). Studio never does that silently.
* FFmpeg limits that shape the design: stream copy cannot filter; `drawtext` can reload TEXT from a file but x/y/size/colour are fixed in the graph; `overlay` inputs cannot be added to a running graph; runtime commands need `zmq`/`sendcmd` plumbing that Ubuntu's FFmpeg exposes only through a client we do not ship. So: **content changes are hot (no interruption); structure changes are a staged restart of that one Studio stream** (never of Dino).

## 2. Chosen compositor architecture
One FFmpeg process per Studio output (kept). Rejected alternatives: GStreamer `compositor` (second media stack to install/secure/package, no gain for 1 background + N overlays); libobs (heavy GUI/GL dependencies, no headless server story on a small VPS); persistent per-layer processes (more encoders, more CPU). Dynamic changes:
* **Hot apply** – when only text-backed content changed (text, ticker, countdown target, clock zone, data URL/path, now-playing prefix…) and the running pipeline is a live composition, Apply updates the file-backed values with **zero interruption**.
* **Staged apply** – any structural change (add/remove/move/resize layer, colour, opacity, image, source, output profile) restarts only that Studio stream. The UI says so before, requires confirmation when it is running, reports the running revision afterwards, and the previous published revision remains one click away (rollback).
* Honest states: *draft saved* → *published (revision N)* → *pipeline running revision N* → *output healthy* (HLS fresh, encoder speed ≥ 0.95×). These are separate and shown separately.

## 3. Preview (what the canvas shows)
* Uploaded video: an authenticated, **on-demand** lightweight stream of the file (stream copy when browser-compatible, otherwise one 540p ultrafast transcode with `nice`), only while someone is previewing; capped concurrency; never touches a broadcast. Local pause/restart/loop/mute act on the `<video>` element only.
* Existing relay / playlist / the Studio output: the public **HLS** of that stream played with a vendored hls.js (read-only, no new encoder, no restart). Latency (~6–15 s with HLS) is stated in the UI. Disconnected / error / retry states are explicit.
* Slideshow / audio sources: a still of the first image / the background (they are not video).

## 4. CPU policy
Relay mode and stream copy stay the default. Opening Studio starts **no** encoder: the only new work is on-demand preview (copy, or a 540p transcode while viewing) and HLS reads. A composited stream costs one encode (≈ 1.5 cores at 720p30 `veryfast`; the UI refuses an output that cannot fit the machine). Encoders: software libx264 (default) and NVIDIA `h264_nvenc` **only after a real one-frame NVENC initialisation test succeeds**.

## 5. Historical restart counter (publisher unit, `NRestarts` 630)
`lib/publisher.sh` gives a 24/7 looped-file relay's publisher `Restart=always`, `StartLimitIntervalSec=0`, short `RestartSec`: every destination disconnect (YouTube idle drop, network blip, relay restart via `BindsTo=`) increments the counter and is retried forever. 630 cumulative restarts over weeks is therefore not by itself a failure signal; the cause must come from timestamps. `tools/canary/restart_forensics.sh` (read-only, redacting, owner-run) prints restart history per day/hour, exit codes/signals, current invocation age, encoder speed and HLS freshness so the pattern (periodic = loop/idle timeout, bursts = network, one-off = deploy) can be read. Not determinable from the repository.

## 6. Overlays on an EXISTING stream ("Edit existing stream") - investigation and design (round 12)
**Question:** can Studio composition be activated on a running relay (e.g. Dino) WITHOUT tearing down the destination (YouTube) publishing session?

**Measured (real nginx-rtmp as shipped, real FFmpeg, a recording 'platform' app; `tools/docker/exp_bus_takeover.py`, `webapp/test_studio_inplace_stack.py`):**
* The destination publisher pulls the bus stream `bluestream-relay/<name>`. When the PRODUCER of that bus name is replaced (old FFmpeg exits, new one publishes under the same name) nginx-rtmp keeps the publisher's play connection: the publisher FFmpeg never exits and the platform sees ONE ingest session. (A second publisher cannot start while the first is still publishing: "already publishing", so the old producer must be gone first.)
* BUT each producer starts its timestamps at 0. A backwards jump of less than ~10 s makes the copy-mode publisher stamp frames 1 ms apart: measured, 4 quick swaps produced 43 s of video inside 9.75 s of platform timeline (frozen then sped-up picture). Fix (implemented): every replacement producer carries `-output_ts_offset <seconds since this supervisor's first producer>`, so the bus has ONE continuous timeline; the swap stall shows up as an honest gap. After the fix the recording's media time tracks wall time (120.4 s vs 122.5 s) and A/V offset stays within -38..+22 ms across all swaps.
* Therefore the existing architecture CAN be extended: the relay keeps its name, bus stream, publishers, destinations and key. Only the FFmpeg that feeds the bus changes (`-c copy` <-> decode+overlay+encode). No second publisher, no destination switching.
* What it cannot do: a producer swap is not frame-seamless. The platform stops receiving packets for about 1.6-2.5 s (measured: activate 1.5, move 2.1, disable 2.0, remove 1.9 s) and the picture changes at an IDR frame; the connection stays up. YouTube's own tolerance of such a 2 s stall was NOT tested here.
* A supervisor started BEFORE this release has no swap handler (and SIGUSR1 would kill it), so for such a relay (Dino today) the first activation needs ONE relay restart: the destinations reconnect with the same key (about 10-20 s). The web bridge checks the caught-signal mask first and the dashboard asks for explicit confirmation. After that restart, activation, edits and removal are swaps.

**Mechanism:** dashboard writes `overlay-<relay>.json` (switch + project + nonce); `lib/relay.sh` builds the composed command from the relay's OWN input arguments (same loop/reconnect/resolver handling) via `studio.cli overlay-argv`, with the stream-copy command as the fallback for EVERY refusal (no switch, nothing visible, encoder unusable, admission refused, bad project, tripped breaker); `lib/supervise.sh` handles SIGUSR1 (kill current FFmpeg, wait until the bus name is free, rebuild at once, no backoff); `web-ctl relay_swap_source` signals only a supervisor that catches it. Text content changes are hot (sidecar files), everything structural is a swap. The picture keeps the source's size/frame rate; audio is stream-copied.
**Failure containment (tested):** corrupt project, unusable encoder, and a compositor that dies at once (breaker: a RETRY after a start that never proved healthy switches overlays off for that activation and restores stream copy in ~4 s; deliberate edits never trip it).
