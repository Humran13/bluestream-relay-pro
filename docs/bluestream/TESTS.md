# TESTS

## Docker Desktop / WSL2 results (local container, NOT a VPS)
Environment: Windows 11, Docker Desktop 29.7.2 + WSL2, image `bs-studio-test:local` (Ubuntu 24.04, FFmpeg 6.1.1, nginx 1.24 + rtmp,
Python 3.12, Node 18, Playwright 1.56.1 browsers), container limited to `--cpus=4 --memory=8g`, no published ports, results on D:.
Rebuild/run: `tools/docker/build.sh`, `tools/docker/run-tests.sh` (phases), `tools/docker/restart-test.sh`. Other Docker projects were not touched.
Everything below is a CONTAINER result: it does not prove systemd units, reboot persistence, sandbox-under-systemd, or VPS performance.

### Passed in Docker (final code)
- Full suite as a non-root user: **838 passed, 15 skipped** (includes the real nginx-rtmp + FFmpeg end-to-end tests below).
- End-to-end through real nginx-rtmp with the repo's rtmp.conf.template, invoked like production (`python3 -m studio.cli argv|widgets`, real
  `lib/supervise.sh` loop): 720p30 H.264/AAC verified with ffprobe on the live RTMP stream; keyframe gaps <= 2.2 s on HLS segments; clock on the live
  stream advances; encoder health HEALTHY with real CPU/RAM; countdown, ticker, image overlay, video picture-in-picture (frozen-frame check);
  slideshow (pre-render then stream copy, slides advance); audio source + waveform; `kill -9 ffmpeg` auto-recovery; deliberate stop -> STOPPED,
  no restart, no orphan; source disconnect and return; missing media fails closed; corrupt media never crashes the runtime; licence revocation blocks the
  runtime and lifting it restores it; browser gate follows the real sandbox self-test.
- Preview-bridge permissions with REAL accounts (`bluestream-relay`, `bluestream-web`, `www-data`): 9/9 - the console user cannot read media or HLS
  (media root:bluestream-relay 0750/0640, HLS www-data 0750); the bridge as root produces JPEG/PNG previews for media, slideshow image, relay HLS and
  thumbnails; the helper process runs as the owning account, never root; unmanaged paths and symlinks refused; console user running the CLI directly gets nothing.
- Editor in real browsers: 27/27 Playwright tests (Chromium, Firefox, WebKit).
- Real-Chromium browser capture: 21/21 (element/viewport sizes, egress policy blocks an internal address, caching, hang timeout, no orphans, validation; test-only `--no-sandbox`).
- Shell: web-ctl self-test 27/27 (3 skipped need a VPS), `bash -n` + shellcheck (errors) over lib, runners, tools.
- **10-minute benchmark** (4 CPU / 8 GB cap, 720p30, 4000k, veryfast, live composition with clock, ticker, countdown, bar and logo):
  0 restarts, 0 stalls, 0 dropped frames, encoder speed min 1.00x, CPU 0.79 cores median / 0.92 p95 / 0.99 max, RAM max 328 MB,
  17,979 frames = 599 s of video in 600 s. (Docker-on-a-workstation figures; the VPS result is still required.)
- Container hard restart mid-stream (`docker restart`): published project revision 2 and history intact, stale lease/port files from the crash handled,
  stream valid 720p30 again after start (nothing restarts by itself without systemd - that is Gate B).

### Failed in Docker, fixed, regression-tested
- CLI emitted `ffmpeg` as argv[0]; the supervisor already runs `ffmpeg "${FFMPEG_ARGS[@]}"` -> a real Studio relay would have exec'd `ffmpeg ffmpeg ...`.
- Project store dropped an audio project's background + visualizer (published audio projects lost their waveform); store now persists every field automatically.
- Soak harness: scratch dir on a bind mount broke drawtext's atomic reload; padded `speed=   1x` was read as 0.0; frames were not accumulated across restarts.
- Test-suite races found (not product bugs): parallel Playwright workers generated colliding project names; stale nginx worker held the RTMP port; leftover sidecars legitimately tripped admission control.
- Running tests as root hid real permission behaviour; the Docker runner now runs the suite as non-root and permissions as root with real accounts.

