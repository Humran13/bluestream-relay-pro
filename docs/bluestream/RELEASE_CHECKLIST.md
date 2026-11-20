# Studio beta release checklist

Legend: [x] verified with evidence, [ ] not done / pending, [~] partial.
Nothing below is deployed anywhere. Production deploys are owner-approved only. **Not beta-ready.**

## Gate A - code & automated tests
- [x] Studio unit/integration tests (planner, model, store, widgets, argv, licence, payments, SSRF, console, ownership, health, now-playing)
- [x] Real FFmpeg media tests in CI (profile, GOP, loop boundaries, silence, copy path, slideshow, audio + waveform, PiP, progress listener)
- [x] Visual editor Playwright tests in Chromium, Firefox, WebKit (CI)
- [x] Real-Chromium browser-source tests in CI (test-only `--no-sandbox`)
- [x] Existing Relay/Playlist/Publisher regression suite unchanged and green
- [x] Studio endpoints do not exist unless `BLUESTREAM_STUDIO_ENABLED=1`; backend refuses Studio start/mutations without a valid licence
- [x] Browser unit account matches the installer's (`bluestream-relay`); soak harness initialisation proven by test
- [x] Browser source refuses to run unless `BLUESTREAM_STUDIO_BROWSER=1` + Chromium + working sandbox (also enforced in the runtime against crafted project files)

## Gate B - staging VPS (NOT DONE: no staging machine/credentials available)
- [ ] Clean install on Ubuntu 24.04, then upgrade-in-place from the previous release
- [ ] Backup -> restore -> rollback rehearsal (backup includes `/var/lib/bluestream/studio`)
- [ ] Start/stop/reboot: deliberate Stop stays stopped after reboot; Start survives reboot
- [ ] Failure injection: `kill -9 ffmpeg`, corrupt media, delete media mid-run, fill disk, drop destination, `systemctl reboot`
- [ ] Close the browser and the editing PC: stream continues
- [ ] Studio stream end to end with nginx-rtmp, HLS and a TEST destination
- [ ] Ownership: attach one destination to a Relay and a Studio stream; second shows BLOCKED; takeover switches and can be reversed; `kill -9` of the owner frees it
- [ ] Now Playing against a real playlist (title changes as items advance; restart resets)
- [x] Preview design: read-only bridge ops, no permission change (tests pin modes; see STATUS.md)
- [ ] Previews verified on the VPS for video, image overlay, slideshow, Relay and Playlist, and after upgrade/upload/import/backup-restore (STAGING.md section 1)
- [ ] `systemctl start bluestream-browser@<relay>` as `bluestream-relay`: unit active, Chromium sandbox starts, capture PNG appears in the run dir
- [ ] Web unit regenerated with `-/var/lib/bluestream/studio` in `ReadWritePaths`; licence activation works from the console

Full step-by-step procedure for Ubuntu 24.04 / 4 vCPU / 8 GB: `docs/bluestream/STAGING.md`.

## Gate C - performance (NOT DONE)
- [ ] `python3 tools/studio_soak.py --minutes 10 --out /var/tmp/studio-quick` on the target 4 vCPU / 8 GB VPS; record in TESTS.md
- [ ] **72-hour** run: `python3 tools/studio_soak.py --hours 72 --out /var/tmp/studio-soak`; gate = `72h-PASS-CANDIDATE`
      (speed >= 1.0x sustained, 0 restarts, 0 stalls; CPU target ~2-2.5 cores). **Status: PENDING.**
- [ ] If 720p30 misses the budget: benchmark lower profiles and enforce honest limits before marketing

## Gate D - commercial
- [x] Signed, expiring, revocable entitlement with grace, install binding, backend enforcement, activation UI (manual licensing for controlled tests)
- [ ] Payment provider: **not integrated** - adapter + verified-webhook plumbing only; needs provider, credentials, refunds, tax/legal
- [ ] Vendor key generated and public key shipped in the release package
- [ ] Terms / privacy / refund policy written by the owner

## Gate E - security review
- [x] argv-only FFmpeg, local-bus-only URLs, path/injection tests, SSRF guard, feedback redaction, store hardening, flock ownership
- [ ] Browser source on a real host: sandbox starts under AppArmor policy; `IPAddressDeny` blocks 127/8, RFC1918, 169.254/16 from the worker; memory/CPU caps hold; no orphaned processes after unit stop
- [ ] Independent review of: pure-Python Ed25519, web unit `ReadWritePaths`, publisher unit `ReadWritePaths=/var/lib/bluestream/run`, browser unit
- [ ] Dependency/vulnerability scan on the release package
- [ ] **Only after the browser items above pass**: document `BLUESTREAM_STUDIO_BROWSER=1` as supported; until then it stays unsupported/off

## Gate F - docs
- [x] docs/studio.md (user), COMMERCIAL.md (vendor), this checklist, STATUS/TASKS/TESTS
- [ ] Screenshots / beginner walkthrough

## Deployment (owner-approved, later; NOT performed)
1. Merge PR after Gates B-E; tag release.
2. **Staging first**: pull, run the normal installer update. It copies `studio/` and the browser unit and creates `/var/lib/bluestream/studio`.
3. Re-run the installer's web-console step (unit gains `ReadWritePaths` entry and the publisher unit gains the run dir), then restart **only** `bluestream-web`. Existing streams are not restarted; publishers pick up the new unit text on their next start.
4. Install the vendor public key file, set `BLUESTREAM_STUDIO_ENABLED=1` via a `bluestream-web` drop-in, restart `bluestream-web` only.
5. Rollback: restore the config backup, remove the drop-in/flag, restart `bluestream-web`; Relay/Playlist are unaffected throughout.
