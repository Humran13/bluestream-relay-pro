# BlueStream Relay Pro

**Version 0.1.0 — foundation release**

BlueStream Relay Pro is a **self-hosted 24/7 media relay server** for Ubuntu
VPS systems. You install it on your own VPS, point it at a supported media
source, and it exposes a clean, stable **HLS/M3U8 output URL** you can use in
WordPress sites, HTML5/hls.js players, VLC, IPTV players, custom websites and
mobile apps.

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
- [Manager usage](#manager-usage)
- [Web console](#web-console)
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
Source (local file / playlist / HLS / RTMP / RTMPS / RTSP / HTTP media)
   |
   v
FFmpeg  (stream copy, HLS muxer, runs as unprivileged bluestream-relay)
   |
   v
/var/www/bluestream/hls/relay/<name>/index.m3u8  (fresh sliding window)
   |
   v
nginx   (MIME types, cache headers, CORS, HTTPS via Let's Encrypt)
   |
   v
https://DOMAIN/hls/relay/<name>/index.m3u8
https://DOMAIN/player/?relay=<name>
```

Key components:

- **systemd** supervises every relay and playlist (restart with backoff,
  boot enable/disable, journald logs).
- **Nginx** serves HLS with correct MIME types, cache headers and CORS.
- **FFmpeg / ffprobe** does the pulling, probing and HLS packaging.
- **Bash** manager (`bluestream-manager`) is the administration interface.
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
destroying existing relays, playlists or media. See `docs/installation.md`
for full details.

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

An optional authenticated **web console** (Gunicorn + Flask, `127.0.0.1:8080`
behind nginx at `https://DOMAIN/console/`) provides a professional dashboard
for the most common tasks. All management remains available through
`bluestream-manager`; the console never bypasses the root-only engine.

Available pages (GUI-1C.1):

- **Dashboard** — server/engine overview and quick actions.
- **Streams** — list relays, start/stop/restart, and **create a stream** from a
  source URL (HLS, HTTP media, RTMP/RTMPS, RTSP). The URL is stored as data
  only and displayed redacted; new streams start **stopped**.
- **Media Library** — list managed media, **upload** a video file, and create a
  local-file stream from it. Uploads are staged in a narrow web-writable
  directory, probed with ffprobe, and imported by the root bridge — a file is
  never overwritten and the staged copy is removed after every import attempt.
- **Playlists** — list playlists and control them (start/stop/restart).

Security properties:

- Every write is a POST guarded by CSRF tokens and authentication, and follows
  Post/Redirect/Get (no mutation on refresh).
- Source URLs, stream names and media names are validated against the engine's
  own rules (and again by the engine) before any bridge call.
- Uploads are capped (10 GiB default, configurable via server.conf and mirrored in nginx) and restricted to
  common media extensions; the web process can only write the upload staging
  directory, never managed media.

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
  privilege drop that **fails closed**.
- Config directories are root-only (0700/0600). No `chmod 777` anywhere.
- Names, paths and URLs are strictly validated; no `eval`, no sourcing of
  untrusted config files.
- Source URL credentials are redacted in output and never written to M3U8.
- systemd hardening is applied to relay/playlist units.
- Backups are root-only; restore validates archive contents.

See `docs/security.md` for the full model.

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

- **No** DRM, website scraping, YouTube/Twitch download bypass, proprietary
  CDN, DASH, recording archive, GPU transcoding, cluster management, billing,
  accounts or web admin dashboard in version 1.
- Ubuntu 18.04 is not officially supported.
- No real-VPS validation has been performed yet (see below).

Deferred roadmap possibilities (documented but not built): CDN/reverse-proxy
mode, hardware-accelerated compatibility transcoding, recording archive,
multi-server orchestration, web dashboard, billing/accounts.

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
├── install.sh
├── uninstall.sh
├── bluestream-manager
├── status.sh
├── VERSION
├── README.md
├── LICENSE
├── CHANGELOG.md
├── lib/
│   ├── common.sh        # paths, validation, logging, safe config parsing
│   ├── relay.sh         # relay CRUD, lifecycle, ffmpeg args
│   ├── playlist.sh      # playlist CRUD, compat checks, concat handling
│   ├── media.sh         # managed media import/list/inspect/remove
│   ├── probe.sh         # ffprobe probing, compat report, bandwidth math
│   ├── health.sh        # HEALTHY/STARTING/STALE/... state logic
│   ├── nginx.sh         # nginx config generation, validation
│   ├── ssl.sh           # certbot management
│   ├── firewall.sh      # UFW management
│   ├── osdetect.sh      # Ubuntu version detection
│   ├── diagnostics.sh   # diagnostics checklist
│   ├── backup.sh        # config backup/restore
│   └── selftest.sh      # end-to-end self-test
├── config/
│   ├── nginx/           # site templates + HLS/player location snippets
│   └── systemd/         # unit templates + runtime privilege-drop wrappers
├── docs/                # installation, sources, playlists, wordpress,
│                        # troubleshooting, security
└── web/                 # bundled hls.js player (index.html, player.js, style.css)
```

## License

Commercial license — all rights reserved. See `LICENSE`. The final license
text will be completed before commercial distribution.