### Docker security results vs VPS-only
- Docker-verified: preview bridge permission model (above), path/symlink refusal, SSRF/egress policy in the browser, licence enforcement, ownership flock, auth/CSRF (suite).
- Docker shows Chromium's sandbox CANNOT start under Docker's default seccomp ("No usable sandbox"); the browser source correctly REFUSES in that case (tested).
  Whether it starts on the VPS under the systemd unit is **VPS-only**. Also VPS-only: `IPAddressDeny`/cgroup caps, unit start-up as `bluestream-relay`, AppArmor userns policy,
  boot persistence, real playlist engine for Now Playing, nginx/CloudPanel coexistence, real destinations.

## Automated evidence
Full local run on the (Windows) dev box, round 2: **747 passed, 35 skipped**. Skips are tests that need Linux-only pieces
(FFmpeg, flock, tz database, real Chromium); they run in CI.
CI (ubuntu-24.04) jobs: python (full webapp suite incl. real FFmpeg media tests), shell, node, browser (Playwright
Chromium/Firefox/WebKit + real-Chromium Studio browser-source tests). Latest CI result is recorded at the bottom.

| Area | Where | What it proves |
|---|---|---|
| Planner/model/argv/widgets | test_studio.py | pipeline choice, validation, injection-proof argv, DST/timers |
| Real FFmpeg | test_studio_media.py | 720p30 H.264/AAC, GOP <= 2 s, loop DTS monotonic, copy path, slideshow, audio+waveform, PiP, health listener fed by real FFmpeg, browser-overlay PNG reload |
| Licence/payments | test_studio_license.py | RFC 8032 vector, expiry/grace/revocation/binding, activation, webhook replay + idempotency |
| Security | test_studio_security.py | SSRF (connect time, redirects, rebinding), injection, store hardening, asset types, source hygiene |
| Console | test_studio_console.py | flag, backend licence gate, CSRF, draft/apply/rollback, health + ownership panels, editor server side, preview/asset routes |
| Visual editor | tests/browser/studio-editor.spec.js | drag/resize/keyboard/visibility/layer order/apply/rollback/data fields/mobile layout in 3 engines |
| Now Playing | test_studio_nowplaying.py + test_install.py::test_27 | title tracks playlist advance/restart; unknown -> empty |
| Ownership | test_studio_ownership.py | real flock: second publisher blocked, released on kill -9, takeover order + rollback |
| Health | test_studio_health.py | verdicts (stalled/degraded/healthy), loopback listener, proc sampler |
| Preview access | test_studio_preview.py | identity choice (HLS->nginx, media->relay), root refuses unmanaged paths, bridge image transport + strict decode, routes fall back to the bridge, permission-regression static guards (modes pinned, no chmod 777, web user in no groups, sudoers single command, bridge ops read-only), real FFmpeg frames for every source kind + alpha-preserving thumbnails (CI) |
| Units / accounts | test_studio_units.py | unit account == installer account, run-dir ownership, isolation + caps present, Chromium-breaking directives absent, systemd-analyze syntax check, browser off by default |
| Soak harness | test_studio_soak.py | text files exist before FFmpeg, missing files really break init, short real run reports frames/CPU/RAM, never claims 72 h |
| Browser source | test_studio_browser.py | gate, spec validation, egress policy, resolver pinning, worker backoff/cache/last-good, orphan kill, REAL Chromium captures (CI) |

