#!/usr/bin/env bash
# BlueStream Relay Pro - playlist management.
#
# Playlists are ordered lists of managed media files. They play sequentially
# and loop continuously through FFmpeg's concat demuxer with stream copy.
# Before starting, all entries are probed and compatibility mismatches are
# reported. Playlist operations never delete media files.
#
# BlueStream Relay Pro 0.1.0 (foundation). See LICENSE for terms.

if [ -n "${BLUESTREAM_PLAYLIST_LOADED:-}" ]; then
    return 0 2>/dev/null || exit 0
fi
BLUESTREAM_PLAYLIST_LOADED=1

PLAYLIST_NAME=""
PLAYLIST_ENABLED="no"
PLAYLIST_FILES=()
PLAYLIST_CONF_FILE=""

# Conservative maximum entries per playlist (GUI-1D.1).  There was no engine
# limit before; this bounds privileged argv/config writes so the bridge and
# the Flask form share one safe ceiling (playlist_create fails closed above
# it).  64 is generous for ordinary sequential promo/looping channels while
# keeping every bridge argv line and config file small.
BLUESTREAM_PLAYLIST_MAX_ITEMS=64

# ---------------------------------------------------------------------------
# Config load / save / validation
# ---------------------------------------------------------------------------
playlist_load_config() {
    local name="$1"
    PLAYLIST_NAME=""
    PLAYLIST_ENABLED="no"
    PLAYLIST_FILES=()
    PLAYLIST_CONF_FILE="$BLUESTREAM_PLAYLIST_CONF_DIR/$name.playlist"
    [ -f "$PLAYLIST_CONF_FILE" ] || return 1
    local line key val
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            ''|\#*) continue ;;
        esac
        case "$line" in
            *=*)
                key="${line%%=*}"
                val="${line#*=}"
                key="${key#"${key%%[![:space:]]*}"}"
                case "$key" in
                    NAME)    PLAYLIST_NAME="$val" ;;
                    ENABLED) PLAYLIST_ENABLED="$val" ;;
                esac
                ;;
            *)
                # playlist entry: managed media file name
                line="${line#"${line%%[![:space:]]*}"}"
                line="${line%"${line##*[![:space:]]}"}"
                case "$line" in
                    ''|.*|*[!A-Za-z0-9._-]*|*'..'*) continue ;;
                esac
                PLAYLIST_FILES+=( "$line" )
                ;;
        esac
    done < "$PLAYLIST_CONF_FILE"
    [ "$PLAYLIST_NAME" = "$name" ] || return 1
    case "$PLAYLIST_ENABLED" in yes|no) ;; *) return 1 ;; esac
    return 0
}

playlist_save_config() {
    local name="$1"
    local tmp="$BLUESTREAM_PLAYLIST_CONF_DIR/.$name.playlist.tmp.$$"
    local dest="$BLUESTREAM_PLAYLIST_CONF_DIR/$name.playlist"
    # Fail closed (GUI-1D.1): any critical write step (temp creation, chmod,
    # rename into place, final mode) must succeed or the function returns
    # non-zero and cleans up its temp file, so a partial config is never
    # presented as saved.  Playlist creation/editing callers check this return.
    if ! {
        printf '# BlueStream Relay Pro playlist (root-only)\n'
        printf 'NAME=%s\n' "$PLAYLIST_NAME"
        printf 'ENABLED=%s\n' "$PLAYLIST_ENABLED"
        printf '# entries (media files from %s)\n' "$BLUESTREAM_MEDIA_DIR"
        local f
        for f in "${PLAYLIST_FILES[@]}"; do
            printf '%s\n' "$f"
        done
    } > "$tmp"; then
        rm -f "$tmp"
        return 1
    fi
    chmod 0600 "$tmp" || { rm -f "$tmp"; return 1; }
    chown root:root "$tmp" 2>/dev/null || true
    mv -f "$tmp" "$dest" || { rm -f "$tmp"; return 1; }
    chmod 0600 "$dest" || { rm -f "$dest"; return 1; }
    return 0
}

playlist_exists() {
    [ -f "$BLUESTREAM_PLAYLIST_CONF_DIR/$1.playlist" ]
}

