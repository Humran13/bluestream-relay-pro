# BlueStream Relay Pro

**Version 0.1.0 — foundation release**

## One-Line Installation

```bash
curl -fsSL https://raw.githubusercontent.com/Humran13/bluestream-relay-pro/main/bootstrap.sh | sudo bash
```

This clones the project to `/usr/local/src/bluestream-relay-pro` and runs its
`install.sh`. It never touches existing BlueStream data on re-run (relays,
playlists, media, destinations, schedules, backups, web admin state). Pass
installer flags after `--`, e.g.
`... | sudo bash -s -- --domain video.example.com --assume-yes`.

BlueStream Relay Pro is a **self-hosted 24/7 media relay AND multistream
server** for Ubuntu VPS systems. You install it on your own VPS, point it at
a supported media source, and it exposes a clean, stable **HLS/M3U8 output
URL** you can use in WordPress sites, HTML5/hls.js players, VLC, IPTV
players, custom websites and mobile apps — and, independently, pushes the
same source to any number of your own streaming destinations (YouTube,
Facebook, Twitch, Rumble, Instagram, TikTok, or any custom RTMP/RTMPS
endpoint).

```text
M3U8:   https://example.com/hls/relay/news/index.m3u8
Player: https://example.com/player/?relay=news
```

The public M3U8 URL stays **stable** even when the source restarts or
temporarily reconnects.

