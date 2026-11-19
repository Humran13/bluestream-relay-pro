# TASKS  (I=implemented V=verified B=blocked U=untested -=not started)

| Area | Item | State |
|---|---|---|
| Phase 0 | CLAUDE.md + checkpoints | I,V |
| Phase 0 | Baseline suite (CI green on main) | V |
| Phase 0 | CPU/codec microbenchmark | B (no ffmpeg here) |
| Planner | direct / prerender / live choice + reasons | I,V(unit) |
| Planner | cache key; eviction/cleanup/disk limits | key I,V; eviction - |
| Planner | admission control | I,V(unit) |
| FFmpeg | live single-chain argv (text/image/rect/ticker/clock/countdown) | I,U(no real ffmpeg run) |
| Widgets | countdown/clock/pomodoro/atomic write | I,V(unit; tz test skipped locally) |
| Runtime | systemd unit, supervisor, desired-state, ownership lease | - |
| Runtime | health from ffmpeg progress/frame age | - |
| UI | Studio tab, draft/apply, templates, preview | - |
| Inputs | slideshow, Now Playing, smart data, browser source (gated) | - |
| Playlist automation | loop modes, schedule | - |
| Help & Feedback | forms, sanitized bundle | - |
| Commercial | entitlements, license UX, payment adapter | - |
| Security | SSRF at connect time, upload safety, log redaction | - (argv/path validation only) |
| Release gate | regression, failure injection, 4-vCPU bench, 72h | B/PENDING |
