#!/usr/bin/env bash
# BlueStream Relay Pro - systemd runtime wrapper for a single relay.
#
# Root bootstrap: reads the root-only relay config, prepares the HLS output
# directory, then permanently drops privileges with setpriv before exec'ing
# FFmpeg. Fails closed if the privilege drop cannot be performed. FFmpeg
# never runs as root.
#
# Installed to /usr/local/lib/bluestream/run-relay.sh
set -u
shopt -s nullglob

NAME="${1:-}"
case "$NAME" in
    *[!a-z0-9_-]*|""|-*|_*) exit 1 ;;
esac

LIBDIR="/usr/local/lib/bluestream/lib"
CONF_DIR="/etc/bluestream/relays"
HLS_ROOT="/var/www/bluestream/hls/relay"

[ -f "$LIBDIR/common.sh" ] || exit 1
# shellcheck source=lib/common.sh
source "$LIBDIR/common.sh" || exit 1
# shellcheck source=lib/probe.sh
source "$LIBDIR/probe.sh" || exit 1
# shellcheck source=lib/relay.sh
source "$LIBDIR/relay.sh" || exit 1

bs_detect_install
bs_load_server_conf

relay_load_config "$NAME" || exit 1
relay_config_validate || exit 1

# Root bootstrap: ensure the HLS output directory exists and is owned by
# bluestream-relay:www-data (readable by nginx, writable by ffmpeg).
mkdir -p "$HLS_ROOT/$NAME" || exit 1
chown "${BLUESTREAM_USER}:${BLUESTREAM_NGINX_USER}" "$HLS_ROOT/$NAME" || exit 1
chmod 0750 "$HLS_ROOT/$NAME" || exit 1

# Fail closed: privilege drop prerequisites.
id "$BLUESTREAM_USER" >/dev/null 2>&1 || exit 1
command -v setpriv >/dev/null 2>&1 || exit 1
command -v ffmpeg >/dev/null 2>&1 || exit 1

relay_build_ffmpeg_args || exit 1

# Final umask so HLS segments are 0640 (group-readable by nginx).
umask 0027

exec setpriv --reuid "$BLUESTREAM_USER" --regid "$BLUESTREAM_GROUP" \
    --init-groups --inh-caps=-all --no-new-privs ffmpeg "${FFMPEG_ARGS[@]}"
