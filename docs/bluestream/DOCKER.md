# BlueStream Relay Pro + Studio - Docker deployment

One Compose project = the **whole** product (Relay Pro console, nginx-rtmp, FFmpeg, publisher, playlists, **Studio as a tab in the same
dashboard, same login**). Everything lives inside one image and a few dedicated volumes. It never uses the host's nginx, apt, systemd,
firewall, SSL certificates, CloudPanel or the Docker socket, and never touches other containers.

**Status (honest):** built and exercised in Docker Desktop (Windows 11/WSL2) only. It has **not** been installed on a real VPS and **no real
YouTube livestream** has been run through it. Do the first real test with a private/unlisted YouTube broadcast and a key that no other stream uses.

## What it creates (and nothing else)

| Resource | Name (project `bluestream` by default) |
|---|---|
| container | `bluestream-app` (non-privileged, read-only root fs, all capabilities dropped except CHOWN DAC_OVERRIDE FOWNER SETUID SETGID KILL NET_BIND_SERVICE AUDIT_WRITE, 2 CPU / 2 GB / 512 pids by default, log rotation) |
| network | `bluestream_net` (private bridge) |
| volumes | `bluestream_etc` (config + stream keys), `bluestream_units`, `bluestream_data` (media, Studio projects, state), `bluestream_www` (HLS) |
| image | `bluestream-relay-pro:local` (built on your server from this repository) |
| published port | **only** `127.0.0.1:<18090-18099>:80` (dashboard + player). The RTMP bus is not published. |
| files on the host | `~/.bluestream-docker/<project>.env` (no secrets) |

Never done: host nginx/CloudPanel changes, apt installs of nginx/RTMP/FFmpeg, systemd restarts, firewall/SSH/SSL changes, Docker socket mounts,
`docker system prune`, touching other Docker workloads or a native BlueStream install.

## Scenario A - fresh VPS

Needs Docker Engine + the Compose plugin and ~8 GB free disk, 2+ spare cores, 2+ GB free RAM.

```bash
# one-line PREFLIGHT (read-only: changes nothing, installs nothing)
git clone --branch feature/bluestream-studio https://github.com/Humran13/bluestream-relay-pro.git ~/bluestream-docker-src && git -C ~/bluestream-docker-src checkout <SHA> && bash ~/bluestream-docker-src/docker/bluestream.sh preflight

# one-line INSTALL (dry run first - prints the plan and changes nothing)
bash ~/bluestream-docker-src/docker/bluestream.sh up
# then, if the plan is what you want:
bash ~/bluestream-docker-src/docker/bluestream.sh up --execute --confirm "INSTALL BLUESTREAM DOCKER bluestream"
```

`up` always runs the preflight first; **any blocker stops the installation before anything is created.**

### If Docker is missing
The preflight reports it and installs nothing. Installing Docker changes the host (a package repository and a system service), so it is a
**separate step you approve and run yourself**, using Docker's official instructions for your OS (<https://docs.docker.com/engine/install/>),
then add your user to the `docker` group (or use sudo) and re-run the preflight. This project does not script that for you.

## Scenario B - VPS that already runs other apps (CloudPanel, other containers, native BlueStream)

Same commands. The preflight additionally:
- lists running containers and refuses a name/network/volume clash (`--project other-name` picks different names);
- picks the first free loopback port in 18090-18099 (an occupied or container-published port is never reused);
- detects a **native BlueStream** install and its running units. It is reported as a warning: the Docker copy is *separate* - its own volumes,
  config, admin login, licence, URL and ports. It **cannot see or change** `/etc/bluestream`, `/var/lib/bluestream`, `/var/www/bluestream`
  or any `bluestream-*` unit, and it is **not an upgrade of the native dashboard**. Do not publish the same YouTube key from both.
- checks 15-minute CPU load, RAM headroom and disk, and blocks if they cannot support a live 720p30 encode.

Use `--cpus 1.5 --mem 1500m` (any `up`/`preflight`) to cap the container so neighbours keep their resources.

