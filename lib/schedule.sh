#!/usr/bin/env bash
# BlueStream Relay Pro - one-time playlist start scheduling (GUI-8A).
#
# A schedule performs the equivalent of pressing "Start" on a playlist at one
# specified future instant. It never schedules a Stop and never recurs.
#
# Model (mirrors relays/playlists - narrow, root-controlled, no generic cron):
#   sidecar  /etc/bluestream/playlists/<name>.schedule   root:root 0600
#            START_AT=<unix epoch, UTC>
#            START_AT_ISO=<original ISO-8601 w/ offset, display only>
#            CREATED=<ts>
#   timer    /etc/systemd/system/bluestream-schedule-<name>.timer  (generated)
#            OnCalendar=<absolute UTC instant>  Persistent=false
#            Unit=bluestream-playlist-start@<name>.service
#
# MISSED-TIME SAFETY: the timer is Persistent=FALSE on purpose. A broadcast
# playlist scheduled for a specific instant must NOT suddenly start hours late
# just because the VPS was offline at that instant - no catch-up programming.
#   * a FUTURE schedule survives a normal reboot (the unit is enabled) and
#     fires normally if the machine is up at the scheduled time;
#   * if the machine was offline and boots AFTER the scheduled time, the timer
#     does NOT fire (Persistent=false), so nothing starts late;
#   * the sidecar is left in place so schedule_get / the UI can report a
#     "missed" state (START_AT already in the past) - the operator then
#     Changes or Cancels it. Opening the web page never deletes that evidence.
#   * as defence-in-depth against a pathological very-late fire, the start
#     wrapper also refuses to run playlist_start once START_AT is more than
#     BLUESTREAM_SCHEDULE_GRACE seconds in the past.
#
# The timer's target service (bluestream-playlist-start@.service, an installed
# oneshot TEMPLATE) runs run-playlist-start.sh, which calls the authoritative
# playlist_start, is a safe no-op if the playlist is already running, fails
# safely if the playlist no longer exists, and (on an on-time start) removes
# its own timer + sidecar. No browser-supplied shell fragment or command is
# ever scheduled - only a validated playlist ID and a validated absolute
# instant.
#
# BlueStream Relay Pro 0.1.0 (foundation). See LICENSE for terms.

if [ -n "${BLUESTREAM_SCHEDULE_LOADED:-}" ]; then
    return 0 2>/dev/null || exit 0
fi
BLUESTREAM_SCHEDULE_LOADED=1

SCHEDULE_START_AT=""
SCHEDULE_START_AT_ISO=""
SCHEDULE_CREATED=""

# Absolute epoch bounds: roughly [2020-01-01, 2100-01-01). Digits only.
BLUESTREAM_SCHEDULE_EPOCH_MIN=1577836800
BLUESTREAM_SCHEDULE_EPOCH_MAX=4102444800

# How many seconds past START_AT a one-time start may still fire. With
# Persistent=false a normal fire is within seconds; this only blocks a
# pathological very-late fire. Never used to launch catch-up programming.
BLUESTREAM_SCHEDULE_GRACE="${BLUESTREAM_SCHEDULE_GRACE:-600}"

bs_valid_epoch() {
    case "$1" in
        ''|*[!0-9]*) return 1 ;;
    esac
    [ "$1" -ge "$BLUESTREAM_SCHEDULE_EPOCH_MIN" ] || return 1
    [ "$1" -le "$BLUESTREAM_SCHEDULE_EPOCH_MAX" ] || return 1
    return 0
}

# Display-only ISO-8601 string: a bounded, safe subset (digits, '-', ':', 'T',
# ' ', '.', '+', 'Z'). Never parsed as code; the authoritative instant is the
# epoch, which is independently revalidated.
bs_valid_schedule_iso() {
    local s="$1"
    [ -n "$s" ] || return 1
    [ "${#s}" -le 40 ] || return 1
    case "$s" in
        *[!0-9T:.Zz+-]*|*' '*) return 1 ;;
    esac
    return 0
}

schedule_sidecar_path() {
    printf '%s/%s.schedule' "$BLUESTREAM_PLAYLIST_CONF_DIR" "$1"
}

schedule_timer_name() {
    printf 'bluestream-schedule-%s.timer' "$1"
}

schedule_timer_path() {
    printf '/etc/systemd/system/%s' "$(schedule_timer_name "$1")"
}

# Load a playlist's schedule sidecar into SCHEDULE_*. Returns 1 when none.
schedule_load() {
    local name="$1" conf
    SCHEDULE_START_AT=""; SCHEDULE_START_AT_ISO=""; SCHEDULE_CREATED=""
    conf="$(schedule_sidecar_path "$name")"
    [ -f "$conf" ] || return 1
    BLUESTREAM_CFG_FILE="$conf"
    BLUESTREAM_CFG_KEYS="START_AT START_AT_ISO CREATED"
    BLUESTREAM_CFG_PREFIX="SCHEDULE_"
    bs_parse_kv_file
    bs_valid_epoch "$SCHEDULE_START_AT" || return 1
    return 0
}

# schedule_state <epoch> -> prints "pending" (still in the future) or "missed"
# (the instant has already passed). A missed schedule never runs late.
schedule_state() {
    local epoch="$1" now
    bs_valid_epoch "$epoch" || { printf 'missed'; return 0; }
    now="$(date +%s)"
    if [ "$epoch" -gt "$now" ]; then printf 'pending'; else printf 'missed'; fi
}