playlist_list_names() {
    local f
    shopt -s nullglob
    for f in "$BLUESTREAM_PLAYLIST_CONF_DIR"/*.playlist; do
        basename "$f" .playlist
    done
    shopt -u nullglob
}

playlist_entries_valid() {
    local f
    for f in "${PLAYLIST_FILES[@]}"; do
        [ -f "$BLUESTREAM_MEDIA_DIR/$f" ] || return 1
    done
    return 0
}

# ---------------------------------------------------------------------------
# Compatibility checking
# ---------------------------------------------------------------------------
# Normalise an ffprobe frame-rate value ("25/1", "30000/1001", "30", "") to a
# decimal fps string for safe numeric comparison. Prints empty on unknown.
playlist_fps_numeric() {
    local raw="$1" num den
    case "$raw" in
        ''|0|0/0|0/1|N/A) return 1 ;;
    esac
    case "$raw" in
        */*)
            num="${raw%%/*}"
            den="${raw##*/}"
            case "$den" in ''|0) return 1 ;; esac
            case "$num" in ''|*[!0-9]*) return 1 ;; esac
            awk -v n="$num" -v d="$den" 'BEGIN{ printf "%.3f", n/d }'
            ;;
        *[0-9]*)
            case "$raw" in *[!0-9.]*) return 1 ;; esac
            printf '%s' "$raw"
            ;;
        *)
            return 1
            ;;
    esac
    return 0
}

playlist_check_compat() {
    local first_v="" first_w="" first_h="" first_a="" first_asr="" first_ch=""
    local first_fps="" first_fps_display="" cur_fps=""
    local first_set=0 warn=0 f
    for f in "${PLAYLIST_FILES[@]}"; do
        local p="$BLUESTREAM_MEDIA_DIR/$f"
        if ! probe_media_path "$p"; then
            bs_warn "'$f' could not be probed; skipping compatibility comparison."
            warn=1
            continue
        fi
        if [ "$first_set" -eq 0 ]; then
            first_v="$PV_CODEC"; first_w="$PV_WIDTH"; first_h="$PV_HEIGHT"
            first_a="$PA_CODEC"; first_asr="$PA_SAMPLE_RATE"; first_ch="$PA_CHANNELS"
            first_fps="$(playlist_fps_numeric "$PV_FPS_RAW")"
            first_fps_display="$PV_FPS"
            first_set=1
            continue
        fi
        if [ "$PV_CODEC" != "$first_v" ]; then
            bs_warn "'$f' video codec '$PV_CODEC' differs from '$first_v'."
            warn=1
        fi
        if [ -n "${PV_WIDTH:-}" ] && [ -n "$first_w" ] && { [ "$PV_WIDTH" != "$first_w" ] || [ "$PV_HEIGHT" != "$first_h" ]; }; then
            bs_warn "'$f' resolution ${PV_WIDTH}x${PV_HEIGHT} differs from ${first_w}x${first_h}."
            warn=1
        fi
        cur_fps="$(playlist_fps_numeric "$PV_FPS_RAW")"
        if [ -n "$first_fps" ] && [ -n "$cur_fps" ] && \
            ! awk -v a="$first_fps" -v b="$cur_fps" 'BEGIN{ d=a-b; if (d<0) d=-d; exit !(d>1.0) }'; then
            bs_warn "'$f' frame rate '${PV_FPS:-unknown}' differs significantly from '${first_fps_display:-unknown}'."
            warn=1
        fi
        if [ "$PA_CODEC" != "$first_a" ]; then
            bs_warn "'$f' audio codec '${PA_CODEC:-none}' differs from '${first_a:-none}'."
            warn=1
        fi
        if [ -n "${PA_SAMPLE_RATE:-}" ] && [ -n "$first_asr" ] && [ "$PA_SAMPLE_RATE" != "$first_asr" ]; then
            bs_warn "'$f' sample rate '$PA_SAMPLE_RATE' differs from '$first_asr'."
            warn=1
        fi
        if [ -n "${PA_CHANNELS:-}" ] && [ -n "$first_ch" ] && [ "$PA_CHANNELS" != "$first_ch" ]; then
            bs_warn "'$f' channel count '$PA_CHANNELS' differs from '$first_ch'."
            warn=1
        fi
        if [ "$PV_CODEC" != "h264" ] || { [ -n "$PA_CODEC" ] && [ "$PA_CODEC" != "aac" ]; }; then
            bs_warn "'$f' is not H.264+AAC ($PV_CODEC / ${PA_CODEC:-none}); player compatibility may vary."
            warn=1
        fi
    done

    if [ "$first_set" -eq 1 ] && [ "$warn" -eq 0 ]; then
        bs_ok "All playlist entries are compatible (${first_v} ${first_w}x${first_h} / ${first_a:-none})."
    elif [ "$warn" -ne 0 ]; then
        bs_warn "Playlist entries have compatibility differences. Prefer uniform H.264 + AAC media."
    fi
    return "$warn"
}

