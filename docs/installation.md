# Installation

BlueStream Relay Pro targets **Ubuntu Server** VPS systems managed with
systemd. The foundation release (0.1.0) has been designed and validated for
Ubuntu 20.04, 22.04, 24.04 and 26.04. Ubuntu 18.04 may work but is not
officially supported by 0.1.0. **No version has been real-VPS tested yet.**

## Requirements

- A fresh Ubuntu Server VPS with root access (SSH).
- At least 1 vCPU / 1 GB RAM for basic stream-copy relays.
- Outbound network access to your media sources.
- A domain name pointed at the server (recommended; HTTPS works best).
- Ports 80 and 443 reachable from the internet.

## 1. Get the project onto the server

**One-line installer (recommended):**

```bash
curl -fsSL https://raw.githubusercontent.com/Humran13/bluestream-relay-pro/main/bootstrap.sh | sudo bash
```

This clones the public repository to `/usr/local/src/bluestream-relay-pro`
(never your BlueStream runtime data) and runs `install.sh` for you. Pass
installer flags after `--`, e.g. `... | sudo bash -s -- --domain
video.example.com --assume-yes`.

**Manual upload (if you prefer):**

```bash
scp -r bluestream-relay-pro root@SERVER:/opt/
```

## 2. Run the installer

```bash
cd /opt/bluestream-relay-pro
sudo bash install.sh
```

(Skip this step if you used the one-line bootstrap installer above — it
already ran `install.sh` for you.)

The installer will:

1. verify root privileges
2. detect the Ubuntu version
3. update package metadata
4. install nginx, ffmpeg/ffprobe, curl and required tools
5. install certbot when SSL is requested
6. configure UFW when requested
7. create the BlueStream directories and the dedicated `bluestream-relay`
   system user
8. install the manager, systemd templates, nginx configuration and the
   bundled web player
9. validate nginx before activating it
10. print post-install information

### Non-interactive install

```bash
sudo bash install.sh \
  --domain video.example.com \
  --email admin@example.com \
  --with-ssl \
  --with-ufw \
  --assume-yes
```

Available flags:

| Flag | Meaning |
| --- | --- |
| `--domain <name>` | Public domain for HLS URLs |
| `--email <addr>` | Administrator email (required with `--with-ssl`) |
| `--with-ssl` | Install certbot and request a Let's Encrypt certificate |
| `--no-ssl` | Skip SSL configuration |
| `--with-ufw` | Enable and configure the UFW firewall |
| `--no-ufw` | Skip firewall configuration |
| `--no-apt-update` | Skip `apt-get update` |
| `--assume-yes` | Answer yes to prompts, use safe defaults |
| `--non-interactive` | No prompts; use flag values or defaults |
| `--help` | Show help |

### Idempotency

Re-running `install.sh` repairs and updates the BlueStream installation. It
**does not** delete existing relays, playlists, media or the server
configuration. The existing `server.conf` values are preserved unless you
override them with flags. This is also how you **repair** a partially-broken
install (fix a stopped service, re-render nginx, reapply permissions) and how
you **upgrade** to a newer checkout: pull/update the source directory (or
re-run `bootstrap.sh`, which does this for you) and run `install.sh` again.
There is no separate "upgrade" command.

### Environment detection and existing web server / control panel compatibility

Before installing or changing anything, `install.sh` inspects the host and
prints what it found:

```text
==> Detected environment
  Classification  : Managed environment (cloudpanel detected)
  Nginx present   : yes (active: yes)
  Apache present  : no
  Caddy present   : no
  Control panel   : cloudpanel
  Port 80 free    : no
  Port 443 free   : no
  Existing install: no
[WARN] An existing web server/control panel was detected (or the environment is ambiguous).
[WARN] BlueStream will NOT modify it. A separate, independent nginx instance will be installed
[WARN] for BlueStream's own streaming engine and web console instead - see docs/installation.md
[WARN] 'Existing Nginx / control panel compatibility' for the manual reverse-proxy step.
```

Two strategies exist, and the installer always announces which one it is
using. **This decision is made once and persisted** (`server.conf`
`NGINX_STRATEGY=`); re-running `install.sh` later never silently switches
strategies even if the live environment looks different that day.

**Owned** (fresh VPS, or a plain/unmanaged nginx already installed and
active): BlueStream manages `/etc/nginx` directly — same as previous
versions. Your existing site files, SSL certificates and global settings are
left alone except for two narrow, reversible changes needed specifically for
the RTMP engine to work correctly:
  - `worker_processes` is pinned to `1` (nginx-rtmp-module keeps live
    publish/play state **per worker process**; with more than one worker, a
    publisher's connection and the source's connection can land on different
    workers and never link up — this is a documented nginx-rtmp-module
    constraint, not a BlueStream choice). This only affects nginx's own HTTP
    serving concurrency; if this host also runs other independent,
    high-traffic nginx sites, weigh that trade-off before installing
    BlueStream alongside them.
  - `sites-enabled/default` (the stock "Welcome to nginx" catch-all) is
    unlinked so a bare-IP or wrong-Host request doesn't silently answer with
    it instead of your BlueStream site. `sites-available/default` itself is
    never touched, so this is one command to reverse: `ln -s
    /etc/nginx/sites-available/default /etc/nginx/sites-enabled/`.

