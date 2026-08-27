# Changelog

All notable changes to BlueStream Relay Pro are documented in this file.

The format is based on Keep a Changelog and this project adheres to
Semantic Versioning.

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
