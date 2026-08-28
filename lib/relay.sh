#!/usr/bin/env bash
# BlueStream Relay Pro - relay management.
#
# Config CRUD, systemd lifecycle, ffmpeg argument construction (stream
# copy/remux only - never transcodes by default), status and health.
#
# BlueStream Relay Pro 0.1.0 (foundation). See LICENSE for terms.

if [ -n "${BLUESTREAM_RELAY_LOADED:-}" ]; then
    return 0 2>/dev/null || exit 0
fi
BLUESTREAM_RELAY_LOADED=1

RELAY_NAME=""; RELAY_TYPE=""; RELAY_URL=""; RELAY_LOOP="no"
RELAY_RESTART_SEC="5"; RELAY_ENABLED="no"; RELAY_NOTE=""; RELAY_CREATED=""

# ---------------------------------------------------------------------------
# Config load / save / validate
# ---------------------------------------------------------------------------
relay_load_config() {
    local name="$1"
    RELAY_NAME=""; RELAY_TYPE=""; RELAY_URL=""; RELAY_LOOP="no"
    RELAY_RESTART_SEC="5"; RELAY_ENABLED="no"; RELAY_NOTE=""; RELAY_CREATED=""
    local conf="$BLUESTREAM_RELAY_CONF_DIR/$name.conf"
    [ -f "$conf" ] || return 1
    BLUESTREAM_CFG_FILE="$conf"
    BLUESTREAM_CFG_KEYS="NAME TYPE URL LOOP RESTART_SEC ENABLED NOTE CREATED"
    BLUESTREAM_CFG_PREFIX="RELAY_"
    bs_parse_kv_file
    [ "$RELAY_NAME" = "$name" ] || return 1
    case "$RELAY_LOOP" in yes|no) ;; *) return 1 ;; esac
    case "$RELAY_ENABLED" in yes|no) ;; *) return 1 ;; esac
    bs_valid_restart_sec "$RELAY_RESTART_SEC" || return 1
    return 0
}

relay_config_validate() {
    bs_valid_name "$RELAY_NAME" || return 1
    bs_valid_type "$RELAY_TYPE" || return 1
    case "$RELAY_TYPE" in
        local-file)
            bs_valid_abs_path "$RELAY_URL" || return 1
            [ -f "$RELAY_URL" ] || return 1
            ;;
        remote-hls|http-file)
            bs_valid_url "$RELAY_URL" || return 1
            case "$RELAY_URL" in http://*|https://*) ;; *) return 1 ;; esac
            ;;
        rtmp)
            bs_valid_url "$RELAY_URL" || return 1
            case "$RELAY_URL" in rtmp://*) ;; *) return 1 ;; esac
            ;;
        rtmps)
            bs_valid_url "$RELAY_URL" || return 1
            case "$RELAY_URL" in rtmps://*|rtmp://*) ;; *) return 1 ;; esac
            ;;
        rtsp)
            bs_valid_url "$RELAY_URL" || return 1
            case "$RELAY_URL" in rtsp://*) ;; *) return 1 ;; esac
            ;;
    esac
    return 0
}

relay_save_config() {
    local name="$1"
    local tmp="$BLUESTREAM_RELAY_CONF_DIR/.$name.conf.tmp.$$"
    {
        printf '# BlueStream Relay Pro relay configuration (root-only)\n'
        printf 'NAME=%s\n' "$RELAY_NAME"
        printf 'TYPE=%s\n' "$RELAY_TYPE"
        printf 'URL=%s\n' "$RELAY_URL"
        printf 'LOOP=%s\n' "$RELAY_LOOP"
        printf 'RESTART_SEC=%s\n' "$RELAY_RESTART_SEC"
        printf 'ENABLED=%s\n' "$RELAY_ENABLED"
        printf 'NOTE=%s\n' "$RELAY_NOTE"
        printf 'CREATED=%s\n' "$RELAY_CREATED"
    } > "$tmp"
    chmod 0600 "$tmp"
    chown root:root "$tmp" 2>/dev/null || true
    mv -f "$tmp" "$BLUESTREAM_RELAY_CONF_DIR/$name.conf"
    chmod 0600 "$BLUESTREAM_RELAY_CONF_DIR/$name.conf"
}

