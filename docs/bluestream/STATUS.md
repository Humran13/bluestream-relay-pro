# STATUS (update after every milestone)

Branch `feature/bluestream-studio` (draft PR #6, NOT merged, NOT deployed). Base: main `dbfb71b`.
Label: **implementation in progress (round 2); release gate PENDING. Not beta-ready.**

## Round 2 (owner review) - progress
| # | Item | State |
|---|---|---|
| 3 | Now Playing from Playlist engine | DONE (titles file + supervisor start time + durations; unit + shell tests) |
| 6 | Stream ownership / safe switching | DONE (flock per destination, BLOCKED state, controlled takeover with rollback; real-flock tests run in CI only) |
| 7 | Health & resource monitoring | DONE (loopback FFmpeg progress listener, CPU/RAM, restarts, drops, failure; console panel; real-ffmpeg test in CI) |
| 1 | OBS-like visual editor (drag/resize/crop/layers + real preview + browser tests) | **NEXT** |
| 4 | Data overlay GUI (URL/path/refresh/format) | NEXT (backend validated; GUI fields + formatting + test pending) |
| 5 | Media sources: audio/video sources, video overlay, slideshow controls | NEXT |
| 2 | Browser source (CDP over pipe, systemd egress filter, limits) | NEXT - stays gated OFF until its tests pass |
| 8 | Security/reliability review | partial: found+fixed NUL bytes in web-ctl; more after items above |
| 9/10 | Tests + docs | ongoing |

## Exact next task
Build the interactive editor: `webapp/static/js/studio_editor.js` (pointer-event drag/resize/handles, layer list with
reorder + visibility, writes the same hidden form fields the server already validates), real source preview frame
(`/console/studio/<name>/frame.jpg`), crop fields (Overlay.crop_*), and Playwright tests under `tests/browser/`
(needs a Flask test server script like `tests/browser/studio-server.py`).

## Known facts / decisions
- Studio stream = relay TYPE=studio (reuses publishers/HLS/boot logic); one publisher per destination enforced by flock.
- Dev box has no ffmpeg/flock/tzdata: those tests skip locally and run in CI (ubuntu-24.04).
- Pushing cancels in-flight CI (concurrency); batch pushes.
- Payments remain NOT LIVE (pending provider + credentials). Manual licence tool unchanged.