# Remove ONLY this playlist's generated timer unit, keeping the schedule
# sidecar in place as "missed" evidence for the UI. Used by the start wrapper
# when a fire arrives after the grace window.
schedule_expire_timer() {
    local name="$1" timer
    bs_require_root
    bs_valid_name "$name" || return 1
    timer="$(schedule_timer_name "$name")"
    systemctl disable --now "$timer" 2>/dev/null || true
    systemctl stop "$timer" 2>/dev/null || true
    rm -f "$(schedule_timer_path "$name")"
    systemctl daemon-reload 2>/dev/null || true
    systemctl reset-failed "$timer" 2>/dev/null || true
    return 0
}

schedule_save_sidecar() {
    local name="$1"
    local tmp="$BLUESTREAM_PLAYLIST_CONF_DIR/.$name.schedule.tmp.$$"
    local dest
    dest="$(schedule_sidecar_path "$name")"
    if ! {
        printf '# BlueStream Relay Pro one-time playlist schedule (root-only)\n'
        printf 'START_AT=%s\n' "$SCHEDULE_START_AT"
        printf 'START_AT_ISO=%s\n' "$SCHEDULE_START_AT_ISO"
        printf 'CREATED=%s\n' "$SCHEDULE_CREATED"
    } > "$tmp"; then
        rm -f "$tmp"; return 1
    fi
    chmod 0600 "$tmp" || { rm -f "$tmp"; return 1; }
    chown root:root "$tmp" 2>/dev/null || true
    mv -f "$tmp" "$dest" || { rm -f "$tmp"; return 1; }
    chmod 0600 "$dest" || { rm -f "$dest"; return 1; }
    return 0
}

schedule_write_timer_unit() {
    local name="$1" epoch="$2" oncal path tmp
    oncal="$(date -u -d "@$epoch" '+%Y-%m-%d %H:%M:%S UTC' 2>/dev/null)" || return 1
    [ -n "$oncal" ] || return 1
    path="$(schedule_timer_path "$name")"
    tmp="/etc/systemd/system/.bluestream-schedule-$name.timer.tmp.$$"
    if ! {
        printf '# BlueStream Relay Pro - generated one-time playlist start timer.\n'
        printf '# Managed by BlueStream (schedule_set / schedule_clear); do not edit.\n'
        printf '[Unit]\n'
        printf 'Description=BlueStream one-time playlist start for %s\n' "$name"
        printf '\n[Timer]\n'
        printf 'OnCalendar=%s\n' "$oncal"
        # Persistent=false: a start missed while the machine was offline is
        # NOT run late on the next boot (no catch-up broadcast).
        printf 'Persistent=false\n'
        printf 'AccuracySec=1s\n'
        printf 'RemainAfterElapse=no\n'
        printf 'Unit=bluestream-playlist-start@%s.service\n' "$name"
        printf '\n[Install]\n'
        printf 'WantedBy=timers.target\n'
    } > "$tmp"; then
        rm -f "$tmp"; return 1
    fi
    chmod 0644 "$tmp" || { rm -f "$tmp"; return 1; }
    chown root:root "$tmp" 2>/dev/null || true
    mv -f "$tmp" "$path" || { rm -f "$tmp"; return 1; }
    chmod 0644 "$path" || return 1
    return 0
}

# schedule_set <playlist> <epoch-utc> <iso-display>
#   0 ok | 1 invalid name | 2 playlist not found | 3 invalid timestamp |
#   4 timestamp is in the past | 5 write / systemd failure
schedule_set() {
    local name="$1" epoch="$2" iso="$3" now timer
    bs_require_root
    bs_valid_name "$name" || return 1
    playlist_exists "$name" || return 2
    bs_valid_epoch "$epoch" || return 3
    bs_valid_schedule_iso "$iso" || return 3
    now="$(date +%s)"
    [ "$epoch" -gt "$now" ] || return 4

    SCHEDULE_START_AT="$epoch"
    SCHEDULE_START_AT_ISO="$iso"
    SCHEDULE_CREATED="$(bs_now_ts)"
    schedule_save_sidecar "$name" || return 5
    schedule_write_timer_unit "$name" "$epoch" || { schedule_clear "$name" >/dev/null 2>&1; return 5; }

    systemctl daemon-reload 2>/dev/null || true
    timer="$(schedule_timer_name "$name")"
    # enable (survives reboot) + start (arms it for this boot). A replaced
    # schedule reuses the same unit name, so this is safe to call repeatedly.
    systemctl reenable "$timer" 2>/dev/null || systemctl enable "$timer" 2>/dev/null || true
    systemctl start "$timer" 2>/dev/null || { schedule_clear "$name" >/dev/null 2>&1; return 5; }
    return 0
}

# schedule_clear <playlist> - idempotent; removes ONLY this playlist's own
# schedule sidecar and generated timer unit.
#   0 ok | 1 invalid name
schedule_clear() {
    local name="$1" timer
    bs_require_root
    bs_valid_name "$name" || return 1
    timer="$(schedule_timer_name "$name")"
    systemctl disable --now "$timer" 2>/dev/null || true
    systemctl stop "$timer" 2>/dev/null || true
    rm -f "$(schedule_timer_path "$name")"
    rm -f "$(schedule_sidecar_path "$name")"
    systemctl daemon-reload 2>/dev/null || true
    systemctl reset-failed "$timer" 2>/dev/null || true
    return 0
}
