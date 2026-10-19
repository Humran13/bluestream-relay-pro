#!/usr/bin/env bash
# BlueStream Relay Pro - generic FFmpeg process supervision for live-source
# reliability (shared by the relay and playlist runtime wrappers; see
# run-relay.sh and run-playlist.sh).
#
# ROOT CAUSE this module exists to fix (confirmed on a real VPS): systemd's
# `Restart=on-failure` NEVER restarts a unit whose main process exited with
# code 0 ("successful"). FFmpeg exits 0 on BOTH a deliberate stop AND a
# plain live-source EOF/disconnect with no error - systemd's ActiveState/
# Result cannot tell these apart. A live relay that hit a transient source
# hiccup therefore looked IDENTICAL, from systemd's point of view, to "the
# operator stopped it on purpose" and was silently never restarted (observed:
# Result=success, ExecMainStatus=0, NRestarts=0, relay simply stopped
# producing HLS about 15 seconds after it started).
#
# This module runs FFmpeg as a CHILD of the same long-lived process systemd
# supervises (Type=simple), in a loop, so the "restart or not" decision is
# made HERE - with full knowledge of *why* FFmpeg exited and whether this
# process itself was asked to stop (via a trapped signal, never inferred
# from FFmpeg's exit code) - instead of leaving it to systemd's blunt
# exit-code-only Restart= policy. systemd's own Restart=on-failure /
# StartLimitBurst remain in the unit file purely as an OUTER safety net,
# triggered only when this loop itself gives up and exits non-zero.
#
# BlueStream Relay Pro 0.1.0 (foundation). See LICENSE for terms.

if [ -n "${BLUESTREAM_SUPERVISE_LOADED:-}" ]; then
    return 0 2>/dev/null || exit 0
fi
BLUESTREAM_SUPERVISE_LOADED=1

# Bounded exponential backoff parameters. Only the base delay (the relay's
# own configured RESTART_SEC, 1-300s) is user-configurable; the bound and
# reset-window are fixed so the safety behavior stays predictable.
BS_SUPERVISE_MAX_ATTEMPTS=8     # give up after this many unexpected restarts...
BS_SUPERVISE_WINDOW_RESET=60    # ...unless the previous run lasted this long (a
                                 # healthy run resets the backoff counter, so one
                                 # bad patch never permanently inflates the delay)
BS_SUPERVISE_DELAY_CAP=300      # backoff never waits longer than this (seconds)

# ---------------------------------------------------------------------------
# Status file: the authoritative, root-cause-fixing record of WHY a relay or
# playlist FFmpeg process most recently stopped - something systemd's ActiveState/
# Result alone cannot express (see header). Written by the UNPRIVILEGED
# relay user into $BLUESTREAM_RUN_DIR, which it already owns; read by
# lib/health.sh, status/detail views and diagnostics (root, or via web-ctl).
# ---------------------------------------------------------------------------
bs_supervise_status_file() {
    # $1=kind (relay|playlist) $2=name
    printf '%s/%s-%s.status' "$BLUESTREAM_RUN_DIR" "$1" "$2"
}

# Atomically write a small key=value status file. Never fatal - a failed
# write (e.g. a transient disk issue) is swallowed so it can never bring
# down the supervised process itself.
#   state:  STARTING | LIVE | RECONNECTING | STOPPED | COMPLETED | FAILED
#   detail: short human-readable reason (never a credential/secret/signed URL)
bs_supervise_write_status() {
    local kind="$1" name="$2" state="$3" detail="$4" exit_code="${5:-}" attempt="${6:-0}"
    local f tmp
    f="$(bs_supervise_status_file "$kind" "$name")"
    tmp="${f}.tmp.$$"
    {
        printf 'STATE=%s\n' "$state"
        printf 'DETAIL=%s\n' "$detail"
        printf 'EXIT_CODE=%s\n' "$exit_code"
        printf 'ATTEMPT=%s\n' "$attempt"
        printf 'UPDATED=%s\n' "$(date +%s)"
    } > "$tmp" 2>/dev/null || return 0
    chmod 0640 "$tmp" 2>/dev/null || true
    mv -f "$tmp" "$f" 2>/dev/null || true
    return 0
}

