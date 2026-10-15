#!/usr/bin/env bash
#
# BlueStream Relay Pro - Phase 1C outbound multistream publisher engine.
#
# Architecture:
#
#   SOURCE (relay/playlist FFmpeg) -> PRIVATE LOCAL RTMP BUS (nginx-rtmp)
#       |-- nginx-rtmp HLS (website playback)              [unaffected]
#       |-- bluestream-publisher@relay:<name>:<destid>      [one per
#       |-- bluestream-publisher@relay:<name>:<destid2>      attached,
#       `-- ...                                              enabled
#                                                             destination]
#
# Each attached + enabled destination gets its OWN independent systemd unit
# instance (its own FFmpeg process) that PULLS the source from the private
# local RTMP bus (the same bus nginx-rtmp already consumes for HLS) and PUSHES
# it to the operator's own RTMP/RTMPS destination. A destination's publisher
# never touches another destination's process, config, or systemd unit:
#   - one destination failing/reconnecting never affects the website HLS feed,
#     the relay/playlist itself, or any other destination;
#   - the only coupling to the source is BindsTo=/After= on the owning
#     relay/playlist unit (see publisher_sync_dropin), so a publisher stops
#     automatically when its source stops and is (re)started by the explicit
#     publisher_sync_target call made at every relay/playlist start (run-
#     relay.sh / run-playlist.sh), whether that start was triggered by the web
#     console, the CLI, systemd's own Restart=on-failure, or a normal boot.
#
# Reconnect model: ffmpeg has no generic "reconnect on write failure" for an
# RTMP OUTPUT (the `-reconnect*` flags only apply to the *input* side and only
# for a few protocols). A destination disconnect therefore ends the publisher
# process; systemd's Restart=on-failure (RestartSec=3) retries it, and it picks
# the source back up from the still-running local RTMP bus. This mirrors
# exactly how relay/playlist sources already recover from their own
# disconnects - no new recovery mechanism was invented.
#
# Instance naming: "<kind>:<target>:<destid>" (kind=relay|playlist). ':' can
# never appear inside a validated name/id (bs_valid_name allows only
# [a-z0-9_-]), so the three fields split back out unambiguously.
#
# Stream-key handling / security: see docs/security.md "Outbound publisher"
# section for the full write-up, including the documented residual exposure
# (same-account /proc visibility) and how it is bounded.
#
# BlueStream Relay Pro 0.1.0 (foundation). See LICENSE for terms.

if [ -n "${BLUESTREAM_PUBLISHER_LOADED:-}" ]; then
    return 0 2>/dev/null || exit 0
fi
BLUESTREAM_PUBLISHER_LOADED=1

BLUESTREAM_SERVICE_PUBLISHER_PREFIX="bluestream-publisher@"

# Fast restart: a destination disconnect should recover quickly without
# hammering the destination or systemd. 15 attempts per 2 minutes is enough
# headroom for a flaky destination while still reaching FAILED (start-limit)
# in a bounded time if the destination is genuinely unreachable.
BLUESTREAM_PUBLISHER_RESTART_SEC=3
BLUESTREAM_PUBLISHER_STARTLIMIT_INTERVAL=120
BLUESTREAM_PUBLISHER_STARTLIMIT_BURST=15

# Status thresholds (seconds).
BLUESTREAM_PUBLISHER_LIVE_WINDOW=30
BLUESTREAM_PUBLISHER_STARTING_WINDOW=20
# Bounded journal lookback used for both status classification and the
# "last connected" evidence below - never an unbounded read.
BLUESTREAM_PUBLISHER_JOURNAL_LINES=60

PUB_KIND=""; PUB_TARGET=""; PUB_DEST=""

# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------
publisher_instance() {
    printf '%s:%s:%s' "$1" "$2" "$3"
}

publisher_unit() {
    printf '%s%s.service' "$BLUESTREAM_SERVICE_PUBLISHER_PREFIX" "$(publisher_instance "$1" "$2" "$3")"
}

