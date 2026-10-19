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
  Nginx present   : yes (active: yes, other sites: yes)
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
using. The decision is **re-evaluated fresh every run** (including
reinstalls/repairs/upgrades) and persisted (`server.conf`
`NGINX_STRATEGY=`) — but a previously recorded strategy is never trusted
blindly:

- If the environment detected **right now** positively requires isolated
  mode (a control panel, another site, an inactive/ambiguous nginx, etc.),
  BlueStream uses isolated **even if a prior install was recorded as
  "owned"**. This is a deliberate safety fix: a stale "owned" record can
  never bypass today's check. `install.sh` prints a loud warning
  (`SAFETY OVERRIDE`) when this happens and, if BlueStream's owned-mode
  config is still live on the system nginx, stops before touching anything
  further and points you at the guided migration (see below) instead of
  guessing.
- The opposite direction never happens silently: once an install has
  legitimately switched to isolated, it **stays isolated** on every later
  run even if the environment looks clear again that day (e.g. a panel was
  briefly misdetected, or later removed). Moving back to owned mode is a
  deliberate, reviewed action, not an automatic reclassification.

**Critical safety rule:** an existing nginx is **never** assumed to be safe
to manage just because no *recognized* control panel was matched. Ownership
must be positively confirmed — nginx present, active, **and** no site or
`conf.d` entry already configured on it that BlueStream didn't create
itself. The absence of a known panel marker is not evidence that nothing
else is managing it; an unrecognized/unsupported panel, or a hand-configured
site, is still detected via this positive check and still gets the isolated
strategy. Only a genuinely empty, freshly-installed nginx (or one BlueStream
already owns from a previous install) is ever managed directly.

**Owned** (fresh VPS, or an existing nginx confirmed to be active with
nothing else configured on it): BlueStream manages `/etc/nginx` directly —
same as previous versions. Your existing site files, SSL certificates and
global settings are left alone except for two narrow, reversible changes
needed specifically for the RTMP engine to work correctly:
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
Virtualmin/Webmin, Nginx Proxy Manager, Apache, Caddy, an nginx that already
has another site configured on it — recognized panel or not, an nginx that
exists but isn't running, or anything else ambiguous): BlueStream **never
writes to `/etc/nginx`**, never changes `worker_processes` or any other
global setting, never touches `sites-enabled/default`, and never runs
`certbot --nginx` against the system config. Instead it installs and runs
its **own** independent nginx instance:

| | Owned | Isolated |
| --- | --- | --- |
| Config root | `/etc/nginx/` | `/etc/bluestream/nginx/` |
| systemd unit | `nginx.service` (system) | `bluestream-nginx.service` |
| HTTP port | 80 | loopback-only; first free port from `8088 8188 8288 8388 18080` |
| HTTPS | 443 | no isolated listener; the hosting panel terminates public HTTPS |
| `--with-ssl` | certbot `--nginx`, fully automatic | **not supported automatically** — see below |

Everything else is identical: the same RTMP bus, the same HLS/player/console
location logic, the same web console. `bluestream-manager`'s Server Status
and the installer's own output always show the ports actually in use.

**The isolated instance is a full streaming GATEWAY, not just a console
proxy.** It is the one process that fronts Gunicorn for `/console/` *and*
serves `/player/` and `/hls/` itself directly (via nginx `alias`, exactly as
the owned-mode nginx does on port 80/443) — Gunicorn/Flask has no routes for
`/player/` or `/hls/` at all. **Always point your reverse proxy at the
gateway's port, never at the bare Gunicorn port** — pointing a panel
straight at Gunicorn is a real misconfiguration that was hit on a live VPS:
`/console/` worked (a 302 redirect) while `/player/?relay=<name>` and
`/hls/relay/<name>/index.m3u8` both 404'd, because Gunicorn simply has
nothing to serve those paths with. `sudo bluestream-manager installinfo`
always names the correct port as "Reverse-proxy destination" and separately
labels the Gunicorn port as console-only.

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

Ports 80 and 443 are always reserved for the hosting panel in isolated mode,
even if the panel nginx is temporarily stopped while BlueStream is installed.
The selected gateway port is persisted and reused; a later collision fails
loudly without silently changing the panel's configured proxy destination.