# ---------------------------------------------------------------------------
# Concat file + ffmpeg argument construction
# ---------------------------------------------------------------------------
playlist_write_concat_file() {
    local concat_file="$BLUESTREAM_RUN_DIR/$PLAYLIST_NAME.concat.txt"
    mkdir -p "$BLUESTREAM_RUN_DIR" 2>/dev/null || return 1
    local tmp="$BLUESTREAM_RUN_DIR/.$PLAYLIST_NAME.concat.tmp.$$"
    local f
    : > "$tmp"
    for f in "${PLAYLIST_FILES[@]}"; do
        printf "file '%s'\n" "$BLUESTREAM_MEDIA_DIR/$f" >> "$tmp"
    done
    mv -f "$tmp" "$concat_file"
    chown "${BLUESTREAM_USER}:${BLUESTREAM_GROUP}" "$concat_file" 2>/dev/null || true
    chmod 0640 "$concat_file"
    return 0
}

playlist_build_ffmpeg_args() {
    local concat_file="$BLUESTREAM_RUN_DIR/$PLAYLIST_NAME.concat.txt"
    FFMPEG_ARGS=(
        -nostdin -y -loglevel warning
        -re -stream_loop -1 -f concat -safe 0 -i "$concat_file"
        -map 0:v:0 -map "0:a:0?"
        -c:v copy -c:a copy
        -f flv
        "${BLUESTREAM_RTMP_BASE}/${BLUESTREAM_RTMP_APP_PLAYLIST}/${PLAYLIST_NAME}"
    )
    return 0
}

# ---------------------------------------------------------------------------
# systemd lifecycle
# ---------------------------------------------------------------------------
playlist_start() {
    local name="$1"
    bs_require_root
    playlist_load_config "$name" || bs_die "Playlist '$name' not found"
    [ "${#PLAYLIST_FILES[@]}" -gt 0 ] || bs_die "Playlist '$name' has no entries"
    playlist_entries_valid || bs_die "One or more playlist entries are missing from managed media"
    bs_ensure_hls_dir playlist "$name" || bs_die "Could not prepare HLS output directory"
    playlist_write_concat_file || bs_die "Could not prepare concat file"
    systemctl start "$(bs_unit playlist "$name")" || bs_die "Failed to start playlist '$name'"
    bs_ok "Playlist '$name' started"
}

playlist_stop() {
    local name="$1" unit
    bs_require_root
    playlist_load_config "$name" || bs_die "Playlist '$name' not found"
    unit="$(bs_unit playlist "$name")"
    systemctl stop "$unit" 2>/dev/null || bs_die "Failed to stop playlist '$name'"
    # Same intentional-stop normalization as relay_stop: an explicit stop of a
    # playlist's FFmpeg process can leave the unit 'failed'; clear the stale
    # state for this exact validated unit so it ends up 'inactive'. Genuine
    # runtime failures outside an explicit BlueStream Stop are untouched.
    systemctl reset-failed "$unit" 2>/dev/null || true
    bs_ok "Playlist '$name' stopped"
}

playlist_restart() {
    local name="$1"
    bs_require_root
    playlist_load_config "$name" || bs_die "Playlist '$name' not found"
    [ "${#PLAYLIST_FILES[@]}" -gt 0 ] || bs_die "Playlist '$name' has no entries"
    playlist_entries_valid || bs_die "One or more playlist entries are missing from managed media"
    bs_ensure_hls_dir playlist "$name" || bs_die "Could not prepare HLS output directory"
    playlist_write_concat_file || bs_die "Could not prepare concat file"
    systemctl restart "$(bs_unit playlist "$name")" || bs_die "Failed to restart playlist '$name'"
    bs_ok "Playlist '$name' restarted"
}

