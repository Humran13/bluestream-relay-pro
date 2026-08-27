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

## 1. Upload the project

Copy the project directory to the server, for example:

```bash
scp -r bluestream-relay-pro root@SERVER:/opt/
```

## 2. Run the installer

```bash
cd /opt/bluestream-relay-pro
sudo bash install.sh
```

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
override them with flags.

## 3. First steps

```bash
sudo bluestream-manager
```

- Relay Management > **Add Relay** — add your first source.
- Media Management > **Import Media** — for playlist channels.
- Server Management > **Diagnostics** and **Self-Test** — verify everything.

## 4. DNS and firewall

Point `video.example.com` at the server's public IP before requesting SSL.

If you enabled UFW during install, confirm these rules exist:

```text
22/tcp   (SSH)
80/tcp   (HTTP)
443/tcp  (HTTPS)
```

## 5. SSL (Let's Encrypt)

The installer requests a certificate when `--with-ssl` is used. You can also
run it later from the manager: **Server Management > SSL Management**.

Renewals are handled by certbot's systemd timers automatically.

## Post-install layout (installed system)

| Path | Purpose |
| --- | --- |
| `/etc/bluestream/` | Root-only configuration (relays, playlists, server.conf) |
| `/var/lib/bluestream/media/` | Managed media files |
| `/var/lib/bluestream/run/` | Runtime concat files |
| `/var/lib/bluestream/backups/` | Configuration backups |
| `/var/www/bluestream/hls/` | HLS output (relay/ and playlist/) |
| `/var/www/bluestream/web/` | Bundled hls.js player |
| `/usr/local/sbin/bluestream-manager` | Manager command |
| `/usr/local/bin/bluestream-status` | Quick status command |
| `/usr/local/lib/bluestream/` | Installed scripts and docs |
| `/etc/systemd/system/bluestream-relay@.service` | Relay unit template |
| `/etc/systemd/system/bluestream-playlist@.service` | Playlist unit template |