# Parse a "<kind>:<target>:<destid>" instance string into PUB_KIND/PUB_TARGET/
# PUB_DEST. Fails closed on anything that is not exactly three validated
# fields (never a path, never a wildcard, never extra ':' fields).
publisher_parse_instance() {
    local instance="$1" kind="" target="" destid="" rest=""
    PUB_KIND=""; PUB_TARGET=""; PUB_DEST=""
    case "$instance" in
        *:*:*) ;;
        *) return 1 ;;
    esac
    kind="${instance%%:*}"
    rest="${instance#*:}"
    target="${rest%%:*}"
    destid="${rest#*:}"
    case "$destid" in *:*) return 1 ;; esac
    case "$kind" in relay|playlist) ;; *) return 1 ;; esac
    bs_valid_name "$target" || return 1
    bs_valid_name "$destid" || return 1
    PUB_KIND="$kind"; PUB_TARGET="$target"; PUB_DEST="$destid"
    return 0
}

# ---------------------------------------------------------------------------
# ffmpeg argument construction: source = private local RTMP bus (play),
# destination = the operator's own validated RTMP/RTMPS server + stream key
# (publish). Never transcodes; stream copy only, exactly like relay/playlist.
# ---------------------------------------------------------------------------
publisher_build_ffmpeg_args() {
    local kind="$1" name="$2" destid="$3" app="" inurl="" outurl=""
    FFMPEG_ARGS=()
    case "$kind" in
        relay)    app="$BLUESTREAM_RTMP_APP_RELAY" ;;
        playlist) app="$BLUESTREAM_RTMP_APP_PLAYLIST" ;;
        *) return 1 ;;
    esac
    bs_valid_name "$name" || return 1
    dest_load_config "$destid" || return 1
    [ "$DEST_ENABLED" = "yes" ] || return 1
    bs_valid_dest_url "$DEST_SERVER_URL" || return 1
    bs_valid_stream_key "$DEST_STREAM_KEY" || return 1

    inurl="${BLUESTREAM_RTMP_BASE}/${app}/${name}"
    outurl="${DEST_SERVER_URL%/}/${DEST_STREAM_KEY}"

    FFMPEG_ARGS=(
        -nostdin -y -loglevel warning
        # -progress pipe:2: ffmpeg's human "-stats" display overwrites one
        # line with carriage returns (\r, no \n); journald only splits
        # entries on \n, so under systemd that entire run collapses into one
        # giant buffered blob that is not readable until the process exits -
        # useless for live status. "-progress" instead writes a newline-
        # terminated key=value report (out_time_ms=..., bitrate=..., ...)
        # every interval, which journald captures as normal log lines in
        # real time. publisher_last_progress_age depends on these lines as
        # evidence of REAL data flow (never just "the process exists").
        -nostats -progress pipe:2
        -fflags +genpts -rw_timeout 20000000
        -i "$inurl"
        -map 0:v:0 -map "0:a:0?"
        -c:v copy -c:a copy
        -f flv
        "$outurl"
    )
    return 0
}

