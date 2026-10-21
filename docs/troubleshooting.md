# Troubleshooting

This guide covers common problems with BlueStream Relay Pro 0.1.0.

## First steps

Always start with the built-in checks:

```bash
sudo bluestream-manager diagnostics
sudo bluestream-manager selftest
```

The self-test generates a tiny H.264/AAC sample, runs a real relay through
systemd, verifies HLS output and the public URL, then cleans up after itself.

## Relay shows STALE / FAILED

A service being "active" is not enough — HLS must be fresh.

```bash
sudo bluestream-manager relay health news
sudo bluestream-manager relay status news
sudo bluestream-manager relay logs news
```

Common causes:

- The source is down or unreachable. Check with `relay probe news`.
- The source is not actually a media stream.
- FFmpeg crashed and systemd is backing off (`StartLimitBurst` reached).
- The HLS directory was removed while the service ran (restart the relay).

## HLS playlist is not being produced

Check the service logs:

```bash
journalctl -u bluestream-relay@news --no-pager -n 100
```

Look for FFmpeg errors such as:

- `No such file or directory` — local source path is wrong or media missing.
- `AAC bitstream not in ADTS format` — unusual AAC container; stream copy may
  need review.
- `Unable to open ...` — source unreachable or blocked.
- `Invalid data found when processing input` — source is not media.

## Public M3U8 returns 404

- Is the relay running and fresh? (`relay health news`)
- Is the site enabled? `ls -l /etc/nginx/sites-enabled/bluestream`
- Is nginx valid? `sudo bluestream-manager nginx test`
- Are the HLS directory permissions correct? HLS dirs must be owned by
  `www-data:www-data` (nginx-rtmp writes them) with mode 0750.

## HTTPS certificate problems

```bash
sudo bluestream-manager ssl status
sudo bluestream-manager ssl renew
```

Ensure the domain resolves to the server's public IP and ports 80/443 are
open before issuing a certificate.

## UFW blocks the stream

Check the firewall:

```bash
sudo bluestream-manager firewall status
```

Required rules: 22/tcp (SSH), 80/tcp, 443/tcp.

## CPU usage is high

Stream copy (`-c:v copy -c:a copy`) is designed to keep CPU low. High CPU
usually means:

- The source bitrate is very high (e.g. 4K). Muxing high-bitrate streams
  still consumes CPU and bandwidth.
- Something is decoding/encoding unexpectedly. Check `relay status` for the
  command; BlueStream never transcodes by default.
- Multiple relays on a small VPS.

## Disk usage grows

HLS uses a sliding window (`delete_segments`) and keeps roughly 24-36
seconds of segments per channel, so disk usage stays bounded. Managed media
in `/var/lib/bluestream/media` and backups in `/var/lib/bluestream/backups`
are the main disk consumers. Backups never include media unless you use
`backup --with-media`.

## I lost SSH access after enabling UFW

UFW is configured to allow your SSH port before it is enabled. If you locked
yourself out, use your provider's console/VNC to re-allow your SSH port.

## Restart storms

systemd limits restarts (`StartLimitIntervalSec=300`, `StartLimitBurst=10`).
If a relay keeps failing, it stops restarting and shows FAILED instead of
endlessly burning CPU. Fix the source, then `relay restart <name>`.

## An nginx worker crashed / repeated RTMP or HLS segfaults

BlueStream's RTMP ingest and HLS output are handled by a single nginx
worker process (`worker_processes 1;` - required because nginx-rtmp-module
keeps live-stream publish/play state per worker, so the publisher and any
local reader must land on the same one; see `lib/nginx.sh`
`nginx_ensure_single_worker`). `ngx_rtmp_module` itself is a third-party,
long-unmaintained module, and a crash inside it (visible as
`segfault at ... in ngx_rtmp_module.so`) is a known class of native defect
in that module, most often tied to HLS fragment-rotation/cleanup timers or
RTMP session teardown on publisher/subscriber disconnect - **not** something
BlueStream's own code can patch, since the module is a pre-built binary
package (`libnginx-mod-rtmp`).

