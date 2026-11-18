# Sources

BlueStream Relay Pro accepts the following source types. Every relay exposes
a stable public HLS URL regardless of source restarts or reconnects.

## Source types

| Type | Example | Notes |
| --- | --- | --- |
| `local-file` | `/var/lib/bluestream/media/video.mp4` | Loops continuously when LOOP=yes |
| `remote-hls` | `https://source.example.com/live/index.m3u8` | Pulls a remote HLS stream |
| `rtmp` | `rtmp://example.com/live/channel` | RTMP ingest |
| `rtmps` | `rtmps://example.com/live/channel` | RTMP over TLS |
| `rtsp` | `rtsp://camera.example.com/stream` | Uses TCP transport by default |
| `http-file` | `https://example.com/videos/program.mp4` | Direct media file, optional loop |

Only direct media/stream URLs are accepted. A normal webpage URL is **not** a
media source. BlueStream does not scrape websites and does not bypass DRM or
authentication. The administrator is responsible for having permission to
relay the content.

## Adding a relay

Interactive:

```
sudo bluestream-manager
Relay Management > Add Relay
```

Non-interactive:

```bash
sudo bluestream-manager relay add news \
  --type remote-hls \
  --url "https://source.example.com/live/index.m3u8" \
  --start \
  --enable
```

Supported `relay add` options:

- `--type <type>` — one of the types above
- `--url <url-or-path>` — the source
- `--loop yes|no` — for `local-file` and `http-file`
- `--restart-sec <1-300>` — systemd restart delay on crash
- `--note <text>` — internal note
- `--start` — start immediately
- `--enable` — enable at boot

## Local video: 24/7 or one-time playback

The web console exposes an explicit **24/7 Continuous Streaming** checkbox
when a stream is created from uploaded media. It is on by default. Creation
never starts the stream: press **Start** when ready.

With 24/7 mode on, Start:

- passes `-stream_loop -1` to FFmpeg, so the media repeats indefinitely;
- keeps the stable HLS output and attached YouTube/RTMP destinations active;
- retries unexpected FFmpeg endings indefinitely with capped backoff; and
- enables the relay's systemd unit so it starts after a VPS reboot.

An explicit **Stop** is never treated as a crash and does not cause an
immediate restart. Stop also removes boot persistence, so a stream you stopped
stays stopped after a VPS reboot until you press **Start** again. **Restart** reapplies the same 24/7 and boot-persistence rules.

Clear 24/7 mode to play the file exactly once. A clean end is recorded as
`COMPLETED`, and disabling 24/7 also removes boot persistence. Existing streams
are not migrated or changed automatically: on **Streams**, open **Edit 24/7**
for a stopped local-video stream (for example `dino`), enable the option, save,
then press **Start**. Names, media, destination attachments and public HLS URLs
are preserved.

## Probing and compatibility

BlueStream uses `ffprobe` before configuring a source. The probe report
shows:

- source type
- video codec
- audio codec
- width x height
- frame rate
- bitrate (when detectable)
- duration (when applicable)
- estimated bandwidth
- whether stream copy is recommended

### Preferred profile

- Video: **H.264 / AVC**
- Audio: **AAC**

If the source is already compatible, the relay uses **stream copy**
(`-c:v copy -c:a copy`) — no transcoding.

If something else is detected (AV1, HEVC/H.265, VP9, unsupported audio) the
manager warns clearly that HLS/player compatibility may vary. BlueStream
will **not** automatically start an expensive software transcode. The
administrator may:

1. Continue with stream copy where technically possible
2. Reject the incompatible source
3. Optionally set up a compatibility transcode manually (advanced)

> **4K / high resolution:** stream copy supports high-resolution sources at
> the cost of bandwidth and muxing overhead. Whether 4K works depends on the
> source codec and bitrate, VPS network bandwidth, server resources, viewer
> count and player compatibility. It is **not guaranteed** on every VPS, and
> a CPU-based 4K transcode may be extremely expensive and slow. Hardware
> acceleration is not part of version 1.

## Remote source resilience

- HTTP/HLS sources use FFmpeg reconnect options (`-reconnect`,
  `-reconnect_streamed`, `-reconnect_delay_max`) where supported.
- RTSP uses TCP transport by default.
- RTMP/RTMPS/RTSP rely on FFmpeg behaviour plus systemd restart supervision.
- Health checks distinguish `HEALTHY`, `STARTING`, `STALE`, `STOPPED`,
  `FAILED` and `UNKNOWN`; a service being "active" is not enough — fresh HLS
  segments are required for `HEALTHY`.

## Source URL credentials

Source URLs may contain tokens, usernames, passwords or signed query
strings. Relay configuration files are root-only (`/etc/bluestream/relays/`,
mode 0600) and credentials are redacted in all manager/status output. The
source URL is never written into generated M3U8 files.

## Managing relays

```bash
sudo bluestream-manager relay list                 # list all relays
sudo bluestream-manager relay status news          # detailed status
sudo bluestream-manager relay start news           # start now
sudo bluestream-manager relay stop news            # stop
sudo bluestream-manager relay restart news         # restart
sudo bluestream-manager relay enable news          # enable at boot
sudo bluestream-manager relay disable news         # disable at boot
sudo bluestream-manager relay url news             # public M3U8 URL
sudo bluestream-manager relay player news          # web player URL
sudo bluestream-manager relay probe news           # probe / compatibility report
sudo bluestream-manager relay logs news            # recent service logs
sudo bluestream-manager relay health news          # health check
sudo bluestream-manager relay bandwidth news 50    # bandwidth estimate for 50 viewers
```