# Fail-closed structural verification before exec'ing FFmpeg (mirrors
# bs_verify_ffmpeg_args, adapted for the publisher's INPUT/OUTPUT direction):
#   - the INPUT (-i value) must be our own private loopback RTMP bus
#   - codec options are separate elements with their own 'copy' values
#   - output format is -f flv
#   - the OUTPUT (final element) is a bounded rtmp(s):// URL, never a flag
publisher_verify_ffmpeg_args() {
    local n="${#FFMPEG_ARGS[@]}" i last input_url found_i=0 found_flv=0
    [ "$n" -ge 12 ] || { bs_error "Publisher FFmpeg argv implausibly short ($n elements)."; return 1; }
    for i in "${!FFMPEG_ARGS[@]}"; do
        [ -n "${FFMPEG_ARGS[$i]}" ] || { bs_error "Publisher FFmpeg argv element $i is empty."; return 1; }
    done
    i=0
    while [ "$i" -lt "$n" ]; do
        case "${FFMPEG_ARGS[$i]}" in
            -i)
                [ $((i + 1)) -lt "$n" ] || { bs_error "Option -i has no value."; return 1; }
                input_url="${FFMPEG_ARGS[$((i + 1))]}"
                found_i=1
                i=$((i + 2))
                ;;
            -c:v|-c:a)
                [ $((i + 1)) -lt "$n" ] || { bs_error "Option ${FFMPEG_ARGS[$i]} has no value."; return 1; }
                if [ "${FFMPEG_ARGS[$((i + 1))]}" != "copy" ]; then
                    bs_error "Option ${FFMPEG_ARGS[$i]} must be followed by 'copy'."
                    return 1
                fi
                i=$((i + 2))
                ;;
            -f)
                [ $((i + 1)) -lt "$n" ] || { bs_error "Option -f has no value."; return 1; }
                [ "${FFMPEG_ARGS[$((i + 1))]}" = "flv" ] && found_flv=1
                i=$((i + 2))
                ;;
            *) i=$((i + 1)) ;;
        esac
    done
    [ "$found_i" = "1" ] || { bs_error "Publisher FFmpeg argv has no -i input."; return 1; }
    case "$input_url" in
        rtmp://127.0.0.1:1935/bluestream-relay/*|rtmp://127.0.0.1:1935/bluestream-playlist/*) ;;
        *) bs_error "Publisher FFmpeg input must be the private local RTMP bus."; return 1 ;;
    esac
    [ "$found_flv" = "1" ] || { bs_error "Publisher FFmpeg output format must be '-f flv'."; return 1; }
    last="${FFMPEG_ARGS[$((n - 1))]}"
    case "$last" in
        rtmp://*|rtmps://*) ;;
        *) bs_error "Publisher FFmpeg output must be an rtmp(s):// URL."; return 1 ;;
    esac
    [ "${#last}" -le 4360 ] || { bs_error "Publisher FFmpeg output URL implausibly long."; return 1; }
    return 0
}

# Print a sanitized copy of FFMPEG_ARGS for diagnostics. Unlike
# bs_dump_ffmpeg_args (which only redacts URL QUERY STRING fields), the
# publisher's destination URL carries the stream key directly in its PATH, so
# the final element (the destination URL) is masked outright - it is NEVER
# safe to print, not even redacted by field. This is the only diagnostic dump
# used anywhere in the publisher path; nothing else prints FFMPEG_ARGS.
publisher_dump_ffmpeg_args() {
    local i n="${#FFMPEG_ARGS[@]}"
    for i in "${!FFMPEG_ARGS[@]}"; do
        if [ "$i" -eq $((n - 1)) ]; then
            printf '  argv[%d] <%s>\n' "$i" "[destination URL redacted - carries the stream key]"
        else
            printf '  argv[%d] <%s>\n' "$i" "$(bs_redact_arg "${FFMPEG_ARGS[$i]}")"
        fi
    done
}

# ---------------------------------------------------------------------------
# systemd lifecycle for one destination's publisher instance.
# ---------------------------------------------------------------------------

# Generated drop-in: binds this publisher's lifecycle to its source unit (a
# publisher stops automatically when the relay/playlist it reads from stops or
# fails) and sets the fast reconnect RestartSec. Never written with any value
# that was not already validated (kind/name pass through bs_unit/bs_valid_name).
publisher_sync_dropin() {
    local kind="$1" name="$2" destid="$3"
    local unit source_unit dropin tmp
    unit="$(publisher_unit "$kind" "$name" "$destid")"
    source_unit="$(bs_unit "$kind" "$name")"
    dropin="/etc/systemd/system/$unit.d"
    mkdir -p "$dropin" 2>/dev/null || return 1
    tmp="$dropin/.override.conf.tmp.$$"
    if ! {
        printf '# BlueStream Relay Pro generated override (publisher lifecycle binding)\n'
        printf '[Unit]\n'
        printf 'BindsTo=%s\n' "$source_unit"
        printf 'After=%s\n' "$source_unit"
        printf '[Service]\n'
        printf 'RestartSec=%s\n' "$BLUESTREAM_PUBLISHER_RESTART_SEC"
    } > "$tmp"; then
        rm -f "$tmp"
        return 1
    fi
    chmod 0644 "$tmp" || { rm -f "$tmp"; return 1; }
    chown root:root "$tmp" 2>/dev/null || true
    mv -f "$tmp" "$dropin/override.conf" || { rm -f "$tmp"; return 1; }
    chmod 0644 "$dropin/override.conf" || return 1
    systemctl daemon-reload 2>/dev/null || true
    return 0
}

