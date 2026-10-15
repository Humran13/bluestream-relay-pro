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
| `/etc/bluestream/relays/`, `playlists/`, `destinations/` | root:root | 0700 |
| individual relay/playlist/destination configs | root:root | 0600 |
| `/etc/bluestream/server.conf` | root:root | 0600 |
| `/var/lib/bluestream/media/` | root:bluestream-relay | 0750 |
| imported media files | root:bluestream-relay | 0640 |
| `/var/lib/bluestream/run/` | bluestream-relay:bluestream-relay | 0750 |
| `/var/lib/bluestream/playlist-cache/` | root:bluestream-relay | 0750 |
| playlist-cache artifacts | root:bluestream-relay | 0640 |
| `/var/lib/bluestream/backups/` | root:root | 0700 |
| `/var/www/bluestream/hls/` | www-data:www-data | 0750 |
| HLS segments (created by nginx-rtmp) | www-data:www-data | 0644 |

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

## Outbound publisher (Phase 1C) stream-key handling

A destination's stream key is written **only** to its root-only config file
(`/etc/bluestream/destinations/<id>.conf`, root:root 0600) and is delivered
through the privileged bridge **exclusively over stdin** — never as a CLI
argument, environment variable, or URL. It is never present in:

- the HTML, an API response, or any listing/status returned to the browser
  (only a `has_stream_key` boolean and, for a running publisher, a derived
  state like `LIVE`/`FAILED` — never the key or the destination server URL);
- application logs, flash messages, or exception text;
- a diagnostic dump — `publisher_dump_ffmpeg_args` (the only place the
  publisher's FFmpeg argv is ever printed) explicitly redacts the entire
  destination URL rather than relying on field-level redaction, because the
  key lives in the URL's **path**, not a query string `bs_redact_url` could
  mask.

**Where it unavoidably exists in plaintext:** FFmpeg must receive the
complete `rtmp(s)://server/.../<key>` URL as one of its own process
arguments to publish to your destination. For the lifetime of that one
process, the key is therefore visible:

- to **root** via `/proc/<pid>/cmdline` (root already holds the plaintext key
  in the destination config, so this grants nothing new);
- to **other processes running as the same `bluestream-relay` account** —
  i.e. the source relay/playlist's own FFmpeg process and any sibling
  publisher process for a *different* destination — because Linux process
  visibility (`/proc/<pid>/cmdline`, ptrace) is governed primarily by UID
  match, and `ProtectProc=invisible` (set on every relay/playlist/publisher
  unit) hides process details from *other* users, not from another process
  running under the *same* UID.

**What this does NOT expose:** `bluestream-relay` cannot read a destination's
stream key from disk — destination configs stay root:root 0600 with no group
access for that account. The exposure above is live-process-only and
requires the `bluestream-relay` account itself to already be running
attacker-controlled code (at which point there are more direct problems);
it is never reachable from the web user, from the network, or from a
different destination's own publisher logic.

**Mitigations applied:** the account runs *only* BlueStream's own fixed
`ffmpeg` invocations (no shell, no login, `NoNewPrivileges`), each publisher
unit gets the full relay/playlist hardening profile plus `ProtectProc=invisible`
and `ProcSubset=pid`, and a publisher unit is granted **zero** `ReadWritePaths`
(it never touches a local file at all). Full per-destination process isolation
(a unique UID per destination via a provisioned system account, so no two
FFmpeg processes under BlueStream ever share a UID) would close this
residual gap entirely; it is a larger architectural change than this phase's
scope and is **not implemented yet** — this is stated here honestly rather
than silently left out.

## systemd hardening

Relay, playlist and outbound publisher units apply:

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
- Narrow `ReadWritePaths` (HLS output and, for playlists, the run directory
  and the normalization cache; a publisher unit gets **none at all** - it
  never touches a local file)
- `ProtectProc=invisible` and `ProcSubset=pid` on relay/playlist/publisher
  units (systemd >= 247 / Ubuntu 22.04+; silently ignored on older systemd)

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
