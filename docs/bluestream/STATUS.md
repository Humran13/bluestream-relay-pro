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