`--with-ssl` is refused (with a clear message, not silently ignored) for an
isolated install, because the only automatic path (`certbot --nginx`) would
mean editing the panel's own nginx configuration — exactly what this
installer promises never to do. Issue and attach the public certificate in
the panel; BlueStream's gateway remains plain HTTP on loopback.

**An existing nginx that is not managed by apt/dpkg** (some panels ship
their own build, sometimes at the very same `/usr/sbin/nginx` path the
Ubuntu package also uses): the installer detects this and **aborts before
installing anything**, rather than risk `apt-get install nginx` silently
overwriting those files. Resolve it manually (remove/relocate the
conflicting binary, or accept that this host can't run BlueStream's nginx-
based streaming engine) and re-run.

### Safe Owned -> Isolated migration

If a host was installed in **owned** mode (BlueStream's config lives in
`/etc/nginx`) and later has a control panel installed on it (e.g.
CloudPanel), re-running `install.sh` will detect the override above and
**stop before changing anything** rather than attempt an in-place switch —
doing so inside a plain install run cannot be proven safe, because the
legacy owned config is still bound to the same ports (80 and the private
RTMP bus on 1935) the new isolated instance also needs.

The installed old manager may not have a `migrate` command. Therefore the
safe production order is: fetch and verify the **new** source, run its
manager directly to migrate, and only then run its installer. Replace
`<approved-commit>` with the reviewed PR head supplied for the release:

```bash
git clone https://github.com/Humran13/bluestream-relay-pro.git
cd bluestream-relay-pro
git fetch origin fix-installer-admin-ports-legacy-migration
git switch --detach <approved-commit>
test "$(git rev-parse HEAD)" = "<approved-commit>"
git status --short                 # must print nothing

# Independent pre-upgrade backup; verify that the archive is readable.
backup="/root/bluestream-preupgrade-$(date +%Y%m%d-%H%M%S).tar.gz"
sudo tar --ignore-failed-read -C / -czpf "$backup" \
  etc/bluestream var/lib/bluestream etc/nginx
sudo test -s "$backup"
sudo tar -tzf "$backup" >/dev/null

sudo bash ./bluestream-manager migrate status
sudo bash ./bluestream-manager migrate run
```

Do not run the ordinary installer before the migration as the planned
sequence. If you do, its initial read-only environment check safely stops
before package installation, file installation, or nginx changes and prints
the same new-checkout commands; it never attempts the cutover implicitly.

What `migrate run` does, in order, stopping and rolling back automatically
if any step fails:

1. **Verifies by content**, not filename, that `/etc/nginx/sites-available/
   bluestream` and/or the RTMP main-context include are genuinely
   BlueStream's own generated files (every one carries a
   `# BlueStream Relay Pro` header comment) — never assumes ownership from
   a path alone.
2. **Backs up** the exact site/RTMP state (including an already-disabled or
   missing RTMP include) to `/var/lib/bluestream/backups/nginx-migration-
   <timestamp>-<pid>/`, writes a checksum manifest, and verifies every
   required entry before touching anything. `cp -a` preserves ownership and
   permissions.
3. **Generates and validates** (`nginx -t`) BlueStream's own isolated
   instance, without starting it yet.
4. **Disables** (never deletes) BlueStream's own two files on the system
   nginx, validates the system config with `nginx -t`, and only then
   reloads it — this is the only part of the process that touches the
   panel-managed nginx, and it only ever removes files BlueStream itself
   created and verified in step 1. No other site, panel config, or firewall
   rule is ever touched.
5. Rechecks the persisted loopback HTTP port and RTMP port immediately before
   startup, then **starts and verifies** BlueStream's isolated instance.
6. Records `NGINX_STRATEGY=isolated` in `server.conf` only after every
   prior step succeeded.

Any failure at any step returns control to the migration transaction, stops
and disables the isolated unit, restores the manifest-recorded pre-migration
state, validates/reloads system nginx, and verifies the former RTMP listener
is accepting connections when the manifest says it existed. If recovery
cannot be verified, rollback fails explicitly and preserves all copies with
manual recovery guidance; it never deletes a disabled RTMP configuration
unless a checksum-verified active replacement has already been restored.
You can also
roll back a completed migration by hand:

```bash
cd /path/to/new/bluestream-relay-pro
sudo bash ./bluestream-manager migrate rollback \
  /var/lib/bluestream/backups/nginx-migration-<timestamp>-<pid>
```

