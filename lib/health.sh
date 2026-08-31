#!/usr/bin/env bash
# BlueStream Relay Pro - relay/playlist health state determination.
#
# A service being "active" is not sufficient: health also requires fresh
# HLS segments. Possible states:
#   HEALTHY  - service active and HLS fresh
#   STARTING - service active but HLS not yet produced (recent start)
#   STALE    - service active but HLS is not fresh
#   STOPPED  - stopped (including a clean intentional stop of a still-enabled
#              unit, detected via ActiveState=inactive + Result=success)
#   DISABLED - alias of STOPPED used when config ENABLED=no
#   FAILED   - enabled at boot but not running with a non-success Result, or
#              the unit is in the failed state
#   UNKNOWN  - not configured / cannot be determined
#
# BlueStream Relay Pro 0.1.0 (foundation). See LICENSE for terms.

if [ -n "${BLUESTREAM_HEALTH_LOADED:-}" ]; then
    return 0 2>/dev/null || exit 0
fi
BLUESTREAM_HEALTH_LOADED=1

HEALTH_STATE="UNKNOWN"
HEALTH_DETAILS=""

health_state() {
    local kind="$1" name="$2"
    local unit conf hlsdir active_state enable_state result
    HEALTH_STATE="UNKNOWN"
    HEALTH_DETAILS=""

    bs_valid_name "$name" || return 1

    if [ "$kind" = "relay" ]; then
        conf="$BLUESTREAM_RELAY_CONF_DIR/$name.conf"
    else
        conf="$BLUESTREAM_PLAYLIST_CONF_DIR/$name.playlist"
    fi
    [ -f "$conf" ] || return 1

    hlsdir="$(bs_hls_dir_for "$kind" "$name")"
    unit="$(bs_unit "$kind" "$name")"

    active_state="$(systemctl is-active "$unit" 2>/dev/null)"
    case "$active_state" in
        active|inactive|failed|activating|deactivating|reloading) ;;
        *) active_state="unknown" ;;
    esac

    enable_state="$(systemctl is-enabled "$unit" 2>/dev/null)"
    case "$enable_state" in
        enabled|enabled-runtime|disabled|static|indirect|alias|masked|linked) ;;
        *) enable_state="unknown" ;;
    esac

    if [ "$active_state" = "active" ]; then
        if bs_hls_is_fresh "$hlsdir" 45; then
            HEALTH_STATE="HEALTHY"
            HEALTH_DETAILS="fresh HLS segments being produced"
        else
            # Playlist preparation (media normalization) happens inside the
            # unit's root bootstrap BEFORE FFmpeg execs, so an active playlist
            # may legitimately produce no HLS for a while. While a fresh
            # prepare marker exists, report STARTING instead of STALE; the
            # marker is cleared by run-playlist.sh once preparation completes.
            if [ "$kind" = "playlist" ] && [ -f "$BLUESTREAM_RUN_DIR/$name.prepare" ]; then
                local p_ts p_now p_age
                p_ts="$(stat -c %Y "$BLUESTREAM_RUN_DIR/$name.prepare" 2>/dev/null || printf '0')"
                p_now="$(date +%s)"
                p_age=$(( p_now - p_ts ))
                if [ "$p_age" -ge 0 ] && [ "$p_age" -le "$BLUESTREAM_PLAYLIST_PREPARE_TIMEOUT" ]; then
                    HEALTH_STATE="STARTING"
                    HEALTH_DETAILS="preparing playlist media (normalizing entries)"
                    return 0
                fi
            fi
            local start_ts="" now age
            start_ts="$(systemctl show -p ActiveEnterTimestamp --value "$unit" 2>/dev/null)"
            now="$(date +%s)"
            if [ -n "$start_ts" ]; then
                age=$(( now - $(date -d "$start_ts" +%s 2>/dev/null || printf '%s' "$now") ))
            else
                age=9999
            fi
            if [ "$age" -lt 30 ]; then
                HEALTH_STATE="STARTING"
                HEALTH_DETAILS="service started ${age}s ago; waiting for HLS output"
            else
                HEALTH_STATE="STALE"
                HEALTH_DETAILS="service active but HLS output is not fresh (source may be down)"
            fi
        fi
    elif [ "$active_state" = "failed" ]; then
        HEALTH_STATE="FAILED"
        HEALTH_DETAILS="unit is in failed state (see: journalctl -u $unit)"
    elif [ "$active_state" = "activating" ]; then
        HEALTH_STATE="STARTING"
        HEALTH_DETAILS="unit is activating"
    else
        # An intentional clean stop (relay_stop/playlist_stop) runs
        # `systemctl reset-failed`, so it is visible as ActiveState=inactive +
        # Result=success even when the unit stays enabled at boot. Only an
        # inactive unit whose systemd Result is anything OTHER than success is
        # a genuine failure when still enabled; empty/unknown Result values are
        # NOT treated as success so real failures are never masked. Do NOT use
        # the process exit status as the success signal here: FFmpeg exits
        # non-zero on every intentional stop, so Result=success is the only
        # trusted intentional-stop signal.
        if [ "$active_state" = "inactive" ]; then
            result="$(systemctl show -p Result --value "$unit" 2>/dev/null)"
            if [ "$result" = "success" ]; then
                HEALTH_STATE="STOPPED"
                HEALTH_DETAILS="stopped cleanly (intentional stop; Result=success)"
                return 0
            fi
        fi
        if [ "$enable_state" = "enabled" ] || [ "$enable_state" = "enabled-runtime" ]; then
            HEALTH_STATE="FAILED"
            HEALTH_DETAILS="enabled at boot but not running"
        else
            HEALTH_STATE="STOPPED"
            HEALTH_DETAILS="stopped and not enabled at boot"
        fi
    fi
    return 0
}

health_color() {
    case "$HEALTH_STATE" in
        HEALTHY)  printf '%s%s%s'   "$C_GREEN"   "$HEALTH_STATE" "$C_RESET" ;;
        STARTING) printf '%s%s%s'   "$C_CYAN"    "$HEALTH_STATE" "$C_RESET" ;;
        STALE|FAILED) printf '%s%s%s' "$C_RED"   "$HEALTH_STATE" "$C_RESET" ;;
        *)        printf '%s%s%s'   "$C_YELLOW"  "$HEALTH_STATE" "$C_RESET" ;;
    esac
}
