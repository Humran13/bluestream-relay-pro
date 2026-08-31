#!/usr/bin/env bash
# BlueStream Relay Pro - systemd runtime wrapper for a playlist.
#
# Root bootstrap: reads the root-only playlist config, normalizes every entry
# into the managed playlist cache (BLUESTREAM_PLAYLIST_CACHE_DIR, content-
# addressed, unchanged media reused), writes the concat file that lists ONLY
# the prepared baseline artifacts, prepares the HLS output directory, then
# permanently drops privileges with setpriv before exec'ing FFmpeg. Fails
# closed if preparation fails or the privilege drop cannot be performed.
# FFmpeg never runs as root.
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

# nginx-rtmp (www-data) owns all HLS output; FFmpeg publishes over the
# private local RTMP socket and never writes the HLS filesystem directly.
mkdir -p "$RUN_DIR" || exit 1

# Managed playlist normalization cache. Defensive create + ownership fix so an
# upgraded host that never re-ran the installer still fails safely: the
# wrapper must be able to write artifacts as root, and the dropped-privilege
# FFmpeg concat process must be able to read them (root:bluestream-relay
# 0640, directory root:bluestream-relay 0750).
mkdir -p "$BLUESTREAM_PLAYLIST_CACHE_DIR" || exit 1
chown root:"$BLUESTREAM_GROUP" "$BLUESTREAM_PLAYLIST_CACHE_DIR" 2>/dev/null || true
chmod 0750 "$BLUESTREAM_PLAYLIST_CACHE_DIR" || exit 1

# Preparation marker: while normalization runs inside this root bootstrap,
# health reports STARTING instead of STALE (see lib/health.sh). Removed right
# before exec'ing FFmpeg, when preparation is complete.
: > "$RUN_DIR/$NAME.prepare" || exit 1
chown root:root "$RUN_DIR/$NAME.prepare" 2>/dev/null || true
chmod 0600 "$RUN_DIR/$NAME.prepare" || exit 1

# Prepare every entry into the managed normalization cache (content-addressed,
# so unchanged media is reused). This is the authoritative media/cache
# resolution boundary: only validated playlist basenames reach it, and any
# entry that cannot be decoded/prepared fails the playlist closed - no partial
# concat is ever written and the unit never claims HEALTHY.
playlist_cache_prune || true
playlist_prepare_all || { rm -f "$RUN_DIR/$NAME.prepare"; exit 1; }

# Write the concat file (readable by bluestream-relay after the drop). It
# lists ONLY the prepared baseline artifacts.
playlist_write_concat_file || { rm -f "$RUN_DIR/$NAME.prepare"; exit 1; }

# Fail closed: privilege drop prerequisites.
id "$BLUESTREAM_USER" >/dev/null 2>&1 || { rm -f "$RUN_DIR/$NAME.prepare"; exit 1; }
command -v setpriv >/dev/null 2>&1 || { rm -f "$RUN_DIR/$NAME.prepare"; exit 1; }
command -v ffmpeg >/dev/null 2>&1 || { rm -f "$RUN_DIR/$NAME.prepare"; exit 1; }

playlist_build_ffmpeg_args || { rm -f "$RUN_DIR/$NAME.prepare"; exit 1; }

# Fail closed if the argv is structurally invalid (e.g. codec args collapsed
# so that 'copy' could be parsed as a positional output filename).
if ! bs_verify_ffmpeg_args; then
    bs_error "Refusing to exec FFmpeg: malformed argument array for playlist '$NAME'."
    bs_dump_ffmpeg_args
    rm -f "$RUN_DIR/$NAME.prepare"
    exit 1
fi
if [ "${BS_DEBUG_ARGS:-0}" = "1" ]; then
    bs_info "Sanitized FFmpeg argv for playlist '$NAME':"
    bs_dump_ffmpeg_args
fi

# Preparation is complete: HLS output begins once FFmpeg publishes. Clear the
# marker so health transitions to HEALTHY/STARTING as normal.
rm -f "$RUN_DIR/$NAME.prepare" 2>/dev/null || true

# Final umask so HLS segments are 0640 (group-readable by nginx).
umask 0027

exec setpriv --reuid "$BLUESTREAM_USER" --regid "$BLUESTREAM_GROUP" \
    --init-groups --inh-caps=-all --no-new-privs ffmpeg "${FFMPEG_ARGS[@]}"
