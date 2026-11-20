# TESTS

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
| Browser source | test_studio_browser.py | gate, spec validation, egress policy, resolver pinning, worker backoff/cache/last-good, orphan kill, REAL Chromium captures (CI) |

## Defects found by this round's testing and fixed
- web-ctl response line contained NUL bytes (bash drops them) - guard test added.
- Click on a layer scrolled the page away from the canvas mid-drag; summary click re-closed the panel.
- Audio-less source and looped image overlay made renders never end (earlier round) ; browser overlay PNG was not reloaded by `-loop 1` (now `-stream_loop -1`).
- Licence activation would have failed in production (web unit cannot write /etc/bluestream) - licence now in the Studio store.
- Licence bound to an install id was bypassable when no local id existed.
- Project files could be symlinks/oversized; concurrent publishes could clash; shared cache temp names.
- Browser spec: an IP-literal rejection was swallowed by an over-broad except; allowed-host list accepted embedded spaces.
- Image/video/audio/slideshow names were not type-checked.

## NOT run (PENDING - need a real staging VPS) - no claim is made
- Clean install / upgrade / rollback; reboot and `kill -9 ffmpeg` on real systemd; disk full; real destination outage.
- Browser source: Chromium sandbox under Ubuntu 24.04 AppArmor, cgroup limits, `IPAddressDeny` behaviour, real-page CPU/RAM.
- 4-vCPU benchmark (`python3 tools/studio_soak.py --minutes 10 ...`) and the 72-hour soak (`--hours 72`). **Not started.**
- Real playlist -> Now Playing end to end; real health numbers under load.
- Payment provider (not integrated).

## Commands
- Studio only: `cd webapp && python -m pytest test_studio*.py -q`
- Editor E2E locally: `PYTHON="python studio_server.py" npx playwright test -c tests/browser/playwright.config.js --project=chromium studio-editor`
- Everything: `cd webapp && python -m pytest -q`

## Latest CI result
Commit `a21f5e1` on `feature/bluestream-studio` (draft PR #6), CI run 44808318901: **python, shell, node, browser all passed**
(python includes the real-FFmpeg media tests, ownership flock tests and the PNG-reload variant test; browser includes the
Studio editor spec in Chromium, Firefox and WebKit and the real-Chromium browser-source tests).
This is automated CI evidence only. It is not a staging, benchmark, reboot or 72-hour result.
