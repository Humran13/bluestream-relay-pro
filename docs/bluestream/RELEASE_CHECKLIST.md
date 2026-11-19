# Studio beta release checklist

Legend: [x] verified with evidence, [ ] not done / pending, [~] partial.
Nothing below is deployed anywhere. Production deploys are owner-approved only.

## Gate A - code & automated tests
- [x] Studio core unit tests (planner, model, store, widgets, argv builder, licence, payments, SSRF, console) - `webapp/test_studio*.py`
- [x] Real FFmpeg media tests (profile, GOP, loop-boundary DTS, silence insertion, copy path, slideshow) - run in CI (`test_studio_media.py`); skipped on machines without ffmpeg
- [x] Existing Relay/Playlist/Publisher regression suite unchanged and green (CI python/shell/node/browser)
- [x] Studio endpoints do not exist unless `BLUESTREAM_STUDIO_ENABLED=1`
- [x] Backend refuses Studio start/mutations without a valid licence

## Gate B - staging VPS (NOT DONE: no staging machine/credentials were available)
- [ ] Clean install on Ubuntu 24.04 staging, then upgrade-in-place from the previous release
- [ ] Backup -> restore -> rollback rehearsal (`bluestream-manager` backup includes `/var/lib/bluestream/studio`)
- [ ] Start/stop/reboot on staging: deliberate Stop stays stopped after reboot; Start survives reboot
- [ ] Failure injection on staging: kill -9 ffmpeg, corrupt media, delete media mid-run, fill disk, drop destination, `systemctl reboot`
- [ ] Close the browser and the editing PC: stream continues
- [ ] nginx-rtmp, HLS and an attached (test) destination work end-to-end with a Studio stream

## Gate C - performance (NOT DONE)
- [ ] `python3 tools/studio_soak.py --minutes 10 --out /var/tmp/studio-quick` on the target 4 vCPU / 8 GB class VPS; record in TESTS.md
- [ ] **72-hour** run: `python3 tools/studio_soak.py --hours 72 --out /var/tmp/studio-soak` -> `verdict.json` gate must read `72h-PASS-CANDIDATE`
      (speed >= 1.0x sustained, 0 restarts, 0 stalls; CPU target ~2-2.5 cores). **Status: PENDING.**
- [ ] If 720p30 misses the budget: benchmark 480p / lower preset and enforce honest limits before marketing

## Gate D - commercial
- [x] Signed, expiring, revocable entitlement with grace; backend enforcement; activation UI
- [ ] Payment provider: **not integrated** (adapter + verified-webhook plumbing only) - see COMMERCIAL.md
- [ ] Vendor key generated and public key shipped in the release package
- [ ] Terms / privacy / refund policy / tax handling written by the owner

## Gate E - security review
- [x] Argv-only FFmpeg, local-bus-only URLs, path-traversal and injection tests, SSRF guard (connect-time, redirects, rebinding)
- [x] Feedback redaction + rate limit + consent
- [ ] Independent review of: pure-Python Ed25519 (non-constant-time sign is vendor-only), web unit's new ReadWritePaths entry
- [ ] Dependency/vulnerability scan on the release package

## Gate F - docs
- [x] docs/studio.md (user), COMMERCIAL.md (vendor), this checklist, STATUS/TASKS/TESTS
- [ ] Screenshots / beginner walkthrough video

## Deployment (owner-approved, later; NOT performed)
1. Merge PR after Gates B-E; tag release.
2. On a **staging** VPS first: `git pull`, run the normal installer update; it copies `studio/` and creates `/var/lib/bluestream/studio`.
3. The web console unit template gained `-/var/lib/bluestream/studio` in `ReadWritePaths`: re-run the installer's web-console step so the unit is regenerated, then restart **only** `bluestream-web`.
4. Install the vendor public key file, set `BLUESTREAM_STUDIO_ENABLED=1` for the web unit, restart `bluestream-web` only.
5. Existing streams are never restarted by this change.
6. Rollback: restore the config backup, remove the `-/var/lib/bluestream/studio` entry / env flag, restart `bluestream-web`; Relay/Playlist are unaffected throughout.
