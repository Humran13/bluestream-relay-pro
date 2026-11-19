# TASKS  (I=implemented, V=verified by automated test, U=untested/needs real VPS, B=blocked, -=not started)

| Area | Item | State |
|---|---|---|
| Phase 0 | CLAUDE.md + checkpoints | I,V |
| Phase 0 | Baseline suite green on main (CI) | V |
| Phase 0 | CPU/codec microbenchmark | harness I; run on VPS = B |
| Planner | direct / pre-render / live choice with reasons | I,V |
| Planner | cache key, LRU eviction, stale temp cleanup, disk-free check | I,V(unit) |
| Planner | admission control (live slots, host load) | I,V |
| FFmpeg | live single-chain argv (text/image/rect/ticker/clock/countdown/data) | I,V(CI real ffmpeg) |
| FFmpeg | pre-render, copy, slideshow, silence insertion, loop-boundary DTS | I,V(CI real ffmpeg) |
| Widgets | clock/countdown/count-up/Pomodoro/DST/atomic write/restart-safe | I,V (tz tests need tzdata: run in CI) |
| Widgets | API data overlay (SSRF-guarded, last-good-value) | I,V(mocked network) |
| Widgets | Now Playing | I (file-fed); producer in Playlist engine - |
| Runtime | relay TYPE=studio via run-relay.sh, sidecar, retry-forever | I,V(shell wiring tests) U(real systemd) |
| Runtime | Stop stays stopped after reboot; Start enables boot | I,V(shell tests) U(real reboot) |
| Runtime | one publisher per stream (no second publish path) | I (by design) U(staging) |
| Runtime | health from FFmpeg progress | helper I,V; surfaced in UI - |
| UI | Studio tab, templates, overlay editor, preview, Apply to Live, rollback | I,V(console tests) U(browser E2E) |
| UI | confirmed single-stream restart on Apply | I,V |
| UI | undo/redo | rollback to last 10 revisions only |
| Inputs | image slideshow | I,V(CI) |
| Inputs | browser-capture source | gated OFF by design (not implemented) |
| Playlist automation | loop/random/schedule | existing Playlist feature (unchanged); Studio adds none |
| Help & Feedback | local redacted queue, consent, rate limit, diagnostics opt-in | I,V; central endpoint - |
| Commercial | entitlement, grace, revocation, backend enforcement, activation UI | I,V |
| Commercial | payment adapter / webhook verifier / idempotency | I,V; provider live = B |
| Security | argv-only, path traversal, injection, SSRF connect-time | I,V |
| Security | upload parser safety for images/video | existing media-import checks; extra - |
| Ops | installer copies studio/, backup includes projects, vendor tool, soak tool | I; U(staging install) |
| Release | regression, failure injection on VPS, 4-vCPU bench, 72h | B/PENDING |