publisher_start() {
    local kind="$1" name="$2" destid="$3" unit
    unit="$(publisher_unit "$kind" "$name" "$destid")"
    publisher_sync_dropin "$kind" "$name" "$destid" || return 1
    [ "$(systemctl is-active "$unit" 2>/dev/null)" = "active" ] && return 0
    systemctl start "$unit" 2>/dev/null || return 1
    return 0
}

publisher_stop() {
    local kind="$1" name="$2" destid="$3" unit
    unit="$(publisher_unit "$kind" "$name" "$destid")"
    systemctl stop "$unit" 2>/dev/null || true
    systemctl reset-failed "$unit" 2>/dev/null || true
    return 0
}

# Stop + remove this publisher's generated drop-in entirely (used when a
# destination is detached, the target is deleted, or an orphaned instance -
# one whose destination is no longer attached at all - is found during sync).
publisher_remove() {
    local kind="$1" name="$2" destid="$3" unit
    unit="$(publisher_unit "$kind" "$name" "$destid")"
    systemctl stop "$unit" 2>/dev/null || true
    systemctl reset-failed "$unit" 2>/dev/null || true
    rm -rf "/etc/systemd/system/$unit.d"
    systemctl daemon-reload 2>/dev/null || true
    return 0
}

# Print the destination IDs of every EXISTING bluestream-publisher@ unit
# instance for this target (regardless of current state), one per line.
publisher_existing_destids_for_target() {
    local kind="$1" name="$2" line unitname rest destid
    systemctl list-units --all --no-legend --plain --no-pager \
        "${BLUESTREAM_SERVICE_PUBLISHER_PREFIX}${kind}:${name}:*.service" 2>/dev/null |
    while IFS= read -r line; do
        unitname="${line%% *}"
        [ -n "$unitname" ] || continue
        rest="${unitname#"${BLUESTREAM_SERVICE_PUBLISHER_PREFIX}"}"
        rest="${rest%.service}"
        destid="${rest##*:}"
        [ -n "$destid" ] && printf '%s\n' "$destid"
    done
    return 0
}

# ---------------------------------------------------------------------------
# Reconciliation: make the running publisher set for one target match its
# current attachments. Safe to call any time (attachment change, enable/
# disable toggle, or every relay/playlist start) - it is pure state sync, not
# an incremental delta, so it can never leave a stale process running or fail
# to pick up a change made by another path.
# ---------------------------------------------------------------------------
publisher_sync_target() {
    local kind="$1" name="$2" target_active="no" csv="" destid existing attached=""
    bs_valid_name "$name" || return 1
    case "$kind" in
        relay)
            relay_load_config "$name" 2>/dev/null && csv="${RELAY_DESTINATIONS:-}"
            ;;
        playlist)
            playlist_load_config "$name" 2>/dev/null && csv="${PLAYLIST_DESTINATIONS:-}"
            ;;
        *) return 1 ;;
    esac
    [ "$(systemctl is-active "$(bs_unit "$kind" "$name")" 2>/dev/null)" = "active" ] && target_active="yes"

    while IFS= read -r destid; do
        [ -n "$destid" ] || continue
        attached="${attached} ${destid}"
        if [ "$target_active" = "yes" ] && dest_load_config "$destid" 2>/dev/null && [ "$DEST_ENABLED" = "yes" ]; then
            publisher_start "$kind" "$name" "$destid" 2>/dev/null || true
        else
            publisher_stop "$kind" "$name" "$destid" 2>/dev/null || true
        fi
    done < <(bs_csv_fields "$csv")

    # Orphan cleanup: a destination that is no longer attached at all must not
    # keep a publisher unit or drop-in lying around.
    while IFS= read -r existing; do
        [ -n "$existing" ] || continue
        case " $attached " in
            *" $existing "*) ;;
            *) publisher_remove "$kind" "$name" "$existing" 2>/dev/null || true ;;
        esac
    done < <(publisher_existing_destids_for_target "$kind" "$name")
    return 0
}