relay_exists() {
    [ -f "$BLUESTREAM_RELAY_CONF_DIR/$1.conf" ]
}

relay_list_names() {
    local f
    shopt -s nullglob
    for f in "$BLUESTREAM_RELAY_CONF_DIR"/*.conf; do
        basename "$f" .conf
    done
    shopt -u nullglob
}

# ---------------------------------------------------------------------------
# ffmpeg argument construction (stream copy / remux)
# ---------------------------------------------------------------------------
relay_build_ffmpeg_args() {
    local input=()
    local output=()
    local url="$RELAY_URL"
    FFMPEG_ARGS=()

    case "$RELAY_TYPE" in
        local-file)
            if [ "$RELAY_LOOP" = "yes" ]; then
                input+=( -re -stream_loop -1 )
            else
                input+=( -re )
            fi
            input+=( -i "$url" )
            ;;
        http-file)
            input+=( -fflags +genpts -reconnect 1 -reconnect_streamed 1 -reconnect_at_eof 1 -reconnect_delay_max 30 )
            if [ "$RELAY_LOOP" = "yes" ]; then
                input+=( -re -stream_loop -1 )
            else
                input+=( -re )
            fi
            input+=( -i "$url" )
            ;;
        remote-hls)
            input+=( -fflags +genpts -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 30 )
            input+=( -i "$url" )
            ;;
        rtmp|rtmps)
            input+=( -rw_timeout 20000000 )
            input+=( -i "$url" )
            ;;
        rtsp)
            input+=( -rtsp_transport tcp -fflags +genpts )
            input+=( -i "$url" )
            ;;
        *)
            return 1
            ;;
    esac

    output=(
        -map 0:v:0
        -map "0:a:0?"
        -c:v copy
        -c:a copy
        -f flv
        "${BLUESTREAM_RTMP_BASE}/${BLUESTREAM_RTMP_APP_RELAY}/${RELAY_NAME}"
    )

    FFMPEG_ARGS=( -nostdin -y -loglevel warning "${input[@]}" "${output[@]}" )
    return 0
}

# ---------------------------------------------------------------------------
# systemd drop-in: honour the per-relay RestartSec value
# ---------------------------------------------------------------------------
relay_sync_dropin() {
    local name="$1"
    local dropin="/etc/systemd/system/$(bs_unit relay "$name").d/override.conf"
    mkdir -p "$(dirname "$dropin")" 2>/dev/null || return 1
    {
        printf '# BlueStream Relay Pro generated override (from relay config RESTART_SEC)\n'
        printf '[Service]\n'
        printf 'RestartSec=%s\n' "$RELAY_RESTART_SEC"
    } > "$dropin"
    chmod 0644 "$dropin"
    systemctl daemon-reload 2>/dev/null || true
    return 0
}

# ---------------------------------------------------------------------------
# systemd lifecycle
# ---------------------------------------------------------------------------
relay_start() {
    local name="$1"
    bs_require_root
    relay_load_config "$name" || bs_die "Relay '$name' not found"
    relay_config_validate || bs_die "Relay '$name' failed configuration validation"
    bs_ensure_hls_dir relay "$name" || bs_die "Could not prepare HLS output directory"
    relay_sync_dropin "$name"
    systemctl start "$(bs_unit relay "$name")" || bs_die "Failed to start relay '$name'"
    bs_ok "Relay '$name' started"
}

relay_stop() {
    local name="$1"
    bs_require_root
    relay_load_config "$name" || bs_die "Relay '$name' not found"
    systemctl stop "$(bs_unit relay "$name")" 2>/dev/null
    bs_ok "Relay '$name' stopped"
}

relay_restart() {
    local name="$1"
    bs_require_root
    relay_load_config "$name" || bs_die "Relay '$name' not found"
    relay_config_validate || bs_die "Relay '$name' failed configuration validation"
    bs_ensure_hls_dir relay "$name" || bs_die "Could not prepare HLS output directory"
    relay_sync_dropin "$name"
    systemctl restart "$(bs_unit relay "$name")" || bs_die "Failed to restart relay '$name'"
    bs_ok "Relay '$name' restarted"
}

relay_enable() {
    local name="$1"
    bs_require_root
    relay_load_config "$name" || bs_die "Relay '$name' not found"
    systemctl enable "$(bs_unit relay "$name")" 2>/dev/null || bs_die "Failed to enable relay '$name' at boot"
    RELAY_ENABLED="yes"
    relay_save_config "$name"
    bs_ok "Relay '$name' enabled at boot"
}

relay_disable() {
    local name="$1"
    bs_require_root
    relay_load_config "$name" || bs_die "Relay '$name' not found"
    systemctl disable "$(bs_unit relay "$name")" 2>/dev/null || true
    RELAY_ENABLED="no"
    relay_save_config "$name"
    bs_ok "Relay '$name' disabled at boot"
}

relay_remove() {
    local name="$1"
    bs_require_root
    relay_load_config "$name" || bs_die "Relay '$name' not found"
    bs_confirm "Remove relay '$name'? This stops it and deletes its HLS output and configuration." || { bs_info "Cancelled."; return 1; }
    systemctl disable "$(bs_unit relay "$name")" 2>/dev/null || true
    systemctl stop "$(bs_unit relay "$name")" 2>/dev/null || true
    rm -f "$BLUESTREAM_RELAY_CONF_DIR/$name.conf"
    rm -rf "/etc/systemd/system/$(bs_unit relay "$name").d"
    rm -rf "$(bs_hls_dir_for relay "$name")"
    bs_ok "Relay '$name' removed"
}

# ---------------------------------------------------------------------------
# Listing / status / URLs / logs / health / probe
# ---------------------------------------------------------------------------
relay_list() {
    local names
    names="$(relay_list_names)"
    if [ -z "$names" ]; then
        bs_info "No relays configured yet."
        return 0
    fi
    printf '%-22s %-12s %-10s %-9s %s\n' "NAME" "TYPE" "SERVICE" "HEALTH" "SOURCE"
    local name
    for name in $names; do
        relay_list_line "$name"
    done
}

relay_list_line() {
    local name="$1" state health src
    relay_load_config "$name" || return 1
    health_state relay "$name"
    state="$(systemctl is-active "$(bs_unit relay "$name")" 2>/dev/null)"
    [ -n "$state" ] || state="unknown"
    src="$(bs_redact_url "$RELAY_URL")"
    printf '%-22s %-12s %-10s %-9s %s\n' "$name" "$RELAY_TYPE" "$state" "$HEALTH_STATE" "$src"
}

relay_status_detail() {
    local name="$1"
    bs_require_root
    relay_load_config "$name" || bs_die "Relay '$name' not found"
    local unit hlsdir pid user
    unit="$(bs_unit relay "$name")"
    hlsdir="$(bs_hls_dir_for relay "$name")"
    health_state relay "$name"

    bs_step "Relay: $name"
    printf '  Type            : %s\n' "$RELAY_TYPE"
    printf '  Source          : %s\n' "$(bs_redact_url "$RELAY_URL")"
    printf '  Loop            : %s\n' "$RELAY_LOOP"
    printf '  Restart sec     : %s\n' "$RELAY_RESTART_SEC"
    printf '  Enabled at boot : %s\n' "$RELAY_ENABLED"
    printf '  Note            : %s\n' "${RELAY_NOTE:--}"
    printf '  Systemd unit    : %s\n' "$unit"
    printf '  Active state    : %s\n' "$(systemctl is-active "$unit" 2>/dev/null)"
    printf '  Boot enabled    : %s\n' "$(systemctl is-enabled "$unit" 2>/dev/null)"
    printf '  Health          : %s\n' "$(health_color)"
    printf '  Health detail   : %s\n' "$HEALTH_DETAILS"
    pid="$(systemctl show -p MainPID --value "$unit" 2>/dev/null)"
    if [ -n "$pid" ] && [ "$pid" != "0" ]; then
        user="$(ps -o user= -p "$pid" 2>/dev/null | tr -d ' ')"
        printf '  FFmpeg PID      : %s\n' "$pid"
        printf '  FFmpeg user     : %s\n' "${user:-unknown}"
    else
        printf '  FFmpeg process  : not running\n'
    fi
    printf '  M3U8 age (s)    : %s\n' "$(bs_hls_age_seconds "$hlsdir")"
    printf '  Segments in list: %s\n' "$(bs_hls_segment_count "$hlsdir")"
    printf '  HLS dir         : %s\n' "$hlsdir"
}

relay_show_urls() {
    local name="$1"
    relay_load_config "$name" || bs_die "Relay '$name' not found"
    printf 'M3U8:\n  %s\n' "$(bs_relay_m3u8_url "$name")"
    printf 'Player:\n  %s\n' "$(bs_relay_player_url "$name")"
}

relay_show_m3u8() {
    local name="$1"
    relay_load_config "$name" || bs_die "Relay '$name' not found"
    printf '%s\n' "$(bs_relay_m3u8_url "$name")"
}

relay_show_player() {
    local name="$1"
    relay_load_config "$name" || bs_die "Relay '$name' not found"
    printf '%s\n' "$(bs_relay_player_url "$name")"
}

relay_logs() {
    local name="$1" lines="${2:-100}"
    bs_require_root
    relay_load_config "$name" || bs_die "Relay '$name' not found"
    journalctl -u "$(bs_unit relay "$name")" -n "$lines" --no-pager
}

relay_health() {
    local name="$1"
    relay_load_config "$name" || bs_die "Relay '$name' not found"
    health_state relay "$name"
    printf '%s - %s\n' "$(health_color)" "$HEALTH_DETAILS"
    [ "$HEALTH_STATE" = "HEALTHY" ] || return 1
    return 0
}

# ---------------------------------------------------------------------------
# Create / edit / change source / probe
# ---------------------------------------------------------------------------
relay_select_interactive() {
    # Sets RELAY_SELECTED to a relay name chosen from a numbered menu.
    local names name i j choice
    names="$(relay_list_names)"
    if [ -z "$names" ]; then
        bs_warn "No relays configured yet."
        return 1
    fi
    RELAY_SELECTED=""
    i=0
    for name in $names; do
        i=$((i + 1))
        printf '  %2d) %s\n' "$i" "$name"
    done
    printf 'Select relay [1-%d, 0 to cancel]: ' "$i"
    read -r choice || return 1
    case "$choice" in
        ''|*[!0-9]*) return 1 ;;
    esac
    if [ "$choice" -ge 1 ] && [ "$choice" -le "$i" ] 2>/dev/null; then
        j=0
        for name in $names; do
            j=$((j + 1))
            if [ "$j" -eq "$choice" ]; then
                RELAY_SELECTED="$name"
                return 0
            fi
        done
    fi
    return 1
}

relay_create() {
    bs_require_root
    local name="" type="" url="" loop="no" restart="5" note=""
    local choice=""

    bs_prompt name "Relay name (lowercase letters, digits, '-' or '_')" "" || return 1
    bs_valid_name "$name" || { bs_error "Invalid relay name. Use [a-z0-9_-], start with a letter or digit, max 48 chars."; return 1; }
    relay_exists "$name" && { bs_error "Relay '$name' already exists."; return 1; }

    bs_select "Select source type" choice \
        "local-file | local media file (loops 24/7)" \
        "remote-hls | remote HLS/M3U8 stream" \
        "rtmp       | remote RTMP stream" \
        "rtmps      | remote RTMPS stream" \
        "rtsp       | remote RTSP stream (TCP transport)" \
        "http-file  | direct HTTP/HTTPS media file" || return 1
    type="${choice%% *}"

    case "$type" in
        local-file)
            bs_prompt url "Absolute path to the local media file" "" || return 1
            ;;
        *)
            bs_prompt url "Source URL (https://..., rtmp://..., rtmps://..., rtsp://...)" "" || return 1
            ;;
    esac

    if [ "$type" = "local-file" ] || [ "$type" = "http-file" ]; then
        if bs_confirm "Loop this file continuously (recommended for 24/7)?"; then
            loop="yes"
        else
            loop="no"
        fi
    fi

    bs_prompt restart "Restart delay in seconds after a crash (1-300)" "5" || return 1
    bs_valid_restart_sec "$restart" || { bs_error "Invalid restart delay."; return 1; }

    bs_prompt note "Optional note (internal only)" "" || return 1

    RELAY_NAME="$name"
    RELAY_TYPE="$type"
    RELAY_URL="$url"
    RELAY_LOOP="$loop"
    RELAY_RESTART_SEC="$restart"
    RELAY_ENABLED="no"
    RELAY_NOTE="$note"
    RELAY_CREATED="$(bs_now_ts)"

    relay_config_validate || { bs_error "Configuration failed validation."; return 1; }

    if bs_have_ffprobe; then
        if bs_confirm "Probe the source now for compatibility?"; then
            if [ "$type" = "local-file" ]; then
                probe_media_path "$url" && probe_compat_report "$name" || bs_warn "Probe failed; the source may be unreachable or is not a media source."
            else
                probe_url "$url" && probe_compat_report "$name" || bs_warn "Probe failed; the source may be unreachable or is not a media source."
            fi
        fi
    else
        bs_warn "ffprobe is not installed; compatibility probing skipped."
    fi

    mkdir -p "$BLUESTREAM_RELAY_CONF_DIR" 2>/dev/null
    relay_save_config "$name"
    bs_ok "Relay '$name' created."
    printf '  M3U8   : %s\n' "$(bs_relay_m3u8_url "$name")"
    printf '  Player : %s\n' "$(bs_relay_player_url "$name")"

    if bs_confirm "Start relay '$name' now?"; then
        relay_start "$name"
    fi
    if bs_confirm "Enable relay '$name' at boot?"; then
        relay_enable "$name"
    fi
    return 0
}

relay_edit() {
    local name="${1:-}"
    bs_require_root
    if [ -z "$name" ]; then
        relay_select_interactive || return 1
        name="$RELAY_SELECTED"
    fi
    relay_load_config "$name" || bs_die "Relay '$name' not found"

    bs_step "Editing relay '$name'"
    printf '  Loop        : %s\n' "$RELAY_LOOP"
    printf '  Restart sec : %s\n' "$RELAY_RESTART_SEC"
    printf '  Note        : %s\n' "${RELAY_NOTE:--}"

    if bs_confirm "Change LOOP setting? (loop file continuously)"; then
        if bs_confirm "Loop the file continuously?"; then
            RELAY_LOOP="yes"
        else
            RELAY_LOOP="no"
        fi
    fi

    local restart
    restart="$RELAY_RESTART_SEC"
    if bs_confirm "Change restart delay? (current: $RELAY_RESTART_SEC s)"; then
        bs_prompt restart "Restart delay in seconds (1-300)" "$RELAY_RESTART_SEC" || return 1
        bs_valid_restart_sec "$restart" || { bs_error "Invalid restart delay."; return 1; }
        RELAY_RESTART_SEC="$restart"
    fi

    local note
    if bs_confirm "Change note? (current: ${RELAY_NOTE:--})"; then
        bs_prompt note "Note" "$RELAY_NOTE" || return 1
        RELAY_NOTE="$note"
    fi

    relay_save_config "$name"
    bs_ok "Relay '$name' updated."
    if bs_unit_active "$(bs_unit relay "$name")" && bs_confirm "Restart relay '$name' to apply changes?"; then
        relay_restart "$name"
    fi
    return 0
}

relay_change_source() {
    local name="${1:-}"
    bs_require_root
    if [ -z "$name" ]; then
        relay_select_interactive || return 1
        name="$RELAY_SELECTED"
    fi
    relay_load_config "$name" || bs_die "Relay '$name' not found"

    bs_step "Changing source for relay '$name'"
    printf '  Current source : %s\n' "$(bs_redact_url "$RELAY_URL")"

    local url=""
    if [ "$RELAY_TYPE" = "local-file" ]; then
        bs_prompt url "New absolute path to the local media file" "" || return 1
    else
        bs_prompt url "New source URL" "" || return 1
    fi
    [ -n "$url" ] || { bs_error "Empty value."; return 1; }

    RELAY_URL="$url"
    relay_config_validate || { bs_error "New source failed validation."; return 1; }

    if bs_have_ffprobe && bs_confirm "Probe the new source for compatibility?"; then
        if [ "$RELAY_TYPE" = "local-file" ]; then
            probe_media_path "$url" && probe_compat_report "$name" || bs_warn "Probe failed."
        else
            probe_url "$url" && probe_compat_report "$name" || bs_warn "Probe failed."
        fi
    fi

    relay_save_config "$name"
    bs_ok "Source updated for relay '$name'."
    if bs_unit_active "$(bs_unit relay "$name")" && bs_confirm "Restart relay '$name' to switch to the new source?"; then
        relay_restart "$name"
    fi
    return 0
}

relay_probe_source() {
    local name="${1:-}"
    if [ -z "$name" ]; then
        relay_select_interactive || return 1
        name="$RELAY_SELECTED"
    fi
    relay_load_config "$name" || bs_die "Relay '$name' not found"
    bs_require_cmd ffprobe
    if [ "$RELAY_TYPE" = "local-file" ]; then
        probe_media_path "$RELAY_URL" || bs_die "Probe failed."
    else
        probe_url "$RELAY_URL" || bs_die "Probe failed."
    fi
    probe_compat_report "$name"
    return 0
}

relay_estimate_bandwidth() {
    local name="${1:-}"
    local bps mbps viewers total
    if [ -z "$name" ]; then
        relay_select_interactive || return 1
        name="$RELAY_SELECTED"
    fi
    relay_load_config "$name" || bs_die "Relay '$name' not found"

    bps="$(probe_pick_bitrate)"
    if [ "$bps" = "0" ] || [ -z "$bps" ]; then
        bs_warn "Source bitrate could not be determined from probe data."
        bs_prompt bps "Approximate source bitrate in Mbps (e.g. 4, 8, 15)" "" || return 1
        case "$bps" in ''|*[!0-9.]*) bs_error "Invalid bitrate."; return 1 ;; esac
        mbps="$bps"
    else
        mbps="$(bandwidth_mbps_from_bps "$bps")"
        bs_info "Detected source bitrate: ${mbps} Mbps"
    fi

    bs_prompt viewers "Expected simultaneous viewers" "" || return 1
    case "$viewers" in ''|*[!0-9]*) bs_error "Invalid viewer count."; return 1 ;; esac

    total="$(estimate_bandwidth "$mbps" "$viewers")"
    bs_step "Estimated outbound bandwidth for relay '$name'"
    printf '  Source bitrate  : %s Mbps\n' "$mbps"
    printf '  Viewers         : %s\n' "$viewers"
    printf '  Approx outbound : %s Mbps\n' "$total"
    bandwidth_show_explanation
    return 0
}



