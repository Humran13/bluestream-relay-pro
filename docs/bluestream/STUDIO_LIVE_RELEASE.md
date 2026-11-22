# Studio live-production release plan (branch `feature/studio-live-compositor`)

NOT deployed. NOT run against the production VPS or the Dino stream. See `STUDIO_ARCHITECTURE.md` for the design and diagnosis.

## What changed, and what each shared component could affect
| Component | Change | Effect on running production |
|---|---|---|
| `webapp/studio/*`, templates, static JS/CSS | new workspace (single editable canvas with real playing video), JSON API, hot/staged apply, encoder gate, fonts/alignment/box, lock/rename | Only the dashboard process (`bluestream-web.service`) loads it. Studio is opt-in (flag), isolated. |
| `web-ctl` | ONE new read-only op `studio_proxy <media>` (raw fragmented-MP4 on stdout; name validated twice; FFmpeg runs as the media owner, never root) | Existing ops untouched. Covered by the existing sudoers rule (no change). |
| `webapp/engine.py` | `proxy_argv()` only | none |
| `studio.cli` (run by `run-relay.sh` for `type=studio` relays) | sidecar hot-reloads text content, reports running revision / plan / pending restart; `argv` refuses an unusable NVENC (exit 6) and writes the plan file | Takes effect for a Studio stream only when THAT stream is next started. Already-running Studio streams keep their old process untouched until restarted. |
| `lib/*.sh`, `run-relay.sh`, publisher units, nginx, CloudPanel, Docker, firewall | **not modified** | Dino relay + publisher are never restarted by this release. |

## Deployment (owner-run; nothing is done for you)
Same guarded one-command activation as before (dry run first; stops unless the Dino relay + publisher + FFmpeg are positively identified; verified backups; only `bluestream-web.service` is restarted; automatic verified rollback). The exact command with the final commit is printed in the delivery message. Order for the owner:
1. `bash tools/canary/restart_forensics.sh --stream dino` (read-only) and keep the output: it answers what the 630 publisher restarts were.
2. Activation dry run, then `--execute` in a quiet moment.
3. First real checks, none of which can disturb Dino: open Studio → *Live sources* → choose `dino` (read-only monitor); create a project on an uploaded video; create a SEPARATE Studio stream and publish it to a private/unlisted YouTube test broadcast with a key no other stream uses.
4. Only when satisfied, and as a deliberate decision, use the existing explicit "Switch to this stream" to move a real destination from Dino to a Studio stream. This is the only step that changes what viewers of Dino see, and it is never automatic.

## Rollback
`tools/canary/studio_activate_rollback.sh` (verified restore of every replaced file from the byte-checked backup, dashboard-only restart, reports `ROLLBACK INCOMPLETE` instead of claiming success). A Studio stream that went wrong after an Apply is reverted from the UI (automatic when the new revision never becomes healthy; or "Load into draft" + Apply). Hot applies cannot break a stream: they only change text files.

## Remaining limitations (verified facts, not hopes)
* Browser source stays OFF by default: it needs the isolated, egress-restricted Chromium renderer verified on a real host (sandbox under systemd). Menu item is visible, disabled, with the reason. Native text/image/countdown/clock/ticker/data/video-PiP overlays do not need it.
* NVENC is wired (encoder option, CBR low-latency args, real one-frame init test, refusal instead of silent fallback) but was only exercised on a machine WITHOUT a usable GPU encoder: the refusal path is verified, a real hardware encode is UNTESTED.
* Preview of a live stream is HLS (≈6–15 s behind) and read-only. A relay that is stopped shows "stopped"; it is never started by Studio.
* Moving/resizing/adding layers on a running Studio stream is a staged restart of that one stream (10–20 s gap), with confirmation, health check and automatic rollback. Content-only text changes are hot. FFmpeg cannot add inputs to a running filter graph.
* Playlist "shuffle / repeat one" are not offered: the existing playlist engine loops its list in order only.
* Measured soak is minutes, not the 72 h gate (still PENDING); CPU numbers are from a container, not a VPS.
* The 630 historical publisher restarts cannot be explained from the repository; run the forensics script.