# Stop and remove EVERY publisher for a target (used right before the target
# itself is deleted - relay_delete / playlist_delete already require the
# target to be definitively stopped, so BindsTo has already stopped these; this
# only cleans up the generated drop-in directories so they never accumulate).
publisher_remove_all_for_target() {
    local kind="$1" name="$2" destid
    while IFS= read -r destid; do
        [ -n "$destid" ] && publisher_remove "$kind" "$name" "$destid" 2>/dev/null || true
    done < <(publisher_existing_destids_for_target "$kind" "$name")
    return 0
}

# ---------------------------------------------------------------------------
# Status (never exposes the stream key or the destination server URL).
# ---------------------------------------------------------------------------
PUBLISHER_STATE="UNKNOWN"
PUBLISHER_DETAIL=""
PUBLISHER_RESTARTS="0"
PUBLISHER_UPTIME="0"
PUBLISHER_LAST_CONNECTED=""

# Age (seconds) of the most recent ffmpeg progress line ("out_time_ms=...")
# within a bounded recent journal window, or empty when none is found.
# Evidence of REAL data flow, not just an active unit.
#
# publisher_last_progress_age <unit> <since-epoch>
#
# <since-epoch> MUST be the CURRENT invocation's own start time (its
# ActiveEnterTimestamp). Without this bound, a progress line left over from a
# PREVIOUS run of the same unit (e.g. one that crashed and was restarted by
# systemd moments ago) would still fall inside a plain "last N lines" or
# "last N seconds" window and could make a brand-new process - one that has
# sent nothing yet - read as LIVE. Scoping strictly to "since this process
# started" closes that false-positive.
publisher_last_progress_age() {
    local unit="$1" since_epoch="$2" now line ts val best=""
    now="$(date +%s)"
    case "$since_epoch" in ''|*[!0-9]*) return 1 ;; esac
    while IFS= read -r line; do
        case "$line" in
            *' out_time_ms='[0-9]*)
                ts="${line%% *}"
                ts="${ts%%.*}"
                val="${line##*out_time_ms=}"
                case "$ts" in ''|*[!0-9]*) continue ;; esac
                case "$val" in ''|*[!0-9]*) continue ;; esac
                best="$ts"
                ;;
        esac
    done < <(journalctl -u "$unit" --since "@$since_epoch" -n "$BLUESTREAM_PUBLISHER_JOURNAL_LINES" -o short-unix --no-pager 2>/dev/null)
    [ -n "$best" ] || return 1
    printf '%d' "$((now - best))"
    return 0
}

# Bounded, fixed-vocabulary failure classification (never raw ffmpeg stderr,
# never the stream key - the key is never printed by ffmpeg in the first
# place, but this also never echoes ANY journal text back verbatim).
publisher_last_failure_reason() {
    local unit="$1" blob
    blob="$(journalctl -u "$unit" -n "$BLUESTREAM_PUBLISHER_JOURNAL_LINES" --no-pager 2>/dev/null)"
    case "$blob" in
        *'Connection refused'*)
            printf 'destination refused the connection' ;;
        *'No route to host'*)
            printf 'destination host is unreachable' ;;
        *'Name or service not known'*|*'Temporary failure in name resolution'*)
            printf 'could not resolve the destination hostname' ;;
        *'Broken pipe'*|*'Connection reset'*)
            printf 'connection to the destination was reset' ;;
        *'Operation not permitted'*|*'Permission denied'*)
            printf 'destination rejected the stream (check the stream key)' ;;
        *'Input/output error'*|*'I/O error'*)
            printf 'input/output error reading the source stream' ;;
        *'End of file'*)
            printf 'connection to the destination ended unexpectedly' ;;
        *'start-limit-hit'*)
            printf 'restart limit reached; destination may be persistently unreachable' ;;
        *)
            printf 'connection failed (see server logs for details)' ;;
    esac
    return 0
}