# Read a status file into BS_SUPERVISE_STATE / _DETAIL / _EXIT_CODE /
# _ATTEMPT / _UPDATED. Returns 1 (BS_SUPERVISE_STATE=UNKNOWN) when missing or
# unreadable - never fatal. Uses the existing safe whitelist kv-parser
# (bs_parse_kv_file), so a tampered status file can never inject arbitrary
# shell variables/values beyond these five fixed keys.
bs_supervise_read_status() {
    local kind="$1" name="$2"
    BS_SUPERVISE_STATE="UNKNOWN"; BS_SUPERVISE_DETAIL=""; BS_SUPERVISE_EXIT_CODE=""
    BS_SUPERVISE_ATTEMPT="0"; BS_SUPERVISE_UPDATED="0"
    local f
    f="$(bs_supervise_status_file "$kind" "$name")"
    [ -r "$f" ] || return 1
    BLUESTREAM_CFG_FILE="$f"
    BLUESTREAM_CFG_KEYS="STATE DETAIL EXIT_CODE ATTEMPT UPDATED"
    BLUESTREAM_CFG_PREFIX="BS_SUPERVISE_"
    bs_parse_kv_file
    return 0
}

# Remove a stale status file - called before a fresh start so an old status
# from a previous run never lingers past a restart the operator explicitly
# asked for, and on relay removal/delete so no orphaned file is left behind.
bs_supervise_clear_status() {
    rm -f "$(bs_supervise_status_file "$1" "$2")" 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# The supervision loop.
#   $1 kind       relay | playlist
#   $2 name       instance name
#   $3 finite_ok  1 = a clean (exit 0, no signal) termination is a NORMAL,
#                 EXPECTED completion (finite media, no loop configured) -
#                 never retried.
#                 0 = ANY unexpected termination (any exit code, including
#                 0 - "clean" tells us nothing about whether it was
#                 expected) is retried with backoff. Used for every live/
#                 remote source type, and for looped finite media (which
#                 should never exit on its own under normal operation).
#   $4 base_delay base seconds for the FIRST reconnect attempt (the relay's
#                 own configured RESTART_SEC).
#   $5 build_fn   name of a function that (re)builds the FFMPEG_ARGS array
#                 fresh on every attempt (required for sources whose
#                 resolved URL can expire, e.g. YouTube - re-resolving only
#                 once at the top would eventually hand a reconnect attempt
#                 a stale URL). Must set the global FFMPEG_ARGS array and
#                 return 0, or return non-zero if the source could not be
#                 prepared (e.g. resolver failure) - treated as fatal.
#
# Traps SIGTERM/SIGINT (what `systemctl stop` sends) to distinguish a
# DELIBERATE stop from FFmpeg exiting on its own: a deliberate stop forwards
# the signal to whichever child is currently running (FFmpeg, or the backoff
# sleep) and exits 0 as STOPPED - never logged as a failure/reconnect. This
# is the ONLY trusted signal for "this was asked to stop"; it is never
# inferred from FFmpeg's exit code, which is 0 on both a clean stop and a
# clean unexpected live-source EOF.
bs_supervise_run() {
    local kind="$1" name="$2" finite_ok="$3" base_delay="$4" build_fn="$5"
    local attempt=0 stop_requested=0 current_child="" start_ts now ran_for ec delay

    case "$base_delay" in
        ''|*[!0-9]*) base_delay=5 ;;
    esac
    [ "$base_delay" -ge 1 ] 2>/dev/null || base_delay=5

    _bs_supervise_on_signal() {
        stop_requested=1
        if [ -n "$current_child" ] && kill -0 "$current_child" 2>/dev/null; then
            kill -TERM "$current_child" 2>/dev/null
        fi
    }
    trap _bs_supervise_on_signal TERM INT

    bs_supervise_write_status "$kind" "$name" STARTING "starting" "" 0

    while true; do
        if [ "$stop_requested" = "1" ]; then
            bs_supervise_write_status "$kind" "$name" STOPPED "stopped by request" "" "$attempt"
            exit 0
        fi

        if ! "$build_fn"; then
            if [ "$stop_requested" = "1" ]; then
                bs_supervise_write_status "$kind" "$name" STOPPED "stopped by request" "" "$attempt"
                exit 0
            fi
            bs_supervise_write_status "$kind" "$name" FAILED "could not prepare the source (see journalctl for this unit)" "" "$attempt"
            exit 1
        fi
        if ! bs_verify_ffmpeg_args; then
            bs_error "Refusing to exec FFmpeg: malformed argument array for $kind '$name'."
            bs_dump_ffmpeg_args
            bs_supervise_write_status "$kind" "$name" FAILED "internal error: malformed ffmpeg arguments" "" "$attempt"
            exit 1
        fi

        # Close the stop-vs-launch race: a TERM/INT may arrive while the
        # build function or argument verifier is running. Never start a new
        # FFmpeg child after that deliberate stop has already been requested.
        if [ "$stop_requested" = "1" ]; then
            bs_supervise_write_status "$kind" "$name" STOPPED "stopped by request" "" "$attempt"
            exit 0
        fi

        bs_supervise_write_status "$kind" "$name" LIVE "ffmpeg running" "" "$attempt"
        start_ts="$(date +%s)"
        ffmpeg "${FFMPEG_ARGS[@]}" &
        current_child=$!
        # TERM can also land in the tiny interval between the check above
        # and assigning current_child. The trap cannot forward it until the
        # PID is known, so re-check immediately and terminate that child.
        if [ "$stop_requested" = "1" ]; then
            kill -TERM "$current_child" 2>/dev/null || true
        fi
        wait "$current_child"
        ec=$?
        current_child=""
        now="$(date +%s)"
        ran_for=$(( now - start_ts ))

        if [ "$stop_requested" = "1" ]; then
            bs_supervise_write_status "$kind" "$name" STOPPED "stopped by request" "$ec" "$attempt"
            exit 0
        fi

        if [ "$finite_ok" = "1" ] && [ "$ec" -eq 0 ]; then
            bs_supervise_write_status "$kind" "$name" COMPLETED "finite media playback completed normally (no loop configured)" "$ec" "$attempt"
            exit 0
        fi

        # Unexpected termination (any exit code, including 0): reconnect
        # with bounded exponential backoff. A sustained healthy run resets
        # the attempt counter first, so one bad patch never permanently
        # inflates the delay for an otherwise long-lived relay.
        if [ "$ran_for" -ge "$BS_SUPERVISE_WINDOW_RESET" ]; then
            attempt=0
        fi
        attempt=$(( attempt + 1 ))
        if [ "$attempt" -gt "$BS_SUPERVISE_MAX_ATTEMPTS" ]; then
            bs_supervise_write_status "$kind" "$name" FAILED "exceeded $BS_SUPERVISE_MAX_ATTEMPTS reconnection attempts (last ffmpeg exit=$ec)" "$ec" "$attempt"
            exit 1
        fi
        delay=$(( base_delay * (1 << (attempt - 1)) ))
        [ "$delay" -gt "$BS_SUPERVISE_DELAY_CAP" ] && delay="$BS_SUPERVISE_DELAY_CAP"
        bs_supervise_write_status "$kind" "$name" RECONNECTING "unexpected end (ffmpeg exit=$ec after ${ran_for}s); reconnecting in ${delay}s (attempt $attempt/$BS_SUPERVISE_MAX_ATTEMPTS)" "$ec" "$attempt"
        sleep "$delay" &
        current_child=$!
        wait "$current_child"
        current_child=""
    done
}

# end of supervise.sh
