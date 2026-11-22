# STATUS (update after every milestone)

Branch `feature/bluestream-studio` (draft PR #6, NOT merged, NOT deployed). Base: main `dbfb71b`.
Label: **V1 feature set implemented; release gate PENDING. NOT beta-ready.**
Open gates (see RELEASE_CHECKLIST.md): B staging VPS, C benchmark + 72 h soak, D payments provider, E sign-off of
sandbox/isolation on a real host.

## Live production round (round 11, branch `feature/studio-live-compositor`) - TESTED LOCALLY, NOT ON THE VPS
Diagnosis: a real server-side compositor already existed (one FFmpeg decode+compose+encode per Studio stream; text/countdown via sidecar files). What was
missing: proof, a playing picture in the editor, and a clear line between WATCHING a stream and CHANGING what viewers see. Design and limits:
`STUDIO_ARCHITECTURE.md`; release plan, impact table, rollback, limitations: `STUDIO_LIVE_RELEASE.md`.
Done (each has automated evidence, see TESTS.md): single editable canvas with a REAL playing `<video>` (uploaded video via an on-demand copy/540p preview,
existing relay/playlist and the Studio's own output via read-only HLS with loading/disconnected/retry states; local pause/restart/loop/mute never touch a
stream); Live-sources tab (monitor an existing relay without touching it; Dino is never started/stopped/restarted); create playlists from Media Library videos;
layer lock/rename/reorder/hide; text font/alignment/background box; countdown to a time or for a duration (absolute server-side moment); honest state chain
(draft saved / applied / pipeline running revision N / output healthy); hot apply for text content (no restart), staged restart with confirmation,
health verification and AUTOMATIC rollback for structural changes; encoder choice with a REAL NVENC init test (never automatic, never silently switched);
frame-level proof that text, rect, PNG, countdown, PiP video are in the encoded output; read-only restart forensics script for the 630-restart signal.
Still open: browser-source renderer enabled on a real host; real NVENC hardware encode; 72 h soak; VPS install. Production deployment: NOT performed.

## Studio workspace redesign (round 10, OBS-inspired) - TESTED LOCALLY, NOT ON THE VPS
Replaced the stacked admin form with a broadcasting workspace inside the SAME dashboard (same URL, login and session; no second app):
toolbar (project, unsaved-changes flag, Save Draft, Apply to Live, Preview/Program, Settings), left Media panel, centre 16:9 canvas, right
Properties, bottom Layers / Stream status / Controls; collapsible and resizable panels; one column on phones.
What is functional (each has automated tests, see TESTS.md):
- Media browser over the EXISTING Media Library: thumbnails (images direct, videos via a still), name/size/format, search, kind filter, upload with
  progress for video, PNG/JPG/JPEG/WebP/GIF and audio (existing quarantined ffprobe import; images capped at 10 MB), visual picker everywhere a file name
  used to be typed (including New project). No filesystem path is ever accepted from the browser.
- Add Source menu: Media (video overlay, image, background video, image slideshow), Graphics (text, colour, clock, countdown, countup, pomodoro, ticker,
  now playing, live data), Streaming (existing stream, existing playlist, audio file). Browser source appears only when its gate is open.
- Properties editor per source; drag, resize (aspect-locked for pictures: the box follows the FFmpeg scale-by-width rule), alignment guides, snapping
  (edges / sources / centre, sensitivity), keyboard nudge and resize, layer order, visibility, duplicate, remove, crop.
- Stream controls over the existing relay operations: Create Stream (applies the saved draft once if nothing was applied), Start, Stop (confirmation),
  live status from FFmpeg progress, publisher states, explicit "Switch to this stream" for BLOCKED destinations. Draft saves never touch a live stream.
- Settings: General (theme, confirmations, snapping, safe areas, grid), Stream (attach existing destinations; key presence only, never the key),
  Output (preset, video bitrate, encoder), Audio (bitrate), Video (480p/720p/1080p, fps, scaling filter), Hotkeys (rebindable, duplicates refused),
  Advanced (refresh intervals). Everything is validated on the server; output settings are checked against this server's CPU (an impossible output is refused,
  existing projects are never locked).
