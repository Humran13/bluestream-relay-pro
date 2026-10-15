#!/usr/bin/env bash
# BlueStream Relay Pro - systemd runtime wrapper for one outbound publisher.
#
# Root bootstrap: parses its own validated instance id ("<kind>:<target>:
# <destid>"), confirms the destination is still enabled AND still attached to
# the named relay/playlist (closes the race where a detach/disable happens
# while this unit is mid-restart), loads the root-only destination config
# (stream key), builds the FFmpeg argv, then permanently drops privileges with
# setpriv before exec'ing FFmpeg. Fails closed at every step. FFmpeg never
# runs as root.
#
# Installed to /usr/local/lib/bluestream/run-publisher.sh
set -u
shopt -s nullglob

INSTANCE="${1:-}"

LIBDIR="/usr/local/lib/bluestream/lib"

[ -f "$LIBDIR/common.sh" ] || exit 1
# shellcheck source=lib/common.sh
source "$LIBDIR/common.sh" || exit 1
# shellcheck source=lib/destinations.sh
source "$LIBDIR/destinations.sh" || exit 1
# shellcheck source=lib/relay.sh
source "$LIBDIR/relay.sh" || exit 1
# shellcheck source=lib/playlist.sh
source "$LIBDIR/playlist.sh" || exit 1
# shellcheck source=lib/publisher.sh
source "$LIBDIR/publisher.sh" || exit 1

bs_detect_install
bs_load_server_conf

publisher_parse_instance "$INSTANCE" || exit 1

# Fail closed: the destination must still exist and be enabled, and must
# still be attached to the referencing relay/playlist. A stale unit/drop-in
# (left over from a detach/delete race, or a hand-edited config) never execs
# FFmpeg - it exits immediately and systemd will not usefully keep retrying
# since the next publisher_sync_target call (every start of the source unit)
# removes the drop-in and stops the unit outright.
dest_load_config "$PUB_DEST" || exit 1
[ "$DEST_ENABLED" = "yes" ] || exit 1

CSV=""
case "$PUB_KIND" in
    relay)
        relay_load_config "$PUB_TARGET" || exit 1
        CSV="${RELAY_DESTINATIONS:-}"
        ;;
    playlist)
        playlist_load_config "$PUB_TARGET" || exit 1
        CSV="${PLAYLIST_DESTINATIONS:-}"
        ;;
    *) exit 1 ;;
esac
case ",$(printf '%s' "$CSV" | tr -d '[:space:]')," in
    *",$PUB_DEST,"*) ;;
    *) exit 1 ;;
esac

# Fail closed: privilege drop prerequisites.
id "$BLUESTREAM_USER" >/dev/null 2>&1 || exit 1
command -v setpriv >/dev/null 2>&1 || exit 1
command -v ffmpeg >/dev/null 2>&1 || exit 1

publisher_build_ffmpeg_args "$PUB_KIND" "$PUB_TARGET" "$PUB_DEST" || exit 1

if ! publisher_verify_ffmpeg_args; then
    bs_error "Refusing to exec FFmpeg: malformed publisher argument array for '$INSTANCE'."
    publisher_dump_ffmpeg_args
    exit 1
fi
if [ "${BS_DEBUG_ARGS:-0}" = "1" ]; then
    bs_info "Sanitized FFmpeg argv for publisher '$INSTANCE':"
    publisher_dump_ffmpeg_args
fi

umask 0027

exec setpriv --reuid "$BLUESTREAM_USER" --regid "$BLUESTREAM_GROUP" \
    --init-groups --inh-caps=-all --no-new-privs ffmpeg "${FFMPEG_ARGS[@]}"