playlist_enable() {
    local name="$1"
    bs_require_root
    playlist_load_config "$name" || bs_die "Playlist '$name' not found"
    systemctl enable "$(bs_unit playlist "$name")" 2>/dev/null || bs_die "Failed to enable playlist '$name' at boot"
    PLAYLIST_ENABLED="yes"
    playlist_save_config "$name"
    bs_ok "Playlist '$name' enabled at boot"
}

playlist_disable() {
    local name="$1"
    bs_require_root
    playlist_load_config "$name" || bs_die "Playlist '$name' not found"
    systemctl disable "$(bs_unit playlist "$name")" 2>/dev/null || true
    PLAYLIST_ENABLED="no"
    playlist_save_config "$name"
    bs_ok "Playlist '$name' disabled at boot"
}

playlist_remove() {
    local name="$1"
    bs_require_root
    playlist_load_config "$name" || bs_die "Playlist '$name' not found"
    bs_confirm "Remove playlist '$name'? This stops it and deletes its HLS output. Media files are NOT deleted." || { bs_info "Cancelled."; return 1; }
    systemctl disable "$(bs_unit playlist "$name")" 2>/dev/null || true
    systemctl stop "$(bs_unit playlist "$name")" 2>/dev/null || true
    rm -f "$BLUESTREAM_PLAYLIST_CONF_DIR/$name.playlist"
    rm -f "$BLUESTREAM_RUN_DIR/$name.concat.txt"
    rm -rf "$(bs_hls_dir_for playlist "$name")"
    bs_ok "Playlist '$name' removed"
}

# ---------------------------------------------------------------------------
# Listing / contents / URLs / logs / health
# ---------------------------------------------------------------------------
playlist_list() {
    local names
    names="$(playlist_list_names)"
    if [ -z "$names" ]; then
        bs_info "No playlists configured yet."
        return 0
    fi
    printf '%-22s %-8s %-9s %s\n' "NAME" "ENTRIES" "HEALTH" "STATUS"
    local name n
    for name in $names; do
        playlist_load_config "$name" || continue
        health_state playlist "$name"
        n="${#PLAYLIST_FILES[@]}"
        printf '%-22s %-8s %-9s %s\n' "$name" "$n" "$HEALTH_STATE" "$(systemctl is-active "$(bs_unit playlist "$name")" 2>/dev/null)"
    done
}

playlist_show() {
    local name="$1"
    playlist_load_config "$name" || bs_die "Playlist '$name' not found"
    bs_step "Playlist: $name"
    printf '  Enabled at boot : %s\n' "$PLAYLIST_ENABLED"
    printf '  Entries         : %d\n' "${#PLAYLIST_FILES[@]}"
    local i f
    for i in "${!PLAYLIST_FILES[@]}"; do
        f="${PLAYLIST_FILES[$i]}"
        printf '  %3d) %s  (%s)\n' "$((i + 1))" "$f" "$(du -h "$BLUESTREAM_MEDIA_DIR/$f" 2>/dev/null | cut -f1)"
    done
}

playlist_show_urls() {
    local name="$1"
    playlist_load_config "$name" || bs_die "Playlist '$name' not found"
    printf 'M3U8:\n  %s\n' "$(bs_playlist_m3u8_url "$name")"
    printf 'Player:\n  %s\n' "$(bs_playlist_player_url "$name")"
}

playlist_show_m3u8() {
    local name="$1"
    playlist_load_config "$name" || bs_die "Playlist '$name' not found"
    printf '%s\n' "$(bs_playlist_m3u8_url "$name")"
}

playlist_show_player() {
    local name="$1"
    playlist_load_config "$name" || bs_die "Playlist '$name' not found"
    printf '%s\n' "$(bs_playlist_player_url "$name")"
}

playlist_logs() {
    local name="$1" lines="${2:-100}"
    bs_require_root
    playlist_load_config "$name" || bs_die "Playlist '$name' not found"
    journalctl -u "$(bs_unit playlist "$name")" -n "$lines" --no-pager
}

playlist_health() {
    local name="$1"
    playlist_load_config "$name" || bs_die "Playlist '$name' not found"
    health_state playlist "$name"
    printf '%s - %s\n' "$(health_color)" "$HEALTH_DETAILS"
    [ "$HEALTH_STATE" = "HEALTHY" ] || return 1
    return 0
}

