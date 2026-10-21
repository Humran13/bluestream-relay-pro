# Changelog

All notable changes to BlueStream Relay Pro are documented in this file.

The format is based on Keep a Changelog and this project adheres to
Semantic Versioning.

## [Unreleased]

### Fixed

- The web console's status snapshot (`web-ctl snapshot`, feeds the
  dashboard's "Nginx" health field) hard-coded `systemctl is-active nginx`,
  which checks the hosting panel's own nginx instead of BlueStream's when
  running in isolated mode. It now goes through the existing
  strategy-aware `nginx_status`/`nginx_running` helpers (`lib/nginx.sh`),
  which `web-ctl` now loads.
- `uninstall.sh` reloaded the system/panel nginx and removed owned-mode-only
  files unconditionally, even on an isolated-mode install where BlueStream
  never touched the system nginx in the first place. Both are now gated to
  owned mode only.

### Added

- `tests/integration/crash-isolation.sh`: a real-service (not mocked)
  integration test proving BlueStream's own streaming engine can be
  repeatedly crashed (SIGSEGV) without affecting a pre-existing web
  server/control panel on the same host, without an uncontrolled restart
  storm, and without an invalid config ever replacing a working one. See
  `docs/troubleshooting.md` "An nginx worker crashed / repeated RTMP or
  HLS segfaults" for how/when to run it and how to investigate a real
  `ngx_rtmp_module` crash on a production host.

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
