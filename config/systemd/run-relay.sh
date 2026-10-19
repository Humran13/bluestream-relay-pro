#!/usr/bin/env bash
# BlueStream Relay Pro - systemd runtime wrapper for a single relay.
#
# Root bootstrap (runs as root): reads the root-only relay config, performs
# the one-time root-only step (Phase 1C publisher sync, which writes systemd
# drop-ins under /etc/systemd/system), then re-execs ITSELF as the
# unprivileged bluestream-relay user (setpriv) to run the actual FFmpeg
# supervision loop (lib/supervise.sh). FFmpeg never runs as root, and
# neither does anything in the supervision loop below - the re-exec happens
# before any of it.
#
# WHY re-exec instead of a single final `exec ffmpeg` (as earlier versions
# did): live-source reliability requires FFmpeg to run inside a retry/
# backoff LOOP that knows why each attempt ended (deliberate stop vs.
# finite-media completion vs. unexpected EOF) - see lib/supervise.sh for the
# full rationale. That loop has to be the actual process systemd supervises
# as Type=simple's main process, so it must survive here, after privileges
# are dropped, instead of being replaced by a single `exec ffmpeg`.
#
# Installed to /usr/local/lib/bluestream/run-relay.sh
set -u
shopt -s nullglob

NAME="${1:-}"
case "$NAME" in
    *[!a-z0-9_-]*|""|-*|_*) exit 1 ;;
esac

LIBDIR="/usr/local/lib/bluestream/lib"

[ -f "$LIBDIR/common.sh" ] || exit 1
# shellcheck source=lib/common.sh
source "$LIBDIR/common.sh" || exit 1
# shellcheck source=lib/probe.sh
source "$LIBDIR/probe.sh" || exit 1
# shellcheck source=lib/relay.sh
source "$LIBDIR/relay.sh" || exit 1
# shellcheck source=lib/destinations.sh
source "$LIBDIR/destinations.sh" || exit 1
# shellcheck source=lib/playlist.sh
source "$LIBDIR/playlist.sh" || exit 1
# shellcheck source=lib/publisher.sh
source "$LIBDIR/publisher.sh" || exit 1
# shellcheck source=lib/resolver.sh
source "$LIBDIR/resolver.sh" || exit 1
# shellcheck source=lib/supervise.sh
source "$LIBDIR/supervise.sh" || exit 1

bs_detect_install
bs_load_server_conf

if [ "${BLUESTREAM_PRIV_DROPPED:-0}" != "1" ]; then
    # --- Root bootstrap (runs once, before privileges are dropped) --------
    relay_load_config "$NAME" || exit 1
    relay_config_validate || exit 1

    # A fresh (re)start clears any stale status from a previous run, so the
    # dashboard never shows a leftover COMPLETED/FAILED/STOPPED state while
    # this new attempt is only just starting.
    bs_supervise_clear_status relay "$NAME"

    # Fail closed: privilege drop prerequisites.
    id "$BLUESTREAM_USER" >/dev/null 2>&1 || exit 1
    command -v setpriv >/dev/null 2>&1 || exit 1
    command -v ffmpeg >/dev/null 2>&1 || exit 1

    # Phase 1C: (re)start this relay's attached+enabled outbound publishers.
    # Needs root (writes systemd drop-ins under /etc/systemd/system) and
    # must happen here, before the privilege drop. Runs once per systemd-
    # level start of this unit; internal reconnects below never re-run it
    # (a relay's publisher attachments don't change between FFmpeg attempts
    # of the SAME start). Best-effort: a publisher sync failure must never
    # block the relay itself from starting.
    publisher_sync_target relay "$NAME" 2>/dev/null || true

    # Hand the already-validated config to the unprivileged continuation via
    # the environment, instead of having it re-read the root-only (0600)
    # relay config file as a non-root user (which would simply fail). These
    # are configuration values, not secrets - already visible in FFmpeg's
    # own argv/journal regardless.
    export BLUESTREAM_PRIV_DROPPED=1
    export BS_RELAY_NAME="$RELAY_NAME" BS_RELAY_TYPE="$RELAY_TYPE" BS_RELAY_URL="$RELAY_URL"
    export BS_RELAY_LOOP="$RELAY_LOOP" BS_RELAY_RESTART_SEC="$RELAY_RESTART_SEC"

    exec setpriv --reuid "$BLUESTREAM_USER" --regid "$BLUESTREAM_GROUP" \
        --init-groups --inh-caps=-all --no-new-privs "$0" "$@"
fi

