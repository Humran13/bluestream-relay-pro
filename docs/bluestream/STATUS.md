# STATUS (update after every milestone)

Last verified: branch `feature/bluestream-studio` on top of main `dbfb71b` (CI green on main at 214adf1 + Streams UI commit).

## Works (unit-tested, pure Python, no ffmpeg needed)
- `webapp/studio/model.py` validated project/overlay/profile model
- `webapp/studio/planner.py` DIRECT / PRERENDER / LIVE planner, cache key, admission control
- `webapp/studio/ffmpeg.py` single-chain live-composition argv builder (local-bus only, injection-safe)
- `webapp/studio/widgets.py` countdown/clock/pomodoro text + atomic file write
- `webapp/test_studio.py`: 21 pass, 1 skip (tzdata missing on this Windows box)

## Current task
Phase 1 core integration: Studio runtime (systemd unit + wrapper reusing local RTMP bus so existing publishers/HLS work unchanged), persisted project store, ownership lease.

## Next three actions
1. Validate generated argv against real FFmpeg on Linux (CI job or WSL): 720p30 output, GOP, loop boundary. (UNTESTED)
2. `lib/studio.sh` + `config/systemd/bluestream-studio@.service` + `run-studio.sh` (supervise.sh, desired-state persisted, Stop stays stopped).
3. `web-ctl` ops + `engine.py` allowlist + console Studio tab (behind feature flag).

## Blockers / honesty
- No ffmpeg, Docker daemon, or tzdata on this dev machine -> media/benchmark tests PENDING.
- VPS SSH details never provided; no staging box. 72h test and 4-vCPU benchmark PENDING.
- Payments/licensing: not started; no provider/credentials.