# ---------------------------------------------------------------------------
# Create / add / remove / reorder
# ---------------------------------------------------------------------------
# Authoritative playlist creation (GUI-1D.1).  Validates the internal playlist
# ID and EVERY media entry, rejects duplicates, then writes ONE new playlist
# config atomically.  The playlist starts STOPPED (ENABLED=no) so the operator
# explicitly starts it from the console.  Shares the exact config model/save
# path as the CLI.  Returns 0 on success.  (Named *_items to avoid shadowing
# the interactive menu's playlist_create.)
#
# Exit status contract (mapped to controlled JSON error codes by web-ctl):
#   0 success
#   1 invalid playlist name
#   2 a playlist with this name already exists (never overwritten)
#   3 fewer than two media items
#   4 more than BLUESTREAM_PLAYLIST_MAX_ITEMS media items
#   5 invalid media basename
#   6 a referenced media file is not present in managed media
#   7 the same media file was listed more than once
#   8 config directory/save failure (fail closed - no config was written)
playlist_create_items() {
    local name="${1:-}"
    shift || true
    bs_require_root
    bs_valid_name "$name" || return 1
    playlist_exists "$name" && return 2
    [ "$#" -ge 2 ] || return 3
    [ "$#" -le "$BLUESTREAM_PLAYLIST_MAX_ITEMS" ] || return 4
    PLAYLIST_FILES=()
    local f
    for f in "$@"; do
        bs_valid_media_name "$f" || return 5
        [ -f "$BLUESTREAM_MEDIA_DIR/$f" ] || return 6
        case " ${PLAYLIST_FILES[*]} " in
            *" $f "*) return 7 ;;
        esac
        PLAYLIST_FILES+=( "$f" )
    done
    PLAYLIST_NAME="$name"
    PLAYLIST_ENABLED="no"
    mkdir -p "$BLUESTREAM_PLAYLIST_CONF_DIR" 2>/dev/null || return 8
    playlist_save_config "$name" || return 8
    return 0
}

playlist_select_interactive() {
    local names name i j choice
    names="$(playlist_list_names)"
    if [ -z "$names" ]; then
        bs_warn "No playlists configured yet."
        return 1
    fi
    PLAYLIST_SELECTED=""
    i=0
    for name in $names; do
        i=$((i + 1))
        printf '  %2d) %s\n' "$i" "$name"
    done
    printf 'Select playlist [1-%d, 0 to cancel]: ' "$i"
    read -r choice || return 1
    case "$choice" in
        ''|*[!0-9]*) return 1 ;;
    esac
    if [ "$choice" -ge 1 ] && [ "$choice" -le "$i" ] 2>/dev/null; then
        j=0
        for name in $names; do
            j=$((j + 1))
            if [ "$j" -eq "$choice" ]; then
                PLAYLIST_SELECTED="$name"
                return 0
            fi
        done
    fi
    return 1
}

playlist_create() {
    bs_require_root
    local name=""
    bs_prompt name "Playlist name (lowercase letters, digits, '-' or '_')" "" || return 1
    bs_valid_name "$name" || { bs_error "Invalid playlist name."; return 1; }
    playlist_exists "$name" && { bs_error "Playlist '$name' already exists."; return 1; }

    mkdir -p "$BLUESTREAM_PLAYLIST_CONF_DIR" 2>/dev/null
    PLAYLIST_NAME="$name"
    PLAYLIST_ENABLED="no"
    PLAYLIST_FILES=()
    playlist_save_config "$name"
    bs_ok "Playlist '$name' created."
    bs_info "Add media files with: Add Video to Playlist"

    if bs_confirm "Start playlist '$name' now? (requires at least one entry)"; then
        playlist_start "$name"
    fi
    if bs_confirm "Enable playlist '$name' at boot?"; then
        playlist_enable "$name"
    fi
    return 0
}