Use the same new checkout for rollback until the ordinary installer has
successfully installed the new manager. Rollback restores only the two
content-verified BlueStream-owned nginx files. It never restores a global
`/etc/nginx/nginx.conf` or a panel site file over newer CloudPanel changes.
Running the same rollback again is safe. A backup with a missing manifest,
missing required entry, checksum mismatch, or unexpected file at a managed
path is refused before services are stopped.

After migration succeeds, validate the gateway before upgrading the installed
manager (substitute the exact port printed by `migrate run`):

```bash
curl -fsS http://127.0.0.1:<gateway-port>/player/ >/dev/null
curl -fsS http://127.0.0.1:<gateway-port>/console/ >/dev/null
sudo ss -ltnp | grep -E '127\.0\.0\.1:(<gateway-port>|1935)'
sudo nginx -t
sudo systemctl is-active nginx bluestream-nginx.service

# Install the reviewed version only after those checks pass.
sudo bash ./install.sh
sudo bluestream-manager installinfo
sudo bluestream-manager diagnostics
sudo bluestream-manager selftest
```

In CloudPanel, configure the public site's reverse proxy to the exact
`http://127.0.0.1:<gateway-port>` shown as **Reverse-proxy destination** by
`installinfo`; do not use the Gunicorn port. Let CloudPanel terminate HTTPS.
Then test the public `/console/`, `/player/`, and a live `/hls/.../index.m3u8`
plus at least one changing `.ts` segment. A real relay/playlist publish is the
functional RTMP check; an HTTP 200 or `nginx -t` alone is not sufficient.

`migrate status` also prints the safe recovery commands. Do not improvise a
manual cutover: missing and already-disabled RTMP states are intentionally
different, and only the manifest-backed transaction knows which one to
restore.

Your data — relays, playlists, destinations, schedules, secrets, and the
web admin account — is never touched by this migration; it lives entirely
under `/etc/bluestream` and `/var/lib/bluestream`, independent of which
nginx strategy is active.

## 3. First steps

```bash
sudo bluestream-manager
```

- Relay Management > **Add Relay** — add your first source.
- Media Management > **Import Media** — for playlist channels.
- Server Management > **Diagnostics** and **Self-Test** — verify everything.

### Administrator account

`install.sh` never finishes without being clear about whether the web
console dashboard is actually usable yet:

- If a real terminal is reachable (a normal interactive run, **or** a piped
  `curl ... | sudo bash` install where your terminal's `/dev/tty` is still
  reachable even though stdin is the pipe), you are prompted for a username
  (default `admin`) and a password + confirmation, both hidden/no-echo.
- If no terminal is reachable at all (pure non-interactive: CI, cron, a
  fully detached pipe), every other install stage still completes, but the
  output clearly states that **admin onboarding is pending** and the
  dashboard cannot be logged into yet — along with the exact command to
  finish it:
  ```bash
  sudo bluestream-manager admin init
  ```
  (or, before `bluestream-manager` itself is installed, the equivalent
  direct `python3 -m webapp.security init-admin` command via stdin, also
  printed in the warning.)
- A reinstall/upgrade **never** resets, overwrites, or duplicates an
  existing admin account — `bluestream-manager admin init` itself refuses
  outright if one already exists.
- The password is never accepted as a command-line argument, never logged,
  and never left in shell history — it is read via a hidden prompt (or
  piped over stdin for the non-interactive command) and hashed with the
  existing `scrypt`-based implementation already in `webapp/security.py`
  (no new authentication system or dependency was introduced).

**Locked out?** Use the dedicated, root-gated password-reset workflow
instead of deleting `admin.json` by hand:

```bash
sudo bluestream-manager admin reset-password
```

It does **not** require the old password (that is the point of a lockout
recovery), but it does require local root/sudo access to invoke, and it
asks for explicit confirmation before changing anything (the current
password stops working immediately). It preserves the existing username.
The piped/non-interactive equivalent (new password via stdin, never argv):

```bash
printf '%s\n' '<new-password>' | (cd /usr/local/lib/bluestream && \
  python3 -m webapp.security reset-admin-password --state-dir /var/lib/bluestream/web)
```

Check status at any time: `sudo bluestream-manager admin status` or
`sudo bluestream-manager installinfo`.

### Relay reliability (reconnection behavior)