**Isolated** (CloudPanel, aaPanel, HestiaCP, Plesk, ISPConfig,
Virtualmin/Webmin, Nginx Proxy Manager, Apache, Caddy, or anything ambiguous
— nginx present but not running, for example): BlueStream **never writes to
`/etc/nginx`**, never changes `worker_processes` or any other global
setting, never touches `sites-enabled/default`, and never runs
`certbot --nginx` against the system config. Instead it installs and runs
its **own** independent nginx instance:

| | Owned | Isolated |
| --- | --- | --- |
| Config root | `/etc/nginx/` | `/etc/bluestream/nginx/` |
| systemd unit | `nginx.service` (system) | `bluestream-nginx.service` |
| HTTP port | 80 | first free port from `80 8088 8188 8288 8388 18080` |
| HTTPS port | 443 | first free port from `443 8443 8543 8643 18443` (reserved; see below) |
| `--with-ssl` | certbot `--nginx`, fully automatic | **not supported automatically** — see below |

Everything else is identical: the same RTMP bus, the same HLS/player/console
location logic, the same web console. `bluestream-manager`'s Server Status
and the installer's own output always show the ports actually in use.

**Connecting your domain to an isolated install:** add one reverse-proxy
rule in your panel pointing a (sub)domain at
`http://127.0.0.1:<isolated-http-port>/` (the exact port is printed during
install and in Server Status) and let the panel terminate HTTPS as it
normally would for any other site it manages. For example, in a panel that
lets you add a custom nginx/Apache vhost:

```nginx
# Example reverse-proxy vhost your panel manages (illustrative - the exact
# UI/file differs per panel). BlueStream's own config is never edited here.
server {
    listen 443 ssl;
    server_name stream.example.com;
    # ... your panel's own certificate directives ...
    location / {
        proxy_pass http://127.0.0.1:8088;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

`--with-ssl` is refused (with a clear message, not silently ignored) for an
isolated install, because the only automatic path (`certbot --nginx`) would
mean editing the panel's own nginx configuration — exactly what this
installer promises never to do. If you want certbot to manage a certificate
for BlueStream's own instance directly (bypassing the panel entirely), point
a dedicated subdomain's DNS straight at this server and run certbot
yourself with `--webroot` or `--standalone` against
`/etc/bluestream/nginx/nginx.conf`'s HTTP port once it is reachable from the
internet on port 80.

**An existing nginx that is not managed by apt/dpkg** (some panels ship
their own build, sometimes at the very same `/usr/sbin/nginx` path the
Ubuntu package also uses): the installer detects this and **aborts before
installing anything**, rather than risk `apt-get install nginx` silently
overwriting those files. Resolve it manually (remove/relocate the
conflicting binary, or accept that this host can't run BlueStream's nginx-
based streaming engine) and re-run.

## 3. First steps

```bash
sudo bluestream-manager
```

- Relay Management > **Add Relay** — add your first source.
- Media Management > **Import Media** — for playlist channels.
- Server Management > **Diagnostics** and **Self-Test** — verify everything.

## 4. DNS and firewall

Point `video.example.com` at the server's public IP before requesting SSL.

`--with-ufw` only ever **adds** rules; it never removes or overwrites an
existing UFW policy or rule you already have. For an **owned** install,
confirm these rules exist:

```text
22/tcp   (SSH)
80/tcp   (HTTP)
443/tcp  (HTTPS)
```

For an **isolated** install, BlueStream's own ports (shown during install)
are **not** opened by `--with-ufw` and do not need to be — they are reached
only via the reverse-proxy rule you add in your panel (loopback-to-loopback
traffic on this same host), never directly from the internet. Only your
panel's own existing ports need to stay open, and `--with-ufw` never touches
those either.

The internal web console port (Gunicorn) and the RTMP bus port (1935) are
**never** opened by `--with-ufw` or any other BlueStream code — both are
always loopback-only (`127.0.0.1`) regardless of strategy, by design.

## 5. SSL (Let's Encrypt)

The installer requests a certificate when `--with-ssl` is used. You can also
run it later from the manager: **Server Management > SSL Management**.

Renewals are handled by certbot's systemd timers automatically.

## Post-install layout (installed system)

| Path | Purpose |
| --- | --- |
| `/etc/bluestream/` | Root-only configuration (relays, playlists, server.conf) |
| `/var/lib/bluestream/media/` | Managed media files |
| `/var/lib/bluestream/run/` | Runtime concat files and preparation markers |
| `/var/lib/bluestream/playlist-cache/` | Normalized playlist artifacts (content-addressed cache) |
| `/var/lib/bluestream/backups/` | Configuration backups |
| `/var/www/bluestream/hls/` | HLS output (relay/ and playlist/) |
| `/var/www/bluestream/web/` | Bundled hls.js player |
| `/usr/local/sbin/bluestream-manager` | Manager command |
| `/usr/local/bin/bluestream-status` | Quick status command |
| `/usr/local/lib/bluestream/` | Installed scripts and docs |
| `/etc/systemd/system/bluestream-relay@.service` | Relay unit template |
| `/etc/systemd/system/bluestream-playlist@.service` | Playlist unit template |
| `/etc/systemd/system/bluestream-publisher@.service` | Outbound publisher unit template |
| `/etc/bluestream/nginx/` | Isolated-strategy nginx instance (only present if used) |
| `/etc/systemd/system/bluestream-nginx.service` | Isolated nginx instance unit (only present if used) |

## Repair

Re-running `install.sh` (or, with no arguments, the one-line bootstrap
installer again) **is** the repair mechanism — it is fully idempotent. It
re-applies permissions, re-renders nginx/systemd configuration from the
current `server.conf`, restarts the web console if it was already running,
and never deletes a relay, playlist, destination, schedule, or media file.
Use `sudo bluestream-manager` → **Server Management → Diagnostics** first to
see exactly what is broken before repairing.

## Upgrade

Update the source checkout (re-run `bootstrap.sh`, or `git pull` your own
clone) and run `install.sh` again. There is no separate upgrade command —
the same idempotent install path handles it. `server.conf` (domain, ports,
nginx strategy, SSL state) and all your relays/playlists/destinations/media
are preserved.

## Rollback

There is no automated version-rollback command in 0.1.0. To roll back:

1. Check out the previous version of the source (`git checkout <tag>` or
   restore a previous `bluestream-relay-pro` directory).
2. Run `install.sh` from that checkout. It re-renders every BlueStream-owned
   file (systemd units, nginx config, web console) to match that version,
   while still preserving `server.conf` and all your data.
3. If the newer version added new `server.conf` keys your older version
   doesn't understand, they are simply ignored by the older code — nothing
   is deleted.

Configuration **backups** (`sudo bluestream-manager` → Backup/Restore, or
`uninstall.sh`'s automatic pre-uninstall backup) cover `/etc/bluestream`
only (relay/playlist/destination/server configuration) and are the
recommended safety net before any risky change — media is excluded by
default (it can be large; back it up separately if needed).

## Uninstallation

```bash
sudo bash uninstall.sh            # keep all data (relays, media, HLS, etc.)
sudo bash uninstall.sh --purge    # also remove /etc/bluestream, /var/lib/bluestream, /var/www/bluestream
```

- A configuration backup is created automatically first (unless
  `--no-backup`).
- You are asked to confirm twice (once y/N, once by typing the project name).
- The system nginx package, service and configuration are **never** removed
  or modified by the uninstaller, in either strategy — only BlueStream's own
  units (`bluestream-relay@*`, `bluestream-playlist@*`, `bluestream-
  publisher@*`, `bluestream-nginx.service` if present), its web console, and
  (with `--purge`) its own data directories are removed. System packages
  (nginx, ffmpeg, certbot, libnginx-mod-rtmp) are never removed either —
  remove them yourself afterward if you no longer need them.

## Troubleshooting

```bash
sudo bluestream-manager diagnostics   # full health checklist, strategy-aware
sudo bluestream-manager selftest      # end-to-end functional test
journalctl -u bluestream-relay@<name> --no-pager -n 100
journalctl -u 'bluestream-publisher@relay:<name>:<destination-id>' --no-pager -n 100
journalctl -u bluestream-nginx.service --no-pager -n 100   # isolated strategy only
```

Common issues:

| Symptom | Likely cause / fix |
| --- | --- |
| `WEB_PORT unset` or similar during install | Should not happen as of this fix — if you see it, `sudo bluestream-manager diagnostics` and check `/etc/bluestream/server.conf` for a missing/invalid `WEB_PORT`; re-run `install.sh`. |
| Install hangs or silently skips a prompt when piped via `curl \| sudo bash` | Fixed — `install.sh` now falls back to `/dev/tty` for interactive prompts and never blocks forever; a domain left blank is fine (see [Domain input](../README.md#domain-input)). |
| A destination never reaches "Live" | Check `journalctl` for that publisher's unit (above) — the detail field gives a bounded, specific reason (e.g. "destination refused the connection"), never the stream key. |
| Isolated instance: public URL unreachable | Confirm your panel's reverse-proxy rule points at the correct `127.0.0.1:<isolated-http-port>` (see Server Status / the install output) and that the panel itself is reachable. |
| `nginx RTMP module loaded: FAIL` | `libnginx-mod-rtmp` failed to install, or (isolated mode) the module `.so` wasn't found at the expected path — check `apt list --installed libnginx-mod-rtmp` and re-run `install.sh`. |