## Defects found by this round's testing and fixed
- Browser unit named an account (`bluestream`) the installer never creates; now `bluestream-relay`, guarded by test_studio_units.py.
- Soak harness started FFmpeg before its text files existed (and never wrote the ticker file); fixed, proven by test_studio_soak.py (real run in CI).
- web-ctl response line contained NUL bytes (bash drops them) - guard test added.
- Click on a layer scrolled the page away from the canvas mid-drag; summary click re-closed the panel.
- Audio-less source and looped image overlay made renders never end (earlier round) ; browser overlay PNG was not reloaded by `-loop 1` (now `-stream_loop -1`).
- Licence activation would have failed in production (web unit cannot write /etc/bluestream) - licence now in the Studio store.
- Licence bound to an install id was bypassable when no local id existed.
- Project files could be symlinks/oversized; concurrent publishes could clash; shared cache temp names.
- Browser spec: an IP-literal rejection was swallowed by an over-broad except; allowed-host list accepted embedded spaces.
- Image/video/audio/slideshow names were not type-checked.

## Staging attempt (round 5)
Not started: no staging VPS is accessible from this environment (the only known VPS is production and was not touched).
Offline preparation only: `test_studio_preflight.py` (7 tests) proves the preflight refuses a missing marker, the production hostname, CloudPanel,
<4 CPUs, <8 GB, Ubuntu != 24.04, low disk, and a missing snapshot id, and that it never edits SSH/firewall/services.

## NOT run (PENDING - need a real staging VPS) - no claim is made
- Clean install / upgrade / rollback; reboot and `kill -9 ffmpeg` on real systemd; disk full; real destination outage.
- Browser source: Chromium sandbox under Ubuntu 24.04 AppArmor, cgroup limits, `IPAddressDeny` behaviour, real-page CPU/RAM.
- 4-vCPU benchmark (`python3 tools/studio_soak.py --minutes 10 ...`) and the 72-hour soak (`--hours 72`). **Not started.**
- Real playlist -> Now Playing end to end; real health numbers under load.
- Payment provider (not integrated).

## Docker-first deployment (round 7, Docker Desktop only)
See STATUS.md "Docker-first deployment". Evidence: in-container `bluestream-manager selftest` 21 passed / 0 failed (after fixing the missing
`_units` volume for publisher drop-ins on a read-only rootfs); Studio stream via the console HTTP flow at 720p30 H.264/AAC; kill -9 FFmpeg -> recovered;
`docker restart` -> enabled relay/console/nginx active; key-leak scan 0 hits (container logs, unit logs, `diag`). UNTESTED: real VPS, real YouTube, Linux
preflight on a real host, schedule timers.

## Commands
- Studio only: `cd webapp && python -m pytest test_studio*.py -q`
- Editor E2E locally: `PYTHON="python studio_server.py" npx playwright test -c tests/browser/playwright.config.js --project=chromium studio-editor`
- Everything: `cd webapp && python -m pytest -q`

## Latest CI result
Final pre-staging commit `5cc56bd`, CI run 44816632783: **python, shell, node, browser all passed**.
Full local run at this commit: **783 passed, 42 skipped** (skips need Linux/FFmpeg/flock/Chromium and run in CI).
A guard test (no `mktemp` in web-ctl) caught my first version of the preview bridge; it now streams to the JSON helper.
Earlier records follow.

Commit `97969dc` on `feature/bluestream-studio` (draft PR #6), CI run 44810021256: **python, shell, node, browser all passed**
(python needed one re-run: the first attempt never reached the tests because the runner's apt mirror stalled while installing FFmpeg).
Full local run on the dev box at this commit: **760 passed, 39 skipped** (skips = Linux/FFmpeg/flock/Chromium tests that run in CI).
Previous record: `a21f5e1`, run 44808318901, all four passed.
(python includes the real-FFmpeg media tests, ownership flock tests and the PNG-reload variant test; browser includes the
Studio editor spec in Chromium, Firefox and WebKit and the real-Chromium browser-source tests).
This is automated CI evidence only. It is not a staging, benchmark, reboot or 72-hour result.