**What a crash does and does not affect:**
- The crashing worker belongs to **BlueStream's own nginx** - either the
  system nginx it owns directly (owned mode) or, whenever any other web
  server/control panel is present, `bluestream-nginx.service`, BlueStream's
  own independent instance (isolated mode; see "Environment detection..."
  above). A worker crash never reaches a different nginx process.
- nginx's master process auto-respawns a crashed worker immediately;
  `Restart=on-failure` (`RestartSec=5`, `StartLimitBurst=10` /
  `StartLimitIntervalSec=300`) covers a full master crash too. A single
  crash is a brief (sub-second to a few seconds) interruption of
  BlueStream's own RTMP/HLS/web-console traffic, not a sustained outage -
  unless the same trigger fires again immediately (see below).
- **A crash in BlueStream's RTMP/HLS engine can never stop CloudPanel, any
  other control panel, or any unrelated website on the same host.** This is
  an architectural guarantee (dedicated process, dedicated systemd unit,
  dedicated config root, dedicated PID/log files - see
  `config/systemd/bluestream-nginx.service` and `lib/nginx.sh`
  `nginx_isolated_*`), proven with real kill signals against real services
  in `tests/integration/crash-isolation.sh` (not a theoretical claim; run
  that script after touching any installer/nginx/systemd code to re-verify
  it).
- Because `worker_processes 1` is required, a crash that repeats on every
  HLS fragment rotation (every `hls_fragment` interval, 5s by default) can
  make a near-continuous respawn loop that leaves **BlueStream's own**
  player/HLS/web-console degraded for as long as that specific stream keeps
  triggering it, even though every other site on the host stays completely
  unaffected the whole time. The GUI's dashboard "Nginx" field
  (`bluestream-manager`, or the web console) reflects this using the
  correct, strategy-aware check (`nginx_status`/`nginx_running` in
  `lib/nginx.sh`) - it checks BlueStream's own instance, never the
  unrelated panel nginx, in isolated mode.

**If this happens on your VPS, gather real evidence before changing
anything** (changing `hls_fragment` without evidence and calling it fixed
hides the real defect):

```bash
# Confirm the exact binaries/versions actually loaded
nginx -V 2>&1
dpkg -s libnginx-mod-rtmp | grep -E 'Version|Status'
ls -l /usr/lib/nginx/modules/ngx_rtmp_module.so

# Find the crash in the system/kernel log (or journalctl -u bluestream-nginx
# / journalctl -u nginx, whichever unit owns it on your install)
journalctl -k --since "-1 hour" | grep -i segfault
journalctl -u bluestream-nginx.service --since "-1 hour" | grep -iE 'signal|segfault|core'

# If a core dump is available (coredumpctl list / ulimit -c), resolve the
# crash offset against the EXACT module file on disk:
addr2line -Cfipe /usr/lib/nginx/modules/ngx_rtmp_module.so <offset>
objdump -dC /usr/lib/nginx/modules/ngx_rtmp_module.so | grep -B20 -A30 '<offset>:'

# Build a controlled reproduction: toggle HLS off entirely, then vary
# hls_fragment, then vary viewer connect/disconnect churn, one variable at
# a time, to see whether the crash interval actually tracks the fragment
# interval or is coincidental.
```

If you can reproduce it reliably, please attach the `addr2line` output and
the exact `nginx -V` / `libnginx-mod-rtmp` version to a bug report - that is
the evidence needed to either patch BlueStream's generated config around
the trigger or document a hard upstream limitation of `ngx_rtmp_module`.

## Health states at a glance

| State | Meaning |
| --- | --- |
| HEALTHY | service active and HLS fresh |
| STARTING | service starting, waiting for HLS |
| STALE | active but HLS not fresh (source problem) |
| STOPPED | stopped and not enabled at boot |
| FAILED | enabled at boot but not running, or unit failed |
| UNKNOWN | not configured / cannot be determined |
