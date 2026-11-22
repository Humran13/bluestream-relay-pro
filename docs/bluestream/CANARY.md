# BlueStream Studio V1 - owner-controlled production canary

You (the owner) install and run this yourself on your existing VPS. Nobody else needs SSH access or credentials.
Nothing here has been run on your VPS yet: **do not treat any step as "known to work" until you have done it and looked at the output.**
Results from Docker on a workstation do not prove this host's behaviour.

## What the installer will and will not do on a CloudPanel server (checked against the code, not assumed)
| Topic | Behaviour |
|---|---|
| CloudPanel | Detected (`clpctl`, `/etc/cloudpanel`, `/home/clp`). BlueStream then uses its **own** nginx instance (`bluestream-nginx.service`), loopback only. It never writes to `/etc/nginx` itself. |
| System nginx | Not restarted. **One honest side effect:** the Ubuntu package `libnginx-mod-rtmp` (a dependency) adds one `load_module` line at `/etc/nginx/modules-enabled/50-mod-rtmp.conf` and queues a **graceful reload** of the system nginx at the end of the apt run. The preflight checks that nginx is apt-managed, provides the required ABI (`nginx-abi-1.24.0-1`), would not be upgraded/replaced, and passes `nginx -t`; otherwise it BLOCKS. |
| Other packages | The preflight runs `apt-get -s` (simulation) and blocks if anything would be removed or any critical package (nginx, docker, openssh, wireguard, mysql/mariadb, php, libc, systemd) would be upgraded. |
| Ports | Private RTMP bus `127.0.0.1:1935`, console `127.0.0.1:<8080...>`, loopback gateway. **Nothing is opened publicly.** |
| Firewall / SSH / certificates | Untouched (the wrapper never passes `--with-ufw` or `--with-ssl`). |
| Docker (RustDesk), WireGuard | Never touched. |
| Public exposure | None. You reach the console through an **SSH tunnel**. (Optional later: a CloudPanel reverse proxy with its HTTPS; not needed for the first test.) |
| Browser capture | Stays **disabled**. |
| Payments | Not configured or required. Licensing is the existing manual token mechanism. |

## 0. Get the exact code
```bash
git clone --branch feature/bluestream-studio --single-branch https://github.com/Humran13/bluestream-relay-pro.git bluestream-canary && cd bluestream-canary && git rev-parse HEAD
```
Compare the printed commit with the SHA you were given. (Use `git clone` with your own GitHub access if the repo is private.)

## 1. Read-only preflight (changes nothing)
```bash
sudo bash tools/canary/canary_preflight.sh
```
Read every WARN. If it says `BLOCKED`, stop and send me the output. A pass is necessary, **not sufficient**.

## 2. Your decisions (do these yourself, before installing)
1. **Take a provider snapshot** of the VPS and write down its name/id. This is your real rollback.
2. Decide the live test is acceptable for the sites on this server (a single 720p30 stream uses about 1 CPU core and ~350 MB RAM in Docker measurements; the VPS figure is what we are about to measure).
3. On **your own PC** (not the VPS) create the licence key pair and a token:
```bash
python tools/studio_license.py keygen --out studio-vendor.key        # prints the PUBLIC key (64 hex characters). Keep the .key file private.
python tools/studio_license.py issue --key studio-vendor.key --lic canary-1 --days 30 --grace-days 3
```

## 3. Install (dry run first, then execute)
Dry run (prints the plan, changes nothing, runs the preflight again):
```bash
sudo bash tools/canary/canary_install.sh --snapshot-id "<your snapshot id>" --vendor-pubkey <64-hex PUBLIC key>
```
**Only after the preflight is clean and the snapshot exists**, run the installation (it asks for the exact phrase naming this host):
```bash
sudo bash tools/canary/canary_install.sh --snapshot-id "<your snapshot id>" --vendor-pubkey <64-hex PUBLIC key> --execute --confirm "INSTALL BLUESTREAM CANARY ON $(hostname)"
```
It runs the existing `install.sh --non-interactive --no-ufw --no-ssl`, writes the public key, enables Studio for the console only
(`BLUESTREAM_STUDIO_ENABLED=1`, a BlueStream-only systemd drop-in), restarts **only** `bluestream-web`, and writes a receipt. It starts no stream.

