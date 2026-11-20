# Studio staging verification procedure

Target: a **separate staging VPS**, Ubuntu 24.04, 4 shared vCPU, 8 GB RAM, same network class as production.
Never run this on the production VPS. Use a **test** YouTube stream key (unlisted/private test broadcast) or a local
RTMP sink; never a production key. Record every result (command output, timestamps) in `docs/bluestream/TESTS.md`;
a gate is "passed" only with that evidence. Nothing in this file has been executed yet.

## 0. Prepare
0. **Preflight guard** (refuses production-like or under-spec hosts): on the staging VPS only,
   `echo STAGING-OK | sudo tee /etc/bluestream-staging`, take a provider snapshot, then
   `sudo tools/staging_preflight.sh --snapshot-id "<snapshot id>"`. Continue only if it ends with `ALL CHECKS PASSED`.
   It checks: explicit staging marker, hostname not a production host, no CloudPanel, Ubuntu 24.04, >= 4 CPUs, ~8 GB RAM,
   >= 20 GB free, snapshot id recorded, SSH service up. It changes nothing on the host.
1. Provision the VPS; `apt update && apt -y upgrade`; snapshot the VM (rollback point).
2. `git clone` the repo, `git checkout feature/bluestream-studio`, note `git rev-parse HEAD`.
3. Install normally: `sudo ./install.sh` (and the web-console step). Confirm `systemctl is-active nginx bluestream-web`.
4. Generate a vendor test key OFF the VPS (`tools/studio_license.py keygen`); put the PUBLIC hex in
   `/usr/local/lib/bluestream/studio/vendor_pubkey.hex`; issue a 7-day token; keep the private key elsewhere.
5. Enable Studio only: `sudo systemctl edit bluestream-web` -> `[Service]` `Environment=BLUESTREAM_STUDIO_ENABLED=1`;
   `sudo systemctl restart bluestream-web`. Do NOT set `BLUESTREAM_STUDIO_BROWSER` yet.
6. Upload a short video, two images (one PNG with transparency), one audio file through the console Media Library.

## 1. Permissions and previews (gate B)
```
namei -l /var/lib/bluestream/media/<file>             # expect root:bluestream-relay 0640 in a 0750 dir
namei -l /var/www/bluestream/hls/relay                # expect www-data 0750
id bluestream-web                                     # expect ONLY group bluestream-web
sudo -u bluestream-web cat /var/lib/bluestream/media/<file> >/dev/null   # MUST FAIL (permission denied)
sudo -u bluestream-web sudo -n /usr/local/lib/bluestream/web-ctl studio_thumb <image.png> | head -c 200  # JSON with mime + data
```
In the console: Studio -> new project from each source type (video, slideshow, audio + image background, a running Relay,
a running Playlist). Expected: the editor background shows a real frame; image overlays show the real image.
Then: upload a NEW image, re-run the installer (upgrade), import another file, run a config backup + restore:
re-check `namei` modes and previews after each. Pass = modes identical to before, previews still work.
Record any case where the preview falls back to the neutral background.

## 2. Install / upgrade / rollback (gate B)
- Clean install already done in step 0 (record the installer output).
- Upgrade: `git pull`, re-run `install.sh`; existing Relay/Playlist streams keep running (check `systemctl status` start times).
- Restore the VM snapshot, repeat the upgrade, confirm the same result. Backup/restore: `bluestream-manager` backup, wipe
  `/var/lib/bluestream/studio`, restore, confirm projects and revisions return.

## 3. Functional end to end (gate B)
1. Studio project -> Apply to Live -> Create stream -> attach the TEST destination -> Start. Verify the HLS player plays and
   the destination shows LIVE.
2. Each overlay kind once: text, clock, countdown, Pomodoro, ticker, data (use a public JSON URL), image + crop, video PiP.
3. Now Playing: playlist source, watch the title change as items advance; stop/start the playlist and confirm it resets.
4. Draft vs Apply: edit while running; confirm nothing changes until Apply; Apply restarts only that stream; rollback works.
5. Ownership: attach one destination to a plain Relay and to the Studio stream; start both. One must show BLOCKED; use
   "Switch to this stream"; `kill -9` the owner's ffmpeg and confirm the lock frees; switch back.
6. Close the browser and shut the editing PC: the stream continues.

## 4. Failure injection (gate B)
Every injection must be reversible and must not endanger the host:
- `kill -9 <studio ffmpeg>`: it auto-recovers; nothing to undo.
- Corrupt / delete media: only copies of TEST media (`cp` first); restore the copy afterwards.
- Disk pressure: NEVER fill `/`. Mount a small dedicated tmpfs over the Studio cache only
  (`sudo mount -t tmpfs -o size=64m tmpfs /var/lib/bluestream/run/studio-cache`), fill THAT, observe the refusal, `sudo umount` it.
  Keep `df -h /` visible and stop if `/` drops below 10 GB free.
- Destination outage: block only the destination's IP and port, with automatic expiry so a forgotten rule cannot persist
  (`sudo iptables -I OUTPUT -d <dest-ip> -p tcp --dport 1935 -j DROP; sleep 90; sudo iptables -D OUTPUT -d <dest-ip> -p tcp --dport 1935 -j DROP`
  in one command line). Never touch rules for port 22 and never use `iptables -F` or `ufw reset`.
- Reboot: `sudo systemctl reboot` from the provider console session open in a second window; confirm SSH returns.
Watch the Health panel in each case; record the state it shows.

## 5. Performance (gate C)
```
python3 tools/studio_soak.py --minutes 10 --out /var/tmp/studio-quick       # quick benchmark
python3 tools/studio_soak.py --hours 72  --out /var/tmp/studio-soak         # run inside tmux/screen or a systemd-run unit
```
Pass criteria for the 72 h run (`verdict.json`): `gate = 72h-PASS-CANDIDATE`, `restarts = 0`, `stall_samples = 0`,
`speed_min >= 1.0`, CPU p95 within the ~2-2.5 core budget. Run it with nothing else heavy on the host; also keep a
Relay stream running to prove isolation. If it fails, benchmark a lower profile and record honest limits. **Do not
report the 72 h gate until it has actually run for 72 hours.**

## 6. Security checks (gate E)
- `systemd-analyze security bluestream-browser@x.service` (review the exposure score), and confirm the unit file is
  `root:root 0644`.
- Only if you decide to evaluate the browser source: install Chromium, set `BLUESTREAM_STUDIO_BROWSER=1` in the relay
  unit's environment, start a Studio stream with a browser overlay, then verify: `systemctl status bluestream-browser@<relay>`
  active as `bluestream-relay`; the capture PNG appears in `/var/lib/bluestream/run`; as that user a connection to
  `127.0.0.1`/`169.254.169.254` from inside the unit fails; `systemctl stop` leaves no `chrome` processes
  (`pgrep -a chrom`); memory stays under the caps. If Chromium's sandbox cannot start (AppArmor userns policy) the worker
  must REFUSE to run: record that. Leave the feature disabled afterwards unless every item passed.
- Licence: wrong key, expired token, revoked token, token bound to another install id are all refused; an expired token
  inside its grace window still streams.

## 7. Sign-off
Fill the `[ ]` items in `RELEASE_CHECKLIST.md` only with evidence. Anything that fails stays open and blocks release.
Roll back by restoring the VM snapshot (or the config backup and `systemctl restart bluestream-web`).