- Program view shows exactly the last applied revision, read-only (a real preview/program split on top of draft/published).
NOT offered because it cannot work in a browser/VPS architecture: screen, window, game and camera capture, recording, virtual camera, NVENC (no GPU is
detected on typical VPSs and the pipeline does not use one; the UI reports why), a mixer, font choice (only DejaVu Sans is installed for drawtext).
Deployment: same one-command activation (dry run by default); new files ride the existing manifest. Production deployment: NOT performed.

## Round-2 owner review items
| # | Item | State |
|---|---|---|
| 1 | OBS-like editor: drag, resize, crop, layer order, visibility, real source frame, keyboard + touch, Save/Apply/rollback | DONE; 9 Playwright tests (Chromium, Firefox, WebKit in CI) |
| 2 | Browser source | IMPLEMENTED but **gated OFF**: not enabled until staging verifies sandbox + systemd isolation (see below) |
| 3 | Now Playing from the Playlist engine | DONE (titles file + supervisor start time + durations) |
| 4 | Data overlay GUI (URL, path, refresh, format, text, position) | DONE; same validated model as the backend |
| 5 | Media sources | DONE: video loop, audio file + background + optional waveform, image slideshow (seconds/shuffle), video picture-in-picture overlay. Existing Playlist unchanged |
| 6 | Stream ownership / switching | DONE: kernel flock per destination, BLOCKED state, explicit takeover with rollback |
| 7 | Health & resource monitoring | DONE: FFmpeg progress listener (speed/fps/drops), CPU + RAM of the encoder, restarts, last failure, HLS output state |
| 8 | Security review | DONE for this round; defects found and fixed (list in TESTS.md); independent review still open |
| 9 | Tests | automated tests for every feature above; evidence in TESTS.md |
| 10 | Docs | updated |

## Production canary package (owner-run on the existing VPS)
Prepared in `tools/canary/` + `docs/bluestream/CANARY.md`: read-only production-canary preflight (separate from the staging gate; the production
hostname does not block it), guarded installer wrapper (dry run by default; needs a snapshot id, the vendor PUBLIC key and a phrase naming the host;
never passes --with-ufw/--with-ssl, never enables browser capture, never starts a stream, restarts only bluestream-web), targeted rollback (dry run
by default; only bluestream-* things; keeps data unless --purge-data; never reloads nginx), read-only diagnostics with redaction.
Compatibility audit of the installer: CloudPanel -> isolated loopback nginx, no /etc/nginx writes, no firewall/SSH/SSL changes, Docker untouched.
ONE real side effect found and disclosed: `libnginx-mod-rtmp` adds a `load_module` line under /etc/nginx/modules-enabled and queues a graceful
system-nginx reload; the preflight blocks unless nginx is apt-managed, provides ABI nginx-abi-1.24.0-1, is not changed by apt, and `nginx -t` passes.
NOTHING has been run on the VPS: no installation or livestream has succeeded yet. The owner performs it; results go in TESTS.md only when real.

## Docker validation (local, Windows 11 + Docker Desktop/WSL2)
Done: full suite 838 passed, real nginx-rtmp/FFmpeg end-to-end, real-account permission tests, editor 27/27 in 3 browsers, 10-minute benchmark
(CPU 0.79 median / 0.99 max cores, speed >=1.00x, 0 drops/restarts), container-restart recovery. Details and defects fixed: TESTS.md.
**72-hour Docker soak: RUNNING, NOT A PASS.** Started 2026-11-21T06:07:35Z at commit `d79098d` in container `bs-studio-soak72` (4 CPU / 8 GB cap,
restart policy none, results on D:). It finishes about 2026-11-24T06:08Z if Docker Desktop and the PC stay awake and running; any interruption (sleep, Windows
update, Docker restart) invalidates it and it must be restarted. Check progress read-only: `bash tools/docker/soak-status.sh`. The verdict is only valid when
`D:/bluestream-studio-docker/soak72/result/verdict.json` exists and says `72h-PASS-CANDIDATE` with restarts 0, stalls 0, speed_min >= 1.0.
This is a Docker-on-workstation result; it does not replace the VPS soak (Gate C). On `proceed`: run soak-status.sh, record the numbers in TESTS.md honestly.
Still VPS-only: systemd units, sandbox under the unit, IP deny/cgroups, reboot, real playlist, real destinations.