Every relay's FFmpeg process is run inside a supervision loop
(`lib/supervise.sh`), not as a single one-shot process, specifically
because systemd's `Restart=on-failure` never restarts a unit whose main
process exits with code `0` — and FFmpeg exits `0` both on a deliberate
stop **and** on a plain live-source disconnect/EOF with no error. Without
this, a live relay that hit a transient source hiccup could silently stop
producing HLS and never come back, indistinguishable at the systemd level
from "the operator stopped it on purpose" (this was observed on a real
VPS: `Result=success`, `ExecMainStatus=0`, `NRestarts=0`, the relay simply
went quiet about 15 seconds after starting).

The supervisor now distinguishes, and reports, exactly why a relay most
recently stopped:

| Situation | Behavior |
| --- | --- |
| Deliberate stop (`relay stop` / `systemctl stop`) | Recognized via the stop **signal itself** (never inferred from FFmpeg's exit code) — exits cleanly, reported as `STOPPED`, never retried. |
| Finite media, no loop configured | A clean end is the **expected** natural completion — reported as `STOPPED` ("finite media playback completed normally"), never retried. |
| Finite media **with** looping configured | FFmpeg loops internally; if it ever exits anyway, that's unexpected and is retried like a live source. |
| Any live/remote source (`remote-hls`, `rtmp(s)`, `rtsp`, `http-file`, `youtube`, `web-resolver`) ending unexpectedly — **including a clean exit 0** | Reconnected automatically with bounded exponential backoff (reported as `RECONNECTING`), using the relay's own configured restart-delay as the base. A sustained healthy run resets the backoff; after 8 unsuccessful attempts it gives up and reports `FAILED` with the reason, rather than retrying forever. |

`sudo bluestream-manager relay show <name>` and the dashboard both surface
the current supervisor state, the last FFmpeg exit code, the reconnect
attempt count, and the actual FFmpeg PID (distinct from the systemd unit's
own `MainPID`, which is the supervisor loop itself) — never just "the
service is active".

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
| A relay went quiet / stopped and you didn't stop it | Run `sudo bluestream-manager relay show <name>` — the "Supervisor state"/"Last reason" lines say exactly why (a deliberate stop, a normal finite-media completion, an active `RECONNECTING` backoff, or a `FAILED` after exhausting reconnection attempts). See "Relay reliability (reconnection behavior)" above. |
| Isolated instance: public URL unreachable | Confirm your panel's reverse-proxy rule points at the correct `127.0.0.1:<isolated-http-port>` (see Server Status / the install output) and that the panel itself is reachable. |
| `nginx RTMP module loaded: FAIL` | `libnginx-mod-rtmp` failed to install, or (isolated mode) the module `.so` wasn't found at the expected path — check `apt list --installed libnginx-mod-rtmp` and re-run `install.sh`. |
| **502 Bad Gateway** from your control panel/reverse proxy | The reverse proxy is almost always pointed at the wrong port. Run `sudo bluestream-manager installinfo` and confirm your panel's vhost targets the exact "Reverse-proxy destination" value shown — in isolated mode this is the **gateway** port (BlueStream's own nginx, e.g. `127.0.0.1:8088`), **never** the raw Gunicorn port (e.g. `127.0.0.1:8180`) and never 80/443/a guessed port. The install output's `PORT CONFIGURATION` section states this explicitly for the same reason. |
| `/console/` works (redirects fine) but `/player/?relay=<name>` and `/hls/relay/<name>/index.m3u8` both 404 through your panel's HTTPS domain | The reverse proxy is pointed at the **Gunicorn** port instead of the gateway port. Gunicorn/Flask only implements `/console/*` routes — it has no `/player/` or `/hls/` routes at all, so proxying straight to it makes `/console/` work while everything else 404s. Fix: point the panel's reverse proxy at the isolated gateway port shown by `sudo bluestream-manager installinfo` ("Isolated streaming gateway" / "Reverse-proxy destination"), not at "Internal Gunicorn port". |
| "No web admin account exists" / cannot log in after install | Admin onboarding was pending (no terminal was reachable during a non-interactive install). Run `sudo bluestream-manager admin init`, or check status with `sudo bluestream-manager admin status`. |
| Installed as "owned" but a panel (e.g. CloudPanel) now exists | `install.sh` stops and prints a migration procedure instead of silently reusing the old strategy. The installed old manager may not support it: from the verified new checkout run `sudo bash ./bluestream-manager migrate status`; see "Safe Owned -> Isolated migration" above. |
