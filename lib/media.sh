#!/usr/bin/env bash
# BlueStream Relay Pro - managed media management.
#
# Safe import/list/inspect/remove of media under /var/lib/bluestream/media.
# Imports copy into the managed directory with root:bluestream-relay 0640.
# Removals require explicit confirmation. Playlist operations never delete
# media through this module.
#
# BlueStream Relay Pro 0.1.0 (foundation). See LICENSE for terms.

if [ -n "${BLUESTREAM_MEDIA_LOADED:-}" ]; then
    return 0 2>/dev/null || exit 0
fi
BLUESTREAM_MEDIA_LOADED=1

media_list_names() {
    local f
    shopt -s nullglob
    for f in "$BLUESTREAM_MEDIA_DIR"/*; do
        [ -f "$f" ] || continue
        basename "$f"
    done
    shopt -u nullglob
}

media_import() {
    bs_require_root
    local src="${1:-}" name="${2:-}"
    if [ -z "$src" ]; then
        bs_prompt src "Source to import (local absolute path or http(s) URL)" "" || return 1
    fi
    [ -n "$src" ] || { bs_error "No source given."; return 1; }

    local tmp=""
    case "$src" in
        http://*|https://*)
            bs_valid_url "$src" || { bs_error "Invalid URL."; return 1; }
            bs_require_cmd curl
            if [ -z "$name" ]; then
                name="$(basename "${src%%\?*}")"
                name="${name:-imported.bin}"
            fi
            tmp="$BLUESTREAM_MEDIA_DIR/.import.$$.part"
            bs_info "Downloading $src ..."
            curl -fL --max-time 600 -o "$tmp" "$src" || { rm -f "$tmp"; bs_die "Download failed."; }
            ;;
        *)
            bs_valid_abs_path "$src" || { bs_error "Invalid path."; return 1; }
            [ -f "$src" ] || { bs_error "File not found: $src"; return 1; }
            if [ -z "$name" ]; then
                name="$(basename "$src")"
            fi
            ;;
    esac

    bs_valid_media_name "$name" || { rm -f "$tmp"; bs_error "Invalid media file name. Use [A-Za-z0-9._-] with no '..' or leading dot."; return 1; }

    local dest="$BLUESTREAM_MEDIA_DIR/$name"
    [ -e "$dest" ] && { rm -f "$tmp"; bs_error "A file named '$name' already exists in managed media."; return 1; }

    # Probe before accepting: only real media is imported.
    if ! bs_have_ffprobe; then
        rm -f "$tmp"
        bs_die "ffprobe is required for safe media import."
    fi
    if [ -n "$tmp" ]; then
        if ! probe_media_path "$tmp"; then
            rm -f "$tmp"
            bs_die "Downloaded file is not a recognized media source."
        fi
        install -o root -g "$BLUESTREAM_GROUP" -m 0640 "$tmp" "$dest"
        rm -f "$tmp"
    else
        if ! probe_media_path "$src"; then
            bs_die "File is not a recognized media source."
        fi
        install -o root -g "$BLUESTREAM_GROUP" -m 0640 "$src" "$dest"
    fi

    bs_ok "Imported '$name' into managed media."
    printf '  Path : %s\n' "$dest"
    return 0
}

media_list() {
    local names
    names="$(media_list_names)"
    if [ -z "$names" ]; then
        bs_info "No managed media yet. Use: Import Media"
        return 0
    fi
    printf '%-40s %-10s %s\n' "FILE" "SIZE" "DETAILS"
    local f
    for f in $names; do
        local p="$BLUESTREAM_MEDIA_DIR/$f"
        local size
        size="$(du -h "$p" 2>/dev/null | cut -f1)"
        if probe_media_path "$p" >/dev/null 2>&1; then
            printf '%-40s %-10s %s %sx%s %s\n' "$f" "${size:-?}" "${PV_CODEC:-?}" "${PV_WIDTH:-?}" "${PV_HEIGHT:-?}" "${PV_FPS:-?}fps"
        else
            printf '%-40s %-10s %s\n' "$f" "${size:-?}" "unprobed"
        fi
    done
}

media_inspect() {
    local name="${1:-}"
    if [ -z "$name" ]; then
        bs_prompt name "Media file name to inspect" "" || return 1
    fi
    bs_valid_media_name "$name" || { bs_error "Invalid media file name."; return 1; }
    local p="$BLUESTREAM_MEDIA_DIR/$name"
    [ -f "$p" ] || bs_die "No such managed media file: $name"
    bs_require_cmd ffprobe
    probe_media_path "$p" || bs_die "Probe failed."
    probe_compat_report "$name"
    return 0
}

media_select_interactive() {
    local names name i j choice
    names="$(media_list_names)"
    if [ -z "$names" ]; then
        bs_warn "No managed media."
        return 1
    fi
    MEDIA_SELECTED=""
    i=0
    for name in $names; do
        i=$((i + 1))
        printf '  %2d) %s\n' "$i" "$name"
    done
    printf 'Select media [1-%d, 0 to cancel]: ' "$i"
    read -r choice || return 1
    case "$choice" in
        ''|*[!0-9]*) return 1 ;;
    esac
    if [ "$choice" -ge 1 ] && [ "$choice" -le "$i" ] 2>/dev/null; then
        j=0
        for name in $names; do
            j=$((j + 1))
            if [ "$j" -eq "$choice" ]; then
                MEDIA_SELECTED="$name"
                return 0
            fi
        done
    fi
    return 1
}

media_remove() {
    bs_require_root
    local name="${1:-}"
    if [ -z "$name" ]; then
        media_select_interactive || return 1
        name="$MEDIA_SELECTED"
    fi
    bs_valid_media_name "$name" || { bs_error "Invalid media file name."; return 1; }
    local p="$BLUESTREAM_MEDIA_DIR/$name"
    [ -f "$p" ] || bs_die "No such managed media file: $name"

    # Warn if any playlist references this file.
    local playlists_with_ref=() pl
    for pl in $(playlist_list_names); do
        if playlist_load_config "$pl" 2>/dev/null; then
            case " ${PLAYLIST_FILES[*]} " in
                *" $name "*) playlists_with_ref+=( "$pl" ) ;;
            esac
        fi
    done
    if [ "${#playlists_with_ref[@]}" -gt 0 ]; then
        bs_warn "This media file is referenced by playlists: ${playlists_with_ref[*]}"
        bs_warn "Removing it will break those playlists (entries will be invalid)."
    fi

    bs_confirm "Permanently remove media file '$name' from managed media?" || { bs_info "Cancelled."; return 1; }
    rm -f "$p"
    bs_ok "Removed media file '$name'."
    return 0
}

media_show_bandwidth() {
    # Standalone bandwidth estimator (not tied to a relay).
    local mbps viewers total
    bs_prompt mbps "Source bitrate in Mbps (e.g. 4, 8, 15)" "" || return 1
    case "$mbps" in ''|*[!0-9.]*) bs_error "Invalid bitrate."; return 1 ;; esac
    bs_prompt viewers "Expected simultaneous viewers" "" || return 1
    case "$viewers" in ''|*[!0-9]*) bs_error "Invalid viewer count."; return 1 ;; esac
    total="$(estimate_bandwidth "$mbps" "$viewers")"
    bs_step "Estimated outbound bandwidth"
    printf '  Source bitrate  : %s Mbps\n' "$mbps"
    printf '  Viewers         : %s\n' "$viewers"
    printf '  Approx outbound : %s Mbps\n' "$total"
    bandwidth_show_explanation
    return 0
}