playlist_add_media() {
    local name="${1:-}"
    bs_require_root
    if [ -z "$name" ]; then
        playlist_select_interactive || return 1
        name="$PLAYLIST_SELECTED"
    fi
    playlist_load_config "$name" || bs_die "Playlist '$name' not found"

    local avail=() f i n choice
    shopt -s nullglob
    for f in "$BLUESTREAM_MEDIA_DIR"/*; do
        [ -f "$f" ] || continue
        f="$(basename "$f")"
        case " ${PLAYLIST_FILES[*]} " in
            *" $f "*) continue ;;
        esac
        avail+=( "$f" )
    done
    shopt -u nullglob

    if [ "${#avail[@]}" -eq 0 ]; then
        bs_warn "No managed media files are available to add. Import media first."
        return 1
    fi

    bs_info "Available media not already in playlist '$name':"
    i=0
    for f in "${avail[@]}"; do
        i=$((i + 1))
        printf '  %2d) %s\n' "$i" "$f"
    done
    printf 'Select files (numbers separated by spaces, e.g. "2 5 1"): '
    read -r choices || return 1
    for c in $choices; do
        case "$c" in
            ''|*[!0-9]*) continue ;;
        esac
        if [ "$c" -ge 1 ] && [ "$c" -le "${#avail[@]}" ] 2>/dev/null; then
            n=$((c - 1))
            f="${avail[$n]}"
            case " ${PLAYLIST_FILES[*]} " in
                *" $f "*) ;;
                *) PLAYLIST_FILES+=( "$f" ) ;;
            esac
        fi
    done

    if [ "${#PLAYLIST_FILES[@]}" -eq 0 ]; then
        bs_warn "No media added."
        return 1
    fi

    playlist_save_config "$name"
    bs_ok "Playlist '$name' updated with ${#PLAYLIST_FILES[@]} entries."
    if bs_confirm "Run compatibility check on the playlist now?"; then
        playlist_check_compat
    fi
    return 0
}

playlist_remove_media() {
    local name="${1:-}"
    bs_require_root
    if [ -z "$name" ]; then
        playlist_select_interactive || return 1
        name="$PLAYLIST_SELECTED"
    fi
    playlist_load_config "$name" || bs_die "Playlist '$name' not found"
    [ "${#PLAYLIST_FILES[@]}" -gt 0 ] || { bs_warn "Playlist is empty."; return 1; }

    local i f choices c n newlist=()
    for i in "${!PLAYLIST_FILES[@]}"; do
        printf '  %2d) %s\n' "$((i + 1))" "${PLAYLIST_FILES[$i]}"
    done
    printf 'Select entries to REMOVE (numbers separated by spaces): '
    read -r choices || return 1

    for i in "${!PLAYLIST_FILES[@]}"; do
        f="${PLAYLIST_FILES[$i]}"
        n=$((i + 1))
        case " $choices " in
            *" $n "*) continue ;;  # selected for removal
        esac
        newlist+=( "$f" )
    done

    PLAYLIST_FILES=( "${newlist[@]}" )
    playlist_save_config "$name"
    bs_ok "Playlist '$name' now has ${#PLAYLIST_FILES[@]} entries."
    bs_info "Media files on disk were NOT modified."
    return 0
}

playlist_reorder() {
    local name="${1:-}"
    bs_require_root
    if [ -z "$name" ]; then
        playlist_select_interactive || return 1
        name="$PLAYLIST_SELECTED"
    fi
    playlist_load_config "$name" || bs_die "Playlist '$name' not found"
    local n="${#PLAYLIST_FILES[@]}"
    [ "$n" -gt 1 ] || { bs_warn "Playlist has fewer than two entries; nothing to reorder."; return 1; }

    bs_step "Current order for playlist '$name'"
    local i f
    for i in "${!PLAYLIST_FILES[@]}"; do
        printf '  %2d) %s\n' "$((i + 1))" "${PLAYLIST_FILES[$i]}"
    done
    printf 'Enter the new order as item numbers separated by spaces (e.g. 3 1 2): '
    read -r choices || return 1

    local newlist=() seen=() valid=1 c j
    for c in $choices; do
        case "$c" in
            ''|*[!0-9]*) valid=0; break ;;
        esac
        if [ "$c" -lt 1 ] || [ "$c" -gt "$n" ] 2>/dev/null; then valid=0; break; fi
        case " ${seen[*]} " in
            *" $c "*) valid=0; break ;;
        esac
        seen+=( "$c" )
        newlist+=( "${PLAYLIST_FILES[$((c - 1))]}" )
    done
    if [ "$valid" -ne 1 ] || [ "${#newlist[@]}" -ne "$n" ]; then
        bs_error "Invalid ordering. The order must use each item number exactly once."
        return 1
    fi

    PLAYLIST_FILES=( "${newlist[@]}" )
    playlist_save_config "$name"
    bs_ok "Playlist '$name' reordered."
    return 0
}



