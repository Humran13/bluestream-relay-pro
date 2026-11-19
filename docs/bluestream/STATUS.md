# STATUS (update after every milestone)

Branch `feature/bluestream-studio` (draft PR #6, NOT merged, NOT deployed). Base: main `dbfb71b`.
Label: **implementation complete for the V1 candidate scope below; release gate PENDING.**
Do NOT call this beta-ready: Gates B (staging), C (benchmarks/72h), D (payments), E (security sign-off) in
`RELEASE_CHECKLIST.md` are open.

## Implemented and tested
- Core: model, planner (copy / pre-render / live), admission control, argv builders (live, pre-render, copy, slideshow)
- Widgets: clock, countdown, count-up, Pomodoro (absolute anchors, DST-correct), ticker, Now Playing (file-fed), API data (SSRF-guarded)
- Runtime: relay TYPE=studio via existing run-relay.sh/supervise.sh; sidecar for live text; cache + eviction; live leases; progress-based health helpers
- Console (flag-gated): home/licence, create from templates, overlay editor + preview, save draft, Apply to Live (confirmed restart of one stream), rollback, create stream, Help & Feedback
- Licensing: Ed25519 offline entitlements, grace, revocation, backend enforcement (routes + runtime); payments adapter (NOT live)
- Ops: installer copies studio/, backup includes projects, soak harness, vendor licence tool
- Tests: ~140 new Python tests + 3 shell-wiring tests; real-FFmpeg tests run in CI only

## NOT done (honest)
- Staging VPS validation, upgrade/rollback rehearsal, failure injection on real systemd, reboot simulation
- 4-vCPU benchmark and 72-hour soak (harness ready: `tools/studio_soak.py`)
- Payments provider integration; vendor key issuance; legal docs
- Browser-capture source (deliberately gated off); Now Playing producer in the Playlist engine; playlist scheduling additions
- Per-destination exclusivity across *different* streams (design: one publisher unit per (stream,destination); verify on staging)

## Next actions (for the owner)
1. Review PR #6, run the checklist on a staging VPS, run the 72h soak.
2. Decide payment provider; generate the vendor key.
