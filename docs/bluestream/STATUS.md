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