# ---------------------------------------------------------------------------
# Unprivileged continuation - runs as $BLUESTREAM_USER from here on.
# ---------------------------------------------------------------------------
RELAY_NAME="$BS_RELAY_NAME"
RELAY_TYPE="$BS_RELAY_TYPE"
RELAY_URL="$BS_RELAY_URL"
RELAY_LOOP="$BS_RELAY_LOOP"
RELAY_RESTART_SEC="$BS_RELAY_RESTART_SEC"

# GUI-7A / source-ingest: TYPE=youtube or TYPE=web-resolver stores a PUBLIC
# WEBPAGE *page* URL, which is HTML - not a media stream. Resolved to the
# CURRENT playable media URL fresh on EVERY attempt (not just the first),
# since a resolved URL can expire between reconnects - re-resolving only
# once, before the retry loop, would eventually hand a reconnect attempt a
# stale URL. Nothing is stored back; the config (and BS_RELAY_URL above)
# keeps the original public page URL. Direct media sources never enter this
# path and are unaffected either way.
#
# This is the function lib/supervise.sh's bs_supervise_run calls fresh
# before every FFmpeg attempt (first start AND every reconnect).
_relay_build_for_supervisor() {
    RELAY_RESOLVED_URL=""
    case "$RELAY_TYPE" in
        youtube|web-resolver)
            if RELAY_RESOLVED_URL="$(bs_web_resolve "$RELAY_URL")"; then
                rm -f "$BLUESTREAM_RUN_DIR/$RELAY_NAME.resolve-fail" 2>/dev/null || true
            else
                local _rc=$? _code="RESOLUTION_FAILED"
                # A safe, bounded, high-level start diagnostic for the web
                # console (never raw yt-dlp stderr, never a signed URL,
                # never a secret). Root-only-readable via web-ctl; owned and
                # writable by this unprivileged user (it lives under
                # $BLUESTREAM_RUN_DIR, which this user already owns).
                case "$_rc" in
                    1) _code="UNSUPPORTED_PAGE" ;;
                    2) _code="RESOLVER_UNAVAILABLE" ;;
                    4) _code="RESOLUTION_INVALID" ;;
                esac
                printf '%s\n' "$_code" > "$BLUESTREAM_RUN_DIR/$RELAY_NAME.resolve-fail" 2>/dev/null || true
                chmod 0640 "$BLUESTREAM_RUN_DIR/$RELAY_NAME.resolve-fail" 2>/dev/null || true
                case "$_code" in
                    RESOLVER_UNAVAILABLE)
                        bs_error "Relay '$RELAY_NAME': the public webpage resolver (yt-dlp) is not installed on this server. Public webpage sources need it; every direct media source works without it." ;;
                    UNSUPPORTED_PAGE)
                        bs_error "Relay '$RELAY_NAME': the configured source is not a supported public webpage URL. Only public pages supported by the installed resolver (yt-dlp) can be used." ;;
                    RESOLUTION_INVALID)
                        bs_error "Relay '$RELAY_NAME': the resolver did not return exactly one usable public media URL." ;;
                    *)
                        bs_error "Relay '$RELAY_NAME': could not resolve the public webpage into a playable media URL. The source may be offline or unavailable to the public; only PUBLIC content is supported." ;;
                esac
                return 1
            fi
            ;;
    esac

    relay_build_ffmpeg_args || return 1

    if [ "${BS_DEBUG_ARGS:-0}" = "1" ]; then
        bs_info "Sanitized FFmpeg argv for relay '$RELAY_NAME':"
        bs_dump_ffmpeg_args
    fi
    return 0
}

# nginx-rtmp (www-data) owns all HLS output; FFmpeg publishes over the
# private local RTMP socket and never writes the HLS filesystem directly,
# so no HLS directory bootstrap is required here. Final umask so HLS
# segments stay 0640 (group-readable by nginx) regardless.
umask 0027

# Deliberate-stop vs. unexpected-end classification (see lib/supervise.sh):
#   - local-file, no loop configured: finite, no loop -> a clean (exit 0)
#     end is the EXPECTED natural completion of the file. Never retried.
#   - local-file WITH loop configured: FFmpeg's own `-stream_loop -1`
#     should never let it exit on its own: if it does, that is unexpected.
#   - every other (live/remote) source type: NEVER an "expected" ending -
#     any termination, any exit code, is an unexpected EOF/disconnect and
#     is retried with bounded exponential backoff.
if [ "$RELAY_TYPE" = "local-file" ] && [ "$RELAY_LOOP" != "yes" ]; then
    RELAY_FINITE_OK=1
else
    RELAY_FINITE_OK=0
fi

bs_supervise_run relay "$RELAY_NAME" "$RELAY_FINITE_OK" "$RELAY_RESTART_SEC" _relay_build_for_supervisor