## SCOPE CORRECTION (round 9): licensing/payment is DEFERRED
Licensing and payments were never requested for this phase. Studio no longer requires a licence, vendor key or token: `entitlement.current()` returns "open" unless an
operator sets `BLUESTREAM_STUDIO_ENFORCE_LICENCE=1` in the future. The signature-verification code is kept dormant (not removed, no replacement mechanism); the
licence panel is hidden. Payments/billing/commercial restrictions are a separate future task the owner will start. Enforcement tests opt in explicitly;
`DeferredLicensingTests` pin the default. Studio is fully usable with no key.

## Native in-place Studio activation (owner-run, round 8) - TESTED IN A CONTAINER ONLY
`tools/canary/studio_activate.sh` (+ `studio_activate_rollback.sh`, `studio_smoke.py`) activates Studio inside an EXISTING native dashboard (same URL/login/session).
Dry-run by default; `--execute` stages+validates, backs up, replaces files atomically (running streams keep old inodes), adds a minimal web-unit drop-in
(`BLUESTREAM_STUDIO_ENABLED=1`, `ReadWritePaths=/var/lib/bluestream/studio`), `daemon-reload`, restarts ONLY `bluestream-web.service`, smoke-tests (anonymous refusal,
authenticated pages, Studio tab, assets, CSRF, sandbox write probe), compares stream units before/after, auto-rolls back on failure (never touching streams).
Exercised in a Docker container with a systemd shim, including a forced failure -> automatic rollback and a manual rollback. NOT run on the real VPS: Claude has no
access to it; the owner runs it. No licence is involved (see the scope correction above).

**72 h Docker soak: INVALIDATED.** The Docker daemon stopped (host sleep/restart) at about 12.3 h (last sample 2026-11-21 21:25 UTC; container exit 255, restart=none). It is NOT a pass
and no 72 h result exists; a new run would have to start from zero.

## Docker-first deployment (Compose) - BUILT AND TESTED LOCALLY ONLY
`docker/` = one image + one Compose project containing the whole product (console, Studio tab, nginx-rtmp, FFmpeg, publisher). Host-side CLI:
`docker/bluestream.sh` (`preflight`, `up`, `status`, `url`, `logs`, `diag`, `admin-password`, `down`), guide: `docs/bluestream/DOCKER.md`.
Isolation: non-privileged, read-only rootfs, cap_drop ALL (+8), named volumes only, loopback-only dashboard port, no Docker socket, CPU/RAM/pids limits,
all names/labels per project, rollback scoped to the project's own labelled resources. The real, unmodified `install.sh` runs at image build; `bsunitd`
replaces systemd inside the container.
**Verified in Docker Desktop (throwaway project, 2026-11-21):** preflight pass/block logic, dry-run-by-default `up`/`down`, image build, healthy start,
login, Studio tab in the same console, licence activation with a TEST key, Studio project -> publish -> stream, live x264 composition 720p30 H.264/AAC,
publisher to a local sink, FFmpeg kill recovery, `docker restart` auto-start, in-container `selftest` 21/21, no stream key in container logs/unit logs/diag,
scoped teardown with `--purge-volumes`. 21 static/stub tests in `webapp/test_docker_deploy.py`, 15 in `test_docker_unitd.py`.
**Real bugs found by this and fixed (they also affected native installs):** `bs_have_ffprobe` was never defined (CLI media import always refused);
`publisher_sync_dropin` returned failure for every non-looping-local-file relay (all Studio streams), so a YouTube destination would never have started.
**NOT done / NOT claimed:** no install on a real VPS, no real YouTube livestream, no preflight run on a real Linux VPS (Windows Git Bash has no `/proc`:
RAM/CPU/disk checks only warn there), no 72 h or 4-vCPU result for the Docker deployment, no playlist schedule timers in the container, no
native->Docker migration (design only). The 72 h soak in `bs-studio-soak72` was running concurrently on the same Docker VM during these tests.