Then create the console admin password (typed on the server, never on a command line):
```bash
sudo bluestream-manager admin init
```

## 4. Open the dashboard (no public exposure)
On the VPS find the console port, then from **your PC** open a tunnel:
```bash
sudo grep WEB_PORT /etc/bluestream/server.conf
ssh -L 8765:127.0.0.1:<WEB_PORT> <your-ssh-user>@<your-server>
```
Browse to **http://localhost:8765/console** (use `localhost`, not another hostname) and log in. If login loops, tell me; the session cookie policy is derived from the SSL state.
Studio -> **Licence**: paste the token from step 2. The Studio tab also needs the licence to create or edit projects.

## 5. First real YouTube livestream (private/unlisted, your own key)
1. YouTube Studio -> Create -> Go live -> **Stream**. Set visibility **Private** or **Unlisted**. Note the *Stream URL* (`rtmp://a.rtmp.youtube.com/live2`) and *Stream key*.
2. In BlueStream: **Media Library** -> upload a test video (and a PNG logo). Then **Destinations** -> add a YouTube destination: paste the URL and key **into the form** (they are stored root-only and shown masked). Never put the key in a command line, a chat, a file in Git, or a diagnostics bundle.
3. **Studio** -> New project (source: your uploaded video; template: Blank or Clock) -> add overlays (text, clock, countdown, ticker, logo image) -> **Save draft** -> check the editor preview -> **Apply to Live**.
4. **Create stream** -> **Destinations** (on that stream) -> attach the YouTube destination -> **Start**.
5. Watch YouTube Studio's stream preview: you should see the video with your overlays. In BlueStream's Studio page the **Health** panel shows encoder speed (should stay about 1.00x), CPU, RAM, restarts and the destination state (LIVE).
6. Stop and Start again from the console; confirm YouTube recovers. Use **Apply to Live** with a change while running (it asks to restart that stream only).

## 6. Long-duration test rules
- One stream, 720p30. **Do not run benchmarks or other heavy jobs on this VPS during the test.**
- A "72-hour success" means 72 hours **continuous** with YouTube showing the stream live. Any stop, crash loop or manual restart resets the clock; report what happened. Less than 72 hours is never reported as a pass.
- Each day, collect diagnostics (read-only) and keep them:
```bash
sudo bash tools/canary/studio_diagnostics.sh --since 24h > /tmp/bluestream-diag-$(date +%F-%H%M).txt
```
It restarts nothing, changes nothing, never reads stream keys, and redacts keys/tokens/passwords (it ends with a self-check; still read it before sharing). Share the file when something looks wrong.
- Record: YouTube "stream health" messages, any gap in the video, the Health panel numbers (speed, dropped frames, restarts), and the times.

## 7. Rollback
Preview the exact plan (changes nothing):
```bash
sudo bash tools/canary/canary_rollback.sh
```
Execute (stops and removes ONLY `bluestream-*` units/files and the BlueStream sudoers entry; keeps media, projects and configs; never reloads or restarts nginx, never touches Docker/firewall/SSH/CloudPanel):
```bash
sudo bash tools/canary/canary_rollback.sh --execute --confirm "ROLLBACK BLUESTREAM CANARY"
```
Add `--purge-data` only if you also want to delete BlueStream's own data (it contains your stream key and media).
For a complete return to the previous state, restore the **provider snapshot**. Packages (nginx, ffmpeg, libnginx-mod-rtmp ...) stay installed on purpose; the one `load_module` line in `/etc/nginx/modules-enabled/` is harmless and is left alone.
After any rollback, check that your sites, CloudPanel, RustDesk and WireGuard still work (the script never touched them).

## 8. What I need from you if something goes wrong
The diagnostics file (never your stream key), the exact step you were on, and what YouTube showed. I will give a targeted fix based on the logs; I do not and will not access the server.