# publisher_status <kind> <name> <destid> - sets PUBLISHER_STATE/DETAIL/
# RESTARTS/UPTIME/LAST_CONNECTED. Never fails closed to an incorrect "Live".
publisher_status() {
    local kind="$1" name="$2" destid="$3"
    PUBLISHER_STATE="UNKNOWN"; PUBLISHER_DETAIL=""; PUBLISHER_RESTARTS="0"
    PUBLISHER_UPTIME="0"; PUBLISHER_LAST_CONNECTED=""

    dest_load_config "$destid" 2>/dev/null || { PUBLISHER_DETAIL="destination not found"; return 1; }
    if [ "$DEST_ENABLED" != "yes" ]; then
        PUBLISHER_STATE="DISABLED"
        PUBLISHER_DETAIL="destination is disabled"
        return 0
    fi

    local target_unit target_active="no"
    target_unit="$(bs_unit "$kind" "$name")"
    [ "$(systemctl is-active "$target_unit" 2>/dev/null)" = "active" ] && target_active="yes"
    if [ "$target_active" != "yes" ]; then
        PUBLISHER_STATE="STOPPED"
        PUBLISHER_DETAIL="source is not running"
        return 0
    fi

    local unit active_state restarts start_ts start_epoch now age progress_age
    unit="$(publisher_unit "$kind" "$name" "$destid")"
    active_state="$(systemctl is-active "$unit" 2>/dev/null)"
    restarts="$(systemctl show -p NRestarts --value "$unit" 2>/dev/null)"
    case "$restarts" in ''|*[!0-9]*) restarts="0" ;; esac
    PUBLISHER_RESTARTS="$restarts"

    case "$active_state" in
        active)
            start_ts="$(systemctl show -p ActiveEnterTimestamp --value "$unit" 2>/dev/null)"
            now="$(date +%s)"
            start_epoch="$now"
            if [ -n "$start_ts" ]; then
                start_epoch="$(date -d "$start_ts" +%s 2>/dev/null || printf '%s' "$now")"
            fi
            case "$start_epoch" in ''|*[!0-9]*) start_epoch="$now" ;; esac
            age=$(( now - start_epoch ))
            [ "$age" -ge 0 ] 2>/dev/null || age=0
            PUBLISHER_UPTIME="$age"
            if progress_age="$(publisher_last_progress_age "$unit" "$start_epoch")" && [ "$progress_age" -le "$BLUESTREAM_PUBLISHER_LIVE_WINDOW" ]; then
                PUBLISHER_STATE="LIVE"
                PUBLISHER_DETAIL="publishing (data flowing to the destination)"
                PUBLISHER_LAST_CONNECTED="$now"
            elif [ "$restarts" -gt 0 ]; then
                # Checked BEFORE the plain "age < STARTING_WINDOW" case: a
                # unit stuck in a fail/restart loop gets a fresh, small "age"
                # on EVERY cycle (ActiveEnterTimestamp resets each restart),
                # so without this ordering it would read as "Starting"
                # forever instead of "Reconnecting", no matter how many times
                # it has actually failed.
                PUBLISHER_STATE="RECONNECTING"
                PUBLISHER_DETAIL="process restarted; waiting for data flow to resume"
            elif [ "$age" -lt "$BLUESTREAM_PUBLISHER_STARTING_WINDOW" ]; then
                PUBLISHER_STATE="STARTING"
                PUBLISHER_DETAIL="connecting to the destination"
            else
                PUBLISHER_STATE="STARTING"
                PUBLISHER_DETAIL="connecting to the destination"
            fi
            ;;
        activating)
            PUBLISHER_STATE="STARTING"
            PUBLISHER_DETAIL="starting"
            ;;
        failed)
            PUBLISHER_STATE="FAILED"
            PUBLISHER_DETAIL="$(publisher_last_failure_reason "$unit")"
            ;;
        *)
            if journalctl -u "$unit" -n 10 --no-pager 2>/dev/null | grep -q 'start-limit-hit'; then
                PUBLISHER_STATE="FAILED"
                PUBLISHER_DETAIL="restart limit reached; manual restart required (restart the stream/playlist)"
            else
                PUBLISHER_STATE="RECONNECTING"
                PUBLISHER_DETAIL="waiting to retry"
            fi
            ;;
    esac
    return 0
}

# end of publisher.sh
