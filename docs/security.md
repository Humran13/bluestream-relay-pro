# Security Model

Security is a first-class concern in BlueStream Relay Pro. This document
describes the model used by version 0.1.0.

## Privileges

- FFmpeg relay processes **never run as root**.
- A dedicated unprivileged system account `bluestream-relay` runs all FFmpeg
  processes.
- The systemd runtime wrapper performs a **root bootstrap** only to read
  root-only configuration and prepare output directories, then permanently
  drops privileges with `setpriv` before exec'ing FFmpeg.
- The privilege drop **fails closed**: if `bluestream-relay` or `setpriv`
  cannot be used, the service exits instead of falling back to root.

## File permissions

| Path | Owner:Group | Mode |
| --- | --- | --- |
| `/etc/bluestream/` | root:root | 0700 |
| `/etc/bluestream/relays/`, `playlists/` | root:root | 0700 |
| individual relay/playlist configs | root:root | 0600 |
| `/etc/bluestream/server.conf` | root:root | 0600 |
| `/var/lib/bluestream/media/` | root:bluestream-relay | 0750 |
| imported media files | root:bluestream-relay | 0640 |
| `/var/lib/bluestream/run/` | bluestream-relay:bluestream-relay | 0750 |
| `/var/lib/bluestream/backups/` | root:root | 0700 |
| `/var/www/bluestream/hls/` | bluestream-relay:www-data | 2750 (setgid) |
| HLS segments (umask 0027) | bluestream-relay:www-data | 0640 |

No `chmod 777` anywhere. Nginx reads HLS through group access.

## Configuration handling

- Relay and playlist configuration files are **never sourced** and never
  evaluated. They are parsed line-by-line with whitelisted keys only.
- All names are validated against `[a-z0-9_-]` (start alphanumeric, max 48
  characters) — this prevents `../` traversal, shell injection and newline
  injection.
- URLs must match supported schemes and are rejected if they contain
  whitespace, control characters or shell-hostile characters.
- FFmpeg commands are built with **Bash arrays**; `eval` is never used.

## Secrets in source URLs

- Source URLs may contain tokens, passwords or signed query strings.
- Relay configs are root-only (0600).
- The manager redacts credentials in all listing/status output.
- Generated M3U8 files never contain the source URL.
- Logs avoid printing full source URLs.

### `/proc/<pid>/cmdline` visibility

The FFmpeg command line (including the source URL) is visible to **root** via
`/proc/<pid>/cmdline`, and to the same user (`bluestream-relay`) for its own
processes. This is standard Linux behaviour. BlueStream does **not** change
global `/proc` settings such as `hidepid` automatically — that is an
operating-system-level decision for the administrator.

## systemd hardening

Relay and playlist units apply:

- `NoNewPrivileges=true`
- `ProtectSystem=strict`
- `ProtectHome=true`
- `PrivateTmp=true`
- `PrivateDevices=true`
- `ProtectKernelTunables=true`
- `ProtectKernelModules=true`
- `ProtectControlGroups=true`
- `LockPersonality=true`
- `RestrictSUIDSGID=true`
- `RestrictRealtime=true`
- Narrow `ReadWritePaths` (HLS output and the playlist run directory)

These directives are compatible with systemd >= 231 (all supported Ubuntu
releases).

## Network and services

- Only ports 80 and 443 (plus SSH) are exposed when UFW is enabled.
- Source URLs are contacted outbound only; the server never listens for
  inbound media.
- Nginx serves HLS with `X-Content-Type-Options: nosniff` and CORS
  restricted to simple GET/HEAD/OPTIONS.

## Administrator responsibility

BlueStream does not implement DRM bypass, website scraping or authentication
bypass. The administrator is responsible for holding the rights to relay
any content.

## Backup safety

Backups are created as root-only tarballs. Restore refuses archives that
contain paths outside the expected BlueStream set, and re-applies
root-only permissions after extraction.
