#!/usr/bin/env bash
# BlueStream Relay Pro - systemd runtime wrapper for a playlist.
#
# Root bootstrap: reads the root-only playlist config, writes the concat
# file, prepares the HLS output directory, then permanently drops
# privileges with setpriv before exec'ing FFmpeg. Fails closed if the
# privilege drop cannot be performed. FFmpeg never runs as root.
#
# Installed to /usr/local/lib/bluestream/run-playlist.sh
set -u
shopt -s nullglob

NAME="${1:-}"
case "$NAME" in
    *[!a-z0-9_-]*|""|-*|_*) exit 1 ;;
esac

LIBDIR="/usr/local/lib/bluestream/lib"
CONF_DIR="/etc/bluestream/playlists"
HLS_ROOT="/var/www/bluestream/hls/playlist"
RUN_DIR="/var/lib/bluestream/run"

[ -f "$LIBDIR/common.sh" ] || exit 1
# shellcheck source=lib/common.sh
source "$LIBDIR/common.sh" || exit 1
# shellcheck source=lib/probe.sh
source "$LIBDIR/probe.sh" || exit 1
# shellcheck source=lib/playlist.sh
source "$LIBDIR/playlist.sh" || exit 1

bs_detect_install
bs_load_server_conf

playlist_load_config "$NAME" || exit 1
[ "${#PLAYLIST_FILES[@]}" -gt 0 ] || exit 1

# Root bootstrap: prepare the HLS output directory and the run directory.
mkdir -p "$HLS_ROOT/$NAME" || exit 1
chown "${BLUESTREAM_USER}:${BLUESTREAM_NGINX_USER}" "$HLS_ROOT/$NAME" || exit 1
chmod 0750 "$HLS_ROOT/$NAME" || exit 1
mkdir -p "$RUN_DIR" || exit 1

# Write the concat file (readable by bluestream-relay after the drop).
playlist_write_concat_file || exit 1

# Fail closed: privilege drop prerequisites.
id "$BLUESTREAM_USER" >/dev/null 2>&1 || exit 1
command -v setpriv >/dev/null 2>&1 || exit 1
command -v ffmpeg >/dev/null 2>&1 || exit 1

playlist_build_ffmpeg_args || exit 1

# Final umask so HLS segments are 0640 (group-readable by nginx).
umask 0027

exec setpriv --reuid "$BLUESTREAM_USER" --regid "$BLUESTREAM_GROUP" \
    --init-groups --inh-caps=-all --no-new-privs ffmpeg "${FFMPEG_ARGS[@]}"
