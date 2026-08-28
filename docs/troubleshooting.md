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
  `bluestream-relay:www-data` with mode 2750 (setgid).

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

## Health states at a glance

| State | Meaning |
| --- | --- |
| HEALTHY | service active and HLS fresh |
| STARTING | service starting, waiting for HLS |
| STALE | active but HLS not fresh (source problem) |
| STOPPED | stopped and not enabled at boot |
| FAILED | enabled at boot but not running, or unit failed |
| UNKNOWN | not configured / cannot be determined |
