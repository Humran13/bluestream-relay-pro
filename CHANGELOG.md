# Changelog

All notable changes to BlueStream Relay Pro are documented in this file.

The format is based on Keep a Changelog and this project adheres to
Semantic Versioning.

## [Unreleased]

### Added - BlueStream Studio (beta candidate, opt-in, Premium)

- New relay type `studio`: a Studio project publishes into the existing private
  local RTMP bus, so destinations, HLS, Start/Stop, boot persistence and health are
  unchanged and there is still exactly one publisher per stream.
- Automatic pipeline planner: direct stream copy, pre-render-then-copy (static
  overlays, slideshows, normalisation) or one live x264 composition (clock,
  countdown/count-up, Pomodoro, ticker, Now Playing, API data). Admission control
  refuses an extra live encode instead of degrading silently.
- Studio console (behind `BLUESTREAM_STUDIO_ENABLED=1`): templates, overlay editor,
  draft vs **Apply to Live** with confirmed single-stream restart, revision rollback.
- Signed (Ed25519), expiring, revocable entitlements with offline verification and a
  bounded grace period, enforced in every Studio endpoint **and** the runtime.
  Payment-provider adapter + verified-webhook plumbing (NOT live).
- SSRF-guarded data overlays (connect-time IP validation, redirect re-validation).
- Help & Feedback: local, redacted, rate-limited report queue.
- `tools/studio_soak.py` (benchmark / 72-hour soak harness) and `tools/studio_license.py` (vendor).
- Studio projects are included in config backups.

### Changed

- The relay FFmpeg-argv verifier accepts `libx264`/`aac` only for Studio relays
  (`BS_ALLOW_ENCODE=1`); every other relay type still requires stream copy.
- The web console unit template gains `-/var/lib/bluestream/studio` in `ReadWritePaths`.

### Fixed

- Added a backend-enforced **24/7 Continuous Streaming** mode for uploaded
  local videos. New local-video streams default to the mode but remain stopped
  until Start is pressed; Start applies loop, indefinite crash recovery and
  reboot persistence.
- Added stopped-only editing and clear Streams-page status for existing local
  videos. Disabling the mode intentionally restores one-time playback and
  removes boot persistence without changing names, media, destinations, keys
  or HLS URLs.
- Kept attached destination publishers restartable for the lifetime of a 24/7
  local-video source, including recovery gaps, while preserving explicit Stop
  semantics and leaving unrelated source types unchanged.

## [0.1.0] - 2026-08-28

Foundation release.

### Added

- Project skeleton: manager, installer, uninstaller, status script, library
  modules, systemd templates, nginx templates, web player and documentation.
- Source types: local file, local playlist, remote HLS/M3U8, RTMP, RTMPS,
  RTSP, and direct HTTP/HTTPS media files.
- Stream-copy philosophy: compatible sources are remuxed with `-c:v copy`
  and `-c:a copy`; no automatic transcoding.
- ffprobe-based source inspection with codec compatibility reporting.
- systemd supervision for relays and playlists with restart/backoff
  behaviour and hardening options.
- Privilege model: root-only configuration, dedicated `bluestream-relay`
  system user, `setpriv` privilege drop before FFmpeg starts.
- HLS output under `/var/www/bluestream/hls` served by nginx with correct
  MIME types, cache headers and CORS.
- Bundled hls.js web player (`.player/?relay=<name>` and
  `.player/?playlist=<name>`).
- Playlist management: create, list, add/remove/reorder, start/stop/restart,
  boot enable/disable, health check and compatibility warnings.
- Media management: safe import/list/inspect/remove of managed media.
- Backup/restore of relay and playlist definitions and server configuration.
- Diagnostics checklist and end-to-end self-test.
- Bandwidth estimator.
- Documentation: installation, sources, playlists, WordPress integration,
  troubleshooting and security model.