### Future migration from native to Docker (investigated, NOT implemented, never automatic)
Native state is small and file-based: `/etc/bluestream` (relay/destination/playlist configs, stream keys, server.conf),
`/var/lib/bluestream` (web state, Studio store, media), `/var/www/bluestream` (HLS, regenerated). A non-disruptive path would be:
(1) install the Docker copy alongside (above) and verify it with a *new* test stream; (2) in a maintenance window, stop the native relays,
copy the three trees into the Docker volumes while the container is stopped (`docker run --rm -v <volume>:/dst -v <backup>:/src:ro ... cp -a`),
keeping ownership/permissions; (3) start the container and verify; (4) leave the native install stopped but intact for rollback. Licensing is
Ed25519 token based and tied to a per-install id, so a migrated install needs a re-issued token. This procedure needs a rehearsal on a copy
before anyone relies on it; it is documented only.

## Dashboard access

The dashboard is loopback-only, so nothing is exposed to the internet. From your PC:

```bash
ssh -L 8765:127.0.0.1:18090 <your-ssh-user>@<server>      # use the port `up` printed (see: bluestream.sh url)
```
Open `http://localhost:8765/console`. Username `admin`; password: `bash docker/bluestream.sh admin-password` (generated on first start, never
printed to logs). **Studio is the "Studio" tab** in that same dashboard - no second login. Use `http://localhost` (not a LAN address) because the
session cookie is only marked non-Secure for plain HTTP on localhost. A public HTTPS reverse proxy is a deliberate later step you configure yourself.

## First real YouTube livestream (start with ONE 720p30 stream)

1. YouTube Studio -> Create -> Go live: create a **private or unlisted** broadcast with a stream key **different from any key already live**.
2. Dashboard -> Media -> upload a video (or Studio -> project source = your media).
3. Studio tab (no licence or payment is involved) -> **New project** (pick a
   template, choose your media) -> add/edit overlays -> **Apply to Live** -> **Create stream**.
4. Destinations -> add YouTube: server `rtmp://a.rtmp.youtube.com/live2`, paste the key (it is stored root-only in the volume and never logged).
   Attach it to the Studio stream; press **Start**.
5. In YouTube Studio watch the Stream health; check the dashboard Streams page. Let it run; then test recovery: `docker exec bluestream-app pkill -9 ffmpeg`
   (the supervisor restarts it) and `docker restart bluestream-app` (enabled streams come back).
6. Stop, and remove the destination key when finished.

Do not paste a key into a chat, a GitHub issue, shell history or a screenshot. `diag`/`logs` redact keys defensively, but never rely on that.

## Diagnostics (read-only, redacted, scoped to this project's container)

```bash
bash docker/bluestream.sh status                 # container + units
bash docker/bluestream.sh diag --since 24h       # limits, health, usage, restart counts, unit logs, Studio diagnostics (no secrets)
bash docker/bluestream.sh logs --lines 200
```

## Safe rollback / removal

```bash
bash docker/bluestream.sh down                                   # dry run: lists exactly what this project owns
bash docker/bluestream.sh down --execute --confirm "REMOVE BLUESTREAM DOCKER bluestream"        # stops+removes the container and network; KEEPS data
bash docker/bluestream.sh down --execute --confirm "REMOVE BLUESTREAM DOCKER bluestream" --purge-volumes --confirm-data-loss "DELETE ALL DATA OF bluestream"   # also deletes THIS project's volumes (media, keys)
```
It only addresses resources labelled `com.bluestream.project=<project>`; it never prunes, never touches other containers/networks/volumes/images,
the host, nginx, firewall, SSH, CloudPanel or a native install. To go back to "nothing" use the second form then `docker image rm bluestream-relay-pro:local`.

## Known limitations
- Playlist **schedule timers** (`bluestream-schedule-*.timer`) are not supported by the in-container unit manager yet; streams, playlists, publishers, Studio are.
- A container restart is not a VPS reboot; enabled streams auto-start with the container (`restart: unless-stopped` also brings it back after a reboot).
- Browser-source overlays stay disabled (Chromium sandbox unverified on a real host).
- No real-VPS install, no real YouTube run, no 4-vCPU benchmark and no 72 h result for this Docker deployment are claimed (see STATUS.md / TESTS.md).
