# TASKS  (I=implemented, V=verified by automated test, U=untested on a real VPS, G=gated off, B=blocked, -=not started)

| Area | Item | State |
|---|---|---|
| Phase 0 | CLAUDE.md + checkpoints; baseline suite green | I,V |
| Planner | direct / pre-render / live with reasons; cache key; LRU eviction; admission control | I,V |
| FFmpeg | live single chain: text, image(+crop), rect, ticker, clock, countdown, count-up, Pomodoro, data, now-playing, video PiP | I,V (real ffmpeg, CI) |
| FFmpeg | pre-render, stream-copy loop, slideshow, silence insertion, loop-boundary DTS | I,V (real ffmpeg, CI) |
| Sources | media loop, relay, playlist, image slideshow (seconds, shuffle), audio file + colour/image background + waveform | I,V |
| Widgets | timers survive restarts (absolute anchors), DST, atomic text files, data overlay formatting | I,V |
| Data | API overlay: URL, JSON path, refresh, format; SSRF-guarded; last good value | I,V |
| Now Playing | real title from the Playlist engine, updates on advance/restart, empty when unknown | I,V (unit + shell) U(real playlist) |
| Previews | real source frame + overlay image thumbnails on a hardened host via read-only bridge ops (no permission widening) | I,V (unit + CI ffmpeg) U(real VPS) |
| Editor | drag, resize, 8 handles, snap, keyboard, touch, crop, layer order, visibility, real source frame, mobile | I,V (Playwright x3 engines) |
| Editor | Save draft, Apply to Live (confirmed single-stream restart), revision rollback (10) | I,V |
| Ownership | one publisher per destination (flock), BLOCKED state, explicit takeover with rollback | I,V (real flock in CI) U(real systemd) |
| Health | FFmpeg progress listener, speed/fps/drops, encoder CPU/RAM, restarts, last failure, HLS state, console panel | I,V U(real VPS numbers) |
| Browser source | CDP capture, selector/region, interval, caching, last-good image, host pinning, per-request egress policy, limits, orphan kill | I,V (real Chromium, CI) **G** |
| Browser source | systemd isolation (cgroup caps, IP deny, sandbox required) | I; U - needs staging; **G** |
| Runtime | relay TYPE=studio, sidecar, retry-forever, boot persistence, Stop stays stopped | I,V (shell) U(real reboot) |
| Commercial | entitlement, grace, revocation, install binding, backend enforcement, activation UI | I,V |
| Commercial | payment provider integration | B (needs provider + credentials); adapter + webhook verifier I,V |
| Support | Help & Feedback local queue (redacted, consented, rate limited) | I,V; central endpoint - |
| Security | review round 2; fixes listed in TESTS.md | I,V; independent review - |
| Ops | installer copies studio/ + browser unit, backup includes projects, soak + licence tools | I; U(staging install) |
| Docker validation | real nginx-rtmp + FFmpeg e2e, real-account permissions, editor x3 engines, 10-min benchmark, container-restart recovery | I,V (Docker) |
| Docker-first deployment | docker/ image + Compose + bluestream.sh preflight/up/down/diag, bsunitd, DOCKER.md | I,V (Docker Desktop); U (real VPS, real YouTube, Linux preflight) |
| Release | staging, failure injection on systemd, 4-vCPU benchmark, 72 h soak | B/PENDING |
