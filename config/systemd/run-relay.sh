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

# GUI-7A / source-ingest: a TYPE=youtube or TYPE=web-resolver relay stores a
# PUBLIC WEBPAGE *page* URL, which is HTML - not a media stream. Resolve the
# CURRENT playable media URL fresh on every start with yt-dlp (public content
# only; fixed argv; no cookies/config/eval). Nothing is stored back; the
# config keeps the original public page URL and is re-resolved on every start.
# Direct media sources (remote-hls/http-file/rtmp/rtmps/rtsp/local-file) never
# enter this path and work with or without yt-dlp.
RELAY_RESOLVED_URL=""
case "$RELAY_TYPE" in
    youtube|web-resolver)
        # shellcheck source=lib/resolver.sh
        source "$LIBDIR/resolver.sh" || exit 1
        if RELAY_RESOLVED_URL="$(bs_web_resolve "$RELAY_URL")"; then
            rm -f "$BLUESTREAM_RUN_DIR/$NAME.resolve-fail" 2>/dev/null || true
        else
            _rc=$?
            # A safe, bounded, high-level start diagnostic for the web console
            # (never raw yt-dlp stderr, never a signed URL, never a secret).
            # Written root-only under the managed run directory and cleared on
            # the next successful resolve/start (relay_start/stop/restart/
            # delete also clear it).
            mkdir -p "$BLUESTREAM_RUN_DIR" 2>/dev/null || true
            _code="RESOLUTION_FAILED"
            case "$_rc" in
                1) _code="UNSUPPORTED_PAGE" ;;
                2) _code="RESOLVER_UNAVAILABLE" ;;
                4) _code="RESOLUTION_INVALID" ;;
            esac
            printf '%s\n' "$_code" > "$BLUESTREAM_RUN_DIR/$NAME.resolve-fail" 2>/dev/null || true
            chmod 0600 "$BLUESTREAM_RUN_DIR/$NAME.resolve-fail" 2>/dev/null || true
            case "$_code" in
                RESOLVER_UNAVAILABLE)
                    bs_error "Relay '$NAME': the public webpage resolver (yt-dlp) is not installed on this server. Public webpage sources need it; every direct media source works without it." ;;
                UNSUPPORTED_PAGE)
                    bs_error "Relay '$NAME': the configured source is not a supported public webpage URL. Only public pages supported by the installed resolver (yt-dlp) can be used." ;;
                RESOLUTION_INVALID)
                    bs_error "Relay '$NAME': the resolver did not return exactly one usable public media URL." ;;
                *)
                    bs_error "Relay '$NAME': could not resolve the public webpage into a playable media URL. The source may be offline or unavailable to the public; only PUBLIC content is supported." ;;
            esac
            unset _rc _code
            exit 1
        fi
        unset _rc
        ;;
esac

# nginx-rtmp (www-data) owns all HLS output; FFmpeg publishes over the
# private local RTMP socket and never writes the HLS filesystem directly,
# so no HLS directory bootstrap is required here.

# Fail closed: privilege drop prerequisites.
id "$BLUESTREAM_USER" >/dev/null 2>&1 || exit 1
command -v setpriv >/dev/null 2>&1 || exit 1
command -v ffmpeg >/dev/null 2>&1 || exit 1

relay_build_ffmpeg_args || exit 1

# Fail closed if the argv is structurally invalid (e.g. codec args collapsed
# so that 'copy' could be parsed as a positional output filename).
if ! bs_verify_ffmpeg_args; then
    bs_error "Refusing to exec FFmpeg: malformed argument array for relay '$NAME'."
    bs_dump_ffmpeg_args
    exit 1
fi
if [ "${BS_DEBUG_ARGS:-0}" = "1" ]; then
    bs_info "Sanitized FFmpeg argv for relay '$NAME':"
    bs_dump_ffmpeg_args
fi

# Final umask so HLS segments are 0640 (group-readable by nginx).
umask 0027

exec setpriv --reuid "$BLUESTREAM_USER" --regid "$BLUESTREAM_GROUP" \
    --init-groups --inh-caps=-all --no-new-privs ffmpeg "${FFMPEG_ARGS[@]}"
