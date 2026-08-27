#!/usr/bin/env bash
# BlueStream Relay Pro - backup / restore of configuration.
#
# Backs up relay definitions, playlist definitions, the server configuration
# and project metadata. Media files are NOT included by default (use
# --with-media explicitly; media backups can be large).
#
# BlueStream Relay Pro 0.1.0 (foundation). See LICENSE for terms.

if [ -n "${BLUESTREAM_BACKUP_LOADED:-}" ]; then
    return 0 2>/dev/null || exit 0
fi
BLUESTREAM_BACKUP_LOADED=1

backup_configs() {
    bs_require_root
    local with_media="${1:-no}"
    mkdir -p "$BLUESTREAM_BACKUP_DIR" 2>/dev/null || bs_die "Cannot create backup directory"
    local stamp backup_file
    stamp="$(bs_now_ts)"
    backup_file="$BLUESTREAM_BACKUP_DIR/bluestream-config-$stamp.tar.gz"

    local includes=("etc/bluestream")
    if [ -f /usr/local/lib/bluestream/VERSION ]; then
        includes+=("usr/local/lib/bluestream/VERSION")
    fi
    if [ "$with_media" = "yes" ]; then
        bs_warn "Including managed media in the backup (this can be very large)."
        includes+=("var/lib/bluestream/media")
    fi

    tar -czf "$backup_file" -C / "${includes[@]}" 2>/dev/null || bs_die "Backup failed."
    chmod 0600 "$backup_file"
    chown root:root "$backup_file" 2>/dev/null || true
    bs_ok "Configuration backup created: $backup_file"
    printf '  Size : %s\n' "$(du -h "$backup_file" 2>/dev/null | cut -f1)"
    return 0
}

backup_list() {
    local b
    shopt -s nullglob
    for b in "$BLUESTREAM_BACKUP_DIR"/*.tar.gz; do
        printf '%s  (%s)\n' "$b" "$(du -h "$b" 2>/dev/null | cut -f1)"
    done
    shopt -u nullglob
}

backup_restore() {
    bs_require_root
    local file="${1:-}"
    if [ -z "$file" ]; then
        local backups=()
        shopt -s nullglob
        backups=( "$BLUESTREAM_BACKUP_DIR"/*.tar.gz )
        shopt -u nullglob
        [ "${#backups[@]}" -gt 0 ] || bs_die "No backups found in $BLUESTREAM_BACKUP_DIR"
        local i choice
        for i in "${!backups[@]}"; do
            printf '  %2d) %s\n' "$((i + 1))" "${backups[$i]}"
        done
        printf 'Select backup [1-%d, 0 to cancel]: ' "${#backups[@]}"
        read -r choice || return 1
        case "$choice" in ''|*[!0-9]*) return 1 ;; esac
        [ "$choice" -ge 1 ] && [ "$choice" -le "${#backups[@]}" ] 2>/dev/null || return 1
        file="${backups[$((choice - 1))]}"
    fi

    [ -f "$file" ] || bs_die "Backup file not found: $file"

    # Safety: refuse archives containing paths outside the allowed set.
    local bad
    bad="$(tar -tzf "$file" 2>/dev/null | grep -v -E '^(etc/bluestream/|usr/local/lib/bluestream/VERSION$|var/lib/bluestream/media/)' | head -n 10)" || true
    if [ -n "$bad" ]; then
        bs_error "Archive contains unexpected paths; refusing to restore:"
        printf '%s\n' "$bad"
        return 1
    fi

    bs_confirm "Restore configuration from '$file'? Existing relays/playlists will be overwritten." || { bs_info "Cancelled."; return 1; }

    systemctl stop 'bluestream-relay@*' 'bluestream-playlist@*' 2>/dev/null || true
    tar -xzf "$file" -C / 2>/dev/null || bs_die "Restore failed."

    # Re-apply root-only permissions after restore.
    chown -R root:root /etc/bluestream 2>/dev/null || true
    chmod 0700 /etc/bluestream 2>/dev/null || true
    chmod 0700 /etc/bluestream/relays /etc/bluestream/playlists 2>/dev/null || true
    chmod 0600 /etc/bluestream/server.conf /etc/bluestream/relays/*.conf /etc/bluestream/playlists/*.playlist 2>/dev/null || true

    systemctl daemon-reload 2>/dev/null || true
    bs_ok "Configuration restored from $file"
    bs_warn "Restored relays/playlists are STOPPED. Start them explicitly when ready."
    return 0
}