> **Validation status:** 0.1.0 is an original, independently implemented
> foundation. It has **not yet been real-VPS tested**. Do not run a paid
> production channel on it until you have completed the self-test and your
> own VPS validation. See [Validation status](#validation-status).

---

## Table of contents

- [What it is](#what-it-is)
- [Use cases](#use-cases)
- [Architecture](#architecture)
- [Supported source types](#supported-source-types)
- [Stream-copy philosophy](#stream-copy-philosophy)
- [Codec recommendations](#codec-recommendations)
- [Installation](#installation)
  - [Existing web server / control panel compatibility](#existing-web-server--control-panel-compatibility)
- [Manager usage](#manager-usage)
- [Web console](#web-console)
- [Destinations & outbound publishing](#destinations--outbound-publishing)
- [Playlists](#playlists)
- [WordPress / websites](#wordpress--websites)
- [Security model](#security-model)
- [Bandwidth considerations](#bandwidth-considerations)
- [1080p / 4K considerations](#1080p--4k-considerations)
- [Troubleshooting](#troubleshooting)
- [Limitations](#limitations)
- [Validation status](#validation-status)
- [Project structure](#project-structure)
- [License](#license)

## What it is

BlueStream Relay Pro is a **stream copy / remux / repackage** relay. For
compatible H.264 + AAC sources it copies video and audio without re-encoding
(`-c:v copy -c:a copy`), then re-packages the stream into clean HLS segments
served by nginx. This keeps CPU use low and supports high-resolution sources.

## Use cases

- 24/7 news or live channels embedded in WordPress sites.
- Looping local file channels (a video file played continuously).
- Playlist channels (several local files played sequentially, looping).
- Relaying remote HLS, RTMP, RTMPS or RTSP sources to your own stable URL.
- Feeding HLS output to VLC, IPTV players and mobile apps.

## Architecture

```text
Source (local file / playlist / HLS / RTMP / RTMPS / RTSP / HTTP media /
        YouTube or other public webpage, resolved via yt-dlp)
   |
   v
FFmpeg  (stream copy, runs as unprivileged bluestream-relay)
   |
   v
PRIVATE LOCAL RTMP BUS (nginx-rtmp, loopback-only)
   |-- nginx-rtmp HLS  -> /var/www/bluestream/hls/relay/<name>/index.m3u8
   |                       -> nginx -> https://DOMAIN/hls/relay/<name>/index.m3u8
   |-- publisher  -> your YouTube Live ingest
   |-- publisher  -> your Facebook Live ingest
   |-- publisher  -> your Twitch ingest
   |-- publisher  -> your Rumble / Instagram / TikTok / custom RTMP(S) ingest
   `-- ... one independent publisher process per attached, enabled destination
```

Every destination has its own FFmpeg process. If one destination's
connection drops, only that destination reconnects (fast, automatic) —
the website HLS output, the source relay/playlist itself, and every other
destination are completely unaffected.

Key components:

- **systemd** supervises every relay, playlist and outbound publisher
  (restart with backoff, boot enable/disable, journald logs). A publisher's
  generated `BindsTo=`/`After=` drop-in stops it automatically when its
  source relay/playlist stops.
- **Nginx** serves HLS with correct MIME types, cache headers and CORS, and
  also runs the private loopback RTMP ingest/distribution bus (`nginx-rtmp`).
- **FFmpeg / ffprobe** does the pulling, probing, HLS packaging and outbound
  publishing.
- **Bash** manager (`bluestream-manager`) and an optional authenticated
  **web console** are the administration interfaces.
- **No frameworks** — no React, Node dashboard, Electron or mobile apps.

## Supported source types

| Type | Example |
| --- | --- |
| Local file | `/var/lib/bluestream/media/video.mp4` (optional 24/7 loop) |
| Local playlist | multiple managed media files, sequential + loop |
| Remote HLS/M3U8 | `https://source.example.com/live/index.m3u8` |
| RTMP | `rtmp://example.com/live/channel` |
| RTMPS | `rtmps://example.com/live/channel` |
| RTSP | `rtsp://camera.example.com/stream` (TCP transport by default) |
| HTTP/HTTPS media | `https://example.com/videos/program.mp4` (optional loop) |

Only direct media/stream URLs are accepted. BlueStream does not scrape
websites, and does not implement DRM bypass or authentication bypass. The
administrator is responsible for having permission to relay the content.

## Stream-copy philosophy

- Compatible sources (H.264 + AAC) are relayed with `-c:v copy -c:a copy`.
- No automatic transcoding, ever, in version 1.
- Incompatible codecs (AV1, HEVC/H.265, VP9, unsupported audio) produce
  clear warnings; the administrator decides whether to continue, reject, or
  set up a compatibility transcode manually.
- CPU-based 4K transcoding is explicitly warned against — it is extremely
  expensive and slow on typical VPS hardware.

## Codec recommendations

Preferred compatibility profile:

- Video: **H.264 / AVC**
- Audio: **AAC**

For playlist channels, prefer uniform **H.264 + AAC** media. Before a
playlist starts, every file is probed and mismatches (codecs, resolution,
frame rate, sample rate, channel layout) are reported clearly.

## Installation

Target: **Ubuntu Server 20.04 / 22.04 / 24.04 / 26.04**. Ubuntu 18.04 may
work but is not officially supported by 0.1.0.

```bash
cd bluestream-relay-pro
sudo bash install.sh
```

With SSL + firewall in one shot:

```bash
sudo bash install.sh \
  --domain video.example.com \
  --email admin@example.com \
  --with-ssl \
  --with-ufw \
  --assume-yes
```

Re-running `install.sh` repairs/updates the installation **without**
destroying existing relays, playlists, destinations, media or schedules. See
`docs/installation.md` for full details.

### Domain input

The public-domain prompt (and `--domain`) accepts `stream.example.com`,
`http://stream.example.com`, `https://stream.example.com`, and a trailing
slash — it normalizes to the bare hostname before validating. A path, query
string, embedded credentials, a port, or shell-hostile characters are
rejected rather than silently stripped.

### Internal web console port

Gunicorn always binds `127.0.0.1:<port>` only — never a public address.
`install.sh` selects the port automatically: **8080 if it is free**,
otherwise the first free port from a small fixed candidate list. The
selection is persisted in `/etc/bluestream/server.conf` (`WEB_PORT=`) and
**reused as-is on every reinstall/update** — it never changes just because
something else now happens to be using 8080. nginx's `/console/` reverse
proxy and the Gunicorn unit are always rendered from this one value, so they
can never drift apart.

### Existing web server / control panel compatibility

`install.sh` detects the host **before** installing or changing anything,
and never requires CloudPanel or any other panel:

| Detected environment | Strategy | What happens |
| --- | --- | --- |
| Fresh VPS (no web server) | **Owned** | BlueStream installs and manages nginx directly (today's default). |
| Plain Nginx, active, no panel | **Owned** | Same as above — your existing nginx is reused, not replaced. |
| CloudPanel / aaPanel / HestiaCP / Plesk / ISPConfig / Virtualmin/Webmin / Nginx Proxy Manager | **Isolated** | BlueStream never touches the panel's nginx/Apache config, service, or certificates. It installs and runs its **own** independent nginx instance (own config root, own systemd unit, own ports) purely for its RTMP bus, HLS output and web console. |
| Apache and/or Caddy present, no nginx | **Isolated** | Same independent-instance behavior; your existing web server is never modified. |
| Nginx present but inactive, or anything ambiguous | **Isolated** | Treated cautiously rather than guessed as a fresh install. |

The chosen strategy is detected once, displayed before anything is touched,
recorded in `server.conf`, and **reused on every reinstall/update** — it
never flips later just because the live environment looks different that
day. An existing nginx binary that is **not** managed by apt/dpkg (common for
some panels' own builds) is never touched either way — the installer aborts
before installing the `nginx` package rather than risk overwriting it.

In isolated mode: your panel/Apache/Caddy keeps its ports, config, and
certificates exactly as they were. BlueStream's independent instance picks
its own loopback-adjacent ports (shown during install and in
`bluestream-manager`'s Server Status) for its RTMP bus, HLS output and web
console. To reach it from your real domain, add one reverse-proxy rule in
your panel pointing a (sub)domain at `http://127.0.0.1:<isolated-http-port>/`
and let the panel terminate HTTPS — `--with-ssl` is not supported for the
isolated instance itself (it would require editing the panel's nginx
config, which this installer will never do); see
`docs/installation.md` for the exact steps and a fully worked example.

## Manager usage

```bash
sudo bluestream-manager
```

Organised into submenus:

- **Server Status** — overview of the server and BlueStream.
- **Relay Management** — add/list/start/stop/restart/enable/disable/edit/
  probe/URLs/logs/health/bandwidth/remove.
- **Playlist Management** — create/list/add/remove/reorder/show/compat/
  start/stop/restart/enable/disable/URLs/health/logs.
- **Media Management** — import/list/inspect/remove managed media.
- **Server Management** — nginx test/reload/restart, SSL, firewall,
  diagnostics, self-test.
- **Backup / Restore** — configuration backups (media excluded by default).

Non-interactive commands are also available, for example:

```bash
sudo bluestream-manager relay add news --type remote-hls --url "https://..." --start --enable
sudo bluestream-manager playlist add channel1 video01.mp4 video02.mp4
sudo bluestream-manager selftest
sudo bluestream-manager diagnostics
sudo bluestream-manager relay url news
```

## Web console

An optional authenticated **web console** (Gunicorn + Flask behind nginx at
`https://DOMAIN/console/`) provides a professional dashboard for the most
common tasks. All management remains available through `bluestream-manager`;
the console never bypasses the root-only engine. Gunicorn listens on an
**auto-selected loopback port** (127.0.0.1 only, never public) — see
[Internal web console port](#internal-web-console-port).

Available pages:

- **Dashboard** — server/engine overview, live CPU/RAM/storage metrics and
  quick actions.
- **Streams** — list relays, start/stop/restart, **create a stream** from a
  source URL (HLS, HTTP media, RTMP/RTMPS, RTSP, or a public YouTube/webpage
  URL resolved via yt-dlp), edit its source, and attach destinations.
- **Media Library** — list managed media, **upload** a video file, and create a
  local-file stream from it. Uploads are staged in a narrow web-writable
  directory, probed with ffprobe, and imported by the root bridge — a file is
  never overwritten and the staged copy is removed after every import attempt.
- **Playlists** — create/list playlists, control them (start/stop/restart),
  schedule a one-time future start, and attach destinations.
- **Destinations** — create/enable/disable/delete outbound streaming
  destinations and see live per-destination publish status (see next
  section).

Security properties:

- Every write is a POST guarded by CSRF tokens and authentication, and follows
  Post/Redirect/Get (no mutation on refresh).
- Source URLs, stream names, media names and destination fields are validated
  against the engine's own rules (and again by the engine) before any bridge
  call.
- Uploads are capped (10 GiB default, configurable via server.conf and mirrored in nginx) and restricted to
  common media extensions; the web process can only write the upload staging
  directory, never managed media.
- A destination's stream key is written only to a root-only config file,
  delivered to the engine over stdin (never argv/env/URL), and is **never**
  shown in the console, an API response, a log, or a flash message.

## Destinations & outbound publishing

Attach one or more **destinations** to a stream or playlist and BlueStream
pushes that same source to all of them simultaneously, independently of the
website HLS output:

- Supported platform labels: YouTube, Facebook, Twitch, Rumble, Instagram,
  TikTok, and Custom RTMP/RTMPS. The platform value is a label only — you
  always supply your own server URL and stream key exactly as the platform
  gives them to you for manual/encoder publishing. BlueStream never hard-codes
  an ingest URL and never automates a platform login.
- Each attached + enabled destination runs its **own** FFmpeg process reading
  from the private local RTMP bus and publishing to your destination. One
  destination disconnecting/reconnecting never affects the website HLS feed,
  the source relay/playlist, or any other destination.
- Per-destination status: **Disabled**, **Stopped** (source not running),
  **Starting**, **Live** (real data flow confirmed, not just "process
  exists"), **Reconnecting**, or **Failed**, plus a restart count and a
  bounded, non-sensitive failure reason. The stream key and destination server
  URL are never part of this status.
- Starting/stopping/restarting the source, attaching/detaching a destination,
  and enabling/disabling a destination all reconcile the running publisher set
  automatically — there is no separate "start publishing" button to forget.

See `docs/security.md` for the full write-up on how the stream key is
protected end-to-end, including the one documented residual limitation.

## WordPress / websites

Every relay/playlist exposes an M3U8 URL and a player URL. Embed with an
iframe, or use the generic hls.js snippet:

```html
<video id="player" controls playsinline></video>
<script src="https://cdn.jsdelivr.net/npm/hls.js@1"></script>
<script>
  var video = document.getElementById("player");
  var hls = new Hls();
  hls.loadSource("https://example.com/hls/relay/news/index.m3u8");
  hls.attachMedia(video);
</script>
```

Full details, including a no-plugin WordPress workflow, are in
`docs/wordpress.md`.

## Security model

- FFmpeg runs as the unprivileged `bluestream-relay` user via a `setpriv`
  privilege drop that **fails closed** — for the source relay/playlist AND
  for every outbound publisher.
- Config directories are root-only (0700/0600), including destination configs
  (stream keys). No `chmod 777` anywhere.
- Names, paths and URLs are strictly validated; no `eval`, no `shell=True`, no
  sourcing of untrusted config files, no generic command execution anywhere
  in the browser → web-ctl → engine chain.
- Source URL credentials are redacted in output and never written to M3U8. A
  destination's stream key is never exposed in the console, an API response,
  a log, a flash message, or a diagnostic dump.
- systemd hardening is applied to relay/playlist/publisher units; a publisher
  additionally gets no writable filesystem path at all.
- Backups are root-only; restore validates archive contents.

See `docs/security.md` for the full model, including the honestly-documented
residual stream-key exposure (same-service-account `/proc` visibility between
BlueStream's own FFmpeg processes) and how it is bounded.

## Bandwidth considerations

Outbound bandwidth is approximately:

```text
source bitrate x number of viewers
```

when every viewer fetches HLS directly from this VPS. For example, a 4 Mbps
source with 25 simultaneous viewers needs ~100 Mbps outbound. A small VPS
cannot serve unlimited viewers — for large audiences, place a **CDN or
reverse proxy** in front of the public M3U8 URL.

Use the built-in estimator:

```bash
sudo bluestream-manager relay bandwidth news 50
```

## 1080p / 4K considerations

- Stream copy relays high-resolution sources without re-encoding, which keeps
  CPU low but **does not reduce bandwidth**.
- 4K capability depends on the source codec and bitrate, VPS network
  bandwidth, server resources, viewer count and player compatibility.
- 4K is **not guaranteed** on every VPS and is never advertised as such.
- A CPU-based 4K transcode may be extremely slow and is not something
  BlueStream does automatically. Hardware acceleration is a future roadmap
  item, not a 0.1.0 requirement.

## Troubleshooting

```bash
sudo bluestream-manager diagnostics
sudo bluestream-manager selftest
sudo bluestream-manager relay health news
journalctl -u bluestream-relay@news --no-pager -n 100
```

Health states: `HEALTHY`, `STARTING`, `STALE`, `STOPPED`, `DISABLED`,
`FAILED`, `UNKNOWN`. A service being "active" is not enough — fresh HLS
segments are required for `HEALTHY`.

See `docs/troubleshooting.md`.

## Limitations

- **No** DRM bypass, authenticated/private-content scraping, proprietary CDN,
  DASH, recording archive, GPU transcoding, cluster management, billing,
  accounts, OAuth platform integrations, or browser-based scene/studio
  compositor (overlays, scenes, transitions, webcam/screen mixing) yet.
- Outbound publishing requires the server URL + stream key the destination
  platform gives you for manual/encoder publishing; BlueStream does not log
  into any platform account on your behalf.
- No generic "reconnect while keeping the same process" exists for an
  outbound RTMP publish (ffmpeg has none); a destination disconnect ends that
  one publisher process and systemd restarts it within seconds — this is by
  design, not a gap, but it means a few seconds of a destination going dark on
  a bad connection is expected and normal.
- Ubuntu 18.04 is not officially supported.
- No real-VPS validation has been performed yet (see below).

Deferred roadmap possibilities (documented but not built): CDN/reverse-proxy
mode, hardware-accelerated compatibility transcoding, recording archive,
multi-server orchestration, billing/accounts, OAuth platform integrations,
and "BlueStream Studio" (a browser-based scene compositor sitting between the
source and the private RTMP bus — the publisher architecture was designed to
leave room for this without rework).

## Validation status

| Item | Status |
| --- | --- |
| Shell syntax check (`bash -n`) | Automated |
| ShellCheck | Not installed during development (per policy) |
| End-to-end self-test | Implemented; run on the VPS via `bluestream-manager selftest` |
| Real VPS (Ubuntu 20.04) | **Not yet tested** |
| Real VPS (Ubuntu 22.04) | **Not yet tested** |
| Real VPS (Ubuntu 24.04) | **Not yet tested** |
| Real VPS (Ubuntu 26.04) | **Not yet tested** |
| WordPress embedding | Implemented; pending live validation |
| IPTV / VLC playback | Implemented; pending live validation |

The self-test generates a tiny H.264/AAC sample, starts a real systemd
relay, verifies FFmpeg runs as `bluestream-relay`, waits for fresh HLS,
fetches the public M3U8, verifies it with ffprobe, and removes its own
artifacts. **Recommended first real-VPS test:** run `install.sh`, run the
self-test, then add one local-file relay and one remote-HLS relay and leave
them running for 48 hours while monitoring health.

## Project structure

```text
.
├── bootstrap.sh         # public one-line installer (clones + runs install.sh)
├── install.sh
├── uninstall.sh
├── bluestream-manager
├── web-ctl              # root-only JSON bridge used by the web console
├── status.sh
├── VERSION
├── README.md
├── LICENSE
├── CHANGELOG.md
├── lib/
│   ├── common.sh        # paths, validation, logging, safe config parsing
│   ├── relay.sh         # relay CRUD, lifecycle, ffmpeg args
│   ├── playlist.sh      # playlist CRUD, compat checks, concat handling
│   ├── destinations.sh  # outbound destination CRUD + stream/playlist attachments
│   ├── publisher.sh     # Phase 1C: outbound multistream publisher engine
│   ├── schedule.sh      # one-time future playlist start scheduling
│   ├── resolver.sh       # public-webpage (yt-dlp) source resolver
│   ├── youtube.sh       # YouTube URL classification helpers
│   ├── media.sh         # managed media import/list/inspect/remove
│   ├── probe.sh         # ffprobe probing, compat report, bandwidth math
│   ├── health.sh        # HEALTHY/STARTING/STALE/... state logic
│   ├── nginx.sh         # nginx config generation, validation
│   ├── webconsole.sh    # web console account/state/port/unit provisioning
│   ├── ssl.sh           # certbot management
│   ├── firewall.sh      # UFW management
│   ├── osdetect.sh      # Ubuntu version detection
│   ├── diagnostics.sh   # diagnostics checklist
│   ├── backup.sh        # config backup/restore
│   └── selftest.sh      # end-to-end self-test (relay + publisher)
├── config/
│   ├── nginx/           # site templates + HLS/player/console location snippets
│   └── systemd/         # unit templates + runtime privilege-drop wrappers
│       ├── bluestream-relay@.service / run-relay.sh
│       ├── bluestream-playlist@.service / run-playlist.sh
│       ├── bluestream-playlist-start@.service / run-playlist-start.sh
│       ├── bluestream-publisher@.service / run-publisher.sh
│       └── bluestream-web.service.template
├── webapp/              # Flask web console (app.py, engine.py, templates/)
├── docs/                # installation, sources, playlists, wordpress,
│                        # troubleshooting, security
└── web/                 # bundled hls.js player (index.html, player.js, style.css)
```

## License

Commercial license — all rights reserved. See `LICENSE`. The final license
text will be completed before commercial distribution.