## Staging status: BLOCKED (no staging VPS accessible)
No staging host, SSH config or credentials exist in this environment. The only VPS ever mentioned (`vmi2980718`) is PRODUCTION and
was deliberately not used. No staging test has run; every `U` item in TASKS.md and every `[ ]` in RELEASE_CHECKLIST.md Gates B/C/E stays open.
Prepared offline: `tools/staging_preflight.sh` (+ tests) that refuses production-like/under-spec hosts, hardened failure-injection steps
in STAGING.md. **Exact next action for the owner:** provision a separate Ubuntu 24.04 / 4 vCPU / 8 GB VPS, give Claude SSH access to it
(host, user, key path), run the preflight; then `proceed` continues at STAGING.md section 0.

## Readiness
Code is **ready for staging validation** (commit `5cc56bd`, CI green, local 783 passed). It is NOT validated on a VPS and NOT beta-ready.
Follow `docs/bluestream/STAGING.md`; browser capture stays disabled by default.

## Media-preview access (round 4)
- Problem confirmed from the code: media is `root:bluestream-relay 0640` in a `0750` dir and HLS is nginx `0750`; the console user
  `bluestream-web` can read neither, so real editor previews could not work on a hardened VPS.
- Fix (no permission changes at all): two READ-ONLY bridge operations, `studio_frame` and `studio_thumb`. web-ctl runs the Studio
  CLI as root, which immediately drops to the account that owns the data (HLS -> nginx user, media -> bluestream-relay), runs
  FFmpeg, and returns a small scaled JPEG/PNG. No new group membership, ACLs, chmod or sudoers entries.
- Because nothing about ownership/modes changed, fresh install, upgrade, new uploads, imports and backup/restore cannot regress it;
  static tests pin the existing modes and forbid loosening.
- NOT yet verified on a VPS: that the bridge path works end to end (STAGING.md section 1).

## Pre-staging verification (functional commit `97969dc`)
- Full local suite 760 passed / 39 skipped; CI python, shell, node, browser all green (python after one infra re-run).
- Pre-staging blockers found by owner review, both fixed: browser unit account mismatch; soak harness init order.
- Static checks of unit installation/permissions pass (root-owned 0644 unit, run dir `bluestream-relay:bluestream-relay` 0750).
- Browser source remains disabled by default (flag unset in every shipped unit/script; gate tested).
- Still NOT validated on a VPS: unit start-up, Chromium sandbox under the unit, IPAddressDeny/cgroup behaviour, media readability by the web console for editor previews.

## Browser source: what is and is not proven
- Proven by tests (CI, real Chromium, test-only `--no-sandbox`): element and viewport capture sizes, per-request egress policy
  blocks an unlisted internal address, identical captures are not rewritten, hung page times out and leaves no browser
  processes, last good image survives failures, backoff, spec validation, gate defaults.
- NOT proven (needs a real host): Chromium's own sandbox on Ubuntu 24.04 (AppArmor can block user namespaces; the worker then
  refuses to run), the systemd unit's cgroup limits and `IPAddressDeny` behaviour, CPU/RAM of real pages.
- Therefore: default OFF, shipped in the package, enabled only by `BLUESTREAM_STUDIO_BROWSER=1` after the staging checklist.

## Exact next task
1. Owner: review PR #6; provision a staging VPS; run RELEASE_CHECKLIST gates B/C/E (including browser-source isolation).
2. Claude on `proceed`: fix any CI/staging findings; no feature work is pending. Candidate follow-ups (not agreed scope):
   per-destination ownership UI on the Streams page for plain Relays; undo/redo beyond revision rollback.

## Standing decisions
- Studio stream = relay TYPE=studio (reuses publishers/HLS/boot); one publisher per destination enforced by flock.
- Dev box has no ffmpeg/flock/tzdata/Chromium-on-Linux: those tests skip locally and run in CI (ubuntu-24.04).
- Pushing cancels in-flight CI (concurrency); batch pushes.
- Payments remain NOT LIVE (pending provider + credentials). Manual licence tool unchanged.
