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

# Import a web-staged upload into managed media (GUI-1C.1).
#
# Security model: the unprivileged web process may only write into
# BLUESTREAM_WEB_UPLOAD_DIR. This root operation takes exclusive control of
# the staged directory entry FIRST by atomically renaming it into a root-only
# quarantine directory (BLUESTREAM_MEDIA_QUARANTINE_DIR), rejecting symlinks
# and hardlinks, and then performs a ROOT-OWNED SNAPSHOT HANDOFF: the bytes of
# the web-originated inode are copied into a NEW root-created inode inside
# quarantine, and the web-originated inode is unlinked before any validation.
# This breaks trust in the web-controlled inode entirely - a retained writable
# file descriptor held by bluestream-web can only write to the unlinked,
# discarded original and can never change the snapshot. ffprobe and the
# atomic no-clobber publication operate ONLY on that root-owned snapshot, so
# the object validated is exactly the object published. The snapshot is
# removed on every failure path.
#
# Exit status contract (mapped to controlled JSON error codes by web-ctl):
#   0 success
#   1 invalid staging ID (media-name rule)
#   2 staged upload not found / already consumed
#   3 destination already exists in managed media (never overwritten)
#   4 staged object is not a genuine regular file (symlink/hardlink/dir)
#   5 ffprobe is unavailable
#   6 probe failed - uploaded file is not recognized media
#   7 quarantine setup / snapshot / ownership / publication failure
media_import_staged() {
    bs_require_root
    local stagingfile="${1:-}" staged dest
    local quarantine="$BLUESTREAM_MEDIA_QUARANTINE_DIR"
    local quarantined="" snapshot="" staged_dev q_dev media_dev
    bs_valid_media_name "$stagingfile" || return 1
    staged="$BLUESTREAM_WEB_UPLOAD_DIR/$stagingfile"
    dest="$BLUESTREAM_MEDIA_DIR/$stagingfile"

    # Early duplicate rejection (re-checked after probe; the final publication
    # is no-overwrite, so a concurrent duplicate can never clobber the file).
    if [ -e "$dest" ]; then
        rm -f "$staged"
        return 3
    fi

    # Root-only quarantine (root:root 0700). Defensive create + verify so an
    # upgraded host that never ran the installer still fails closed safely.
    mkdir -p "$quarantine" 2>/dev/null || return 7
    chown root:root "$quarantine" 2>/dev/null || true
    chmod 0700 "$quarantine" || return 7
    [ -d "$quarantine" ] && [ ! -L "$quarantine" ] || return 7
    if [ "$(id -u)" -eq 0 ]; then
        # Owner/mode are only verifiable as root (production always runs here
        # as root; the test harness stubs bs_require_root instead).
        [ "$(stat -c %u:%a "$quarantine" 2>/dev/null || printf 'x')" = "0:700" ] || return 7
    fi

    # Atomic ownership transfer requires staging, quarantine and the final
    # media dir to live on the SAME filesystem; otherwise mv would degrade to
    # a racy copy+delete. Fail closed if the invariant cannot be established.
    staged_dev="$(stat -c %d "$BLUESTREAM_WEB_UPLOAD_DIR" 2>/dev/null || printf '0')"
    q_dev="$(stat -c %d "$quarantine" 2>/dev/null || printf '0')"
    media_dev="$(stat -c %d "$BLUESTREAM_MEDIA_DIR" 2>/dev/null || printf '0')"
    [ "$staged_dev" = "$q_dev" ] && [ "$q_dev" = "$media_dev" ] || return 7

    # Exclusive acquisition: rename whatever directory entry exists at the
    # staging path right now into quarantine (atomic rename on the same
    # filesystem). If the entry was a symlink, the symlink itself is moved and
    # rejected below; the web user can no longer influence this object.
    quarantined="$quarantine/.import.$$.$stagingfile"
    mv "$staged" "$quarantined" 2>/dev/null || return 2

    # The moved entry must be a genuine regular file - never a symlink (the
    # rename would have moved the symlink itself) and never a hardlink to a
    # file the web user cannot legitimately read.
    if [ -L "$quarantined" ] || [ ! -f "$quarantined" ]; then
        rm -rf -- "$quarantined"
        return 4
    fi
    if [ "$(stat -c %h "$quarantined" 2>/dev/null || printf '2')" -gt 1 ]; then
        rm -rf -- "$quarantined"
        return 4
    fi

    # --- root-owned snapshot handoff ---------------------------------------
    # Do NOT probe or publish the web-originated inode: a retained writable FD
    # held by bluestream-web can keep mutating it even after the rename into
    # the root-only quarantine. Copy its bytes into a NEW root-created inode
    # inside quarantine, then unlink the original so any such FD writes to an
    # unlinked, discarded inode. Only the root-owned snapshot is validated or
    # published from this point on.
    snapshot="$quarantine/.import.$$.$stagingfile.snapshot"
    if ! cp -f -- "$quarantined" "$snapshot" 2>/dev/null; then
        rm -f -- "$snapshot"
        rm -f -- "$quarantined"
        return 7
    fi
    [ -f "$snapshot" ] && [ ! -L "$snapshot" ] || {
        rm -f -- "$snapshot"
        rm -f -- "$quarantined"
        return 7
    }
    # Unlink the web-originated original NOW. Any retained writable FD writes
    # to an unlinked inode and is discarded.
    rm -f -- "$quarantined"

    # Test-only seam (never set in production - sudo env_reset strips it):
    # lets a deterministic POSIX harness write through a retained FD or
    # recreate the original staging pathname AFTER the snapshot handoff to
    # prove the validated/published object can no longer be influenced.
    if [ -n "${BLUESTREAM_MEDIA_IMPORT_HOOK:-}" ]; then
        ( BLUESTREAM_IMPORT_HOOK_SNAPSHOT="$snapshot" "$BLUESTREAM_MEDIA_IMPORT_HOOK" ) 2>/dev/null || true
    fi

    # ffprobe gate - on the root-owned SNAPSHOT only (never the original).
    if ! command -v ffprobe >/dev/null 2>&1; then
        rm -f -- "$snapshot"
        return 5
    fi
    if ! probe_media_path "$snapshot"; then
        rm -f -- "$snapshot"
        return 6
    fi

    # Duplicate re-check before publication (never overwrite managed media).
    if [ -e "$dest" ]; then
        rm -f -- "$snapshot"
        return 3
    fi

    # Normalize owner/mode on the snapshot itself and publish THE SAME inode
    # that passed ffprobe with an atomic no-clobber rename. chown/chmod
    # failures fail closed - a file the relay runtime cannot read is never
    # published.
    chown root:"$BLUESTREAM_GROUP" "$snapshot" 2>/dev/null || { rm -f -- "$snapshot"; return 7; }
    chmod 0640 "$snapshot" || { rm -f -- "$snapshot"; return 7; }
    mv -n "$snapshot" "$dest" 2>/dev/null || true
    if [ -e "$snapshot" ]; then
        # mv -n declined (destination appeared concurrently) or failed.
        rm -f -- "$snapshot"
        [ -e "$dest" ] && return 3
        return 7
    fi

    # Success: the snapshot inode is now the managed-media file; nothing else
    # remains in quarantine.
    return 0
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

# Return 0 (referenced - do NOT delete) when a managed media basename is used
# by any relay (a local-file relay whose URL is exactly this managed file) or
# listed in any playlist.  Returns 1 only when nothing references it.  Reads
# the root-only config files directly (whole-line fixed-string match) so it
# needs no other library loaded and cannot be fooled by a partial match.
media_is_referenced() {
    local name="$1" f
    bs_valid_media_name "$name" || return 0
    shopt -s nullglob
    for f in "$BLUESTREAM_RELAY_CONF_DIR"/*.conf; do
        [ -f "$f" ] || continue
        if grep -qxF "URL=$BLUESTREAM_MEDIA_DIR/$name" "$f"; then
            shopt -u nullglob
            return 0
        fi
    done
    for f in "$BLUESTREAM_PLAYLIST_CONF_DIR"/*.playlist; do
        [ -f "$f" ] || continue
        if grep -qxF "$name" "$f"; then
            shopt -u nullglob
            return 0
        fi
    done
    shopt -u nullglob
    return 1
}

# Authoritative NON-interactive managed-media delete for the web console
# (GUI-5A).  Deletes EXACTLY ONE validated managed-media file and only when
# nothing references it.  Never accepts an arbitrary path, never follows a
# symlink, never recurses into a directory.  Unrelated normalized-cache
# artifacts may remain and are reclaimed through the existing cache workflow.
#   0 ok | 1 invalid media name | 2 not found | 3 not a plain file / symlink |
#   4 still referenced by a stream or playlist | 5 removal failed
media_delete() {
    bs_require_root
    local name="${1:-}" p
    bs_valid_media_name "$name" || return 1
    p="$BLUESTREAM_MEDIA_DIR/$name"
    [ -e "$p" ] || return 2
    { [ -L "$p" ] || [ ! -f "$p" ]; } && return 3
    media_is_referenced "$name" && return 4
    rm -f -- "$p" || return 5
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

