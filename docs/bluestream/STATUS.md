# STATUS (update after every milestone)

Branch `feature/bluestream-studio` (draft PR #6, NOT merged, NOT deployed). Base: main `dbfb71b`.
Label: **V1 feature set implemented; release gate PENDING. NOT beta-ready.**
Open gates (see RELEASE_CHECKLIST.md): B staging VPS, C benchmark + 72 h soak, D payments provider, E sign-off of
sandbox/isolation on a real host.

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
