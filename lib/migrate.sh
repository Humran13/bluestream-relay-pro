#!/usr/bin/env bash
# BlueStream Relay Pro - safe Owned -> Isolated nginx migration.
#
# Handles the case where a BlueStream install previously ran in "owned" mode
# (BlueStream's config lives directly in /etc/nginx) and the environment has
# since changed so that isolated mode is now required (a control panel such
# as CloudPanel was installed, or another site/panel now shares the same
# system nginx) - see lib/envdetect.sh bs_env_classify_existing for how that
# is detected.
#
# Design rules (do not relax without re-reading the task's safety notes):
#   - Legacy BlueStream-owned files are identified by CONTENT marker (the
#     "BlueStream Relay Pro" header comment every generated file carries),
#     never by filename/path alone - a file that merely happens to be named
#     sites-available/bluestream is not touched unless it actually is one of
#     BlueStream's own generated files.
#   - Everything is backed up before anything is removed/disabled.
#   - The only part of the SYSTEM (panel-managed) nginx this ever touches is
#     disabling two files BlueStream itself created and verified above; nginx
#     -t validates before any reload, and a failure at any step rolls back
#     immediately and prints the manual procedure instead of guessing further.
#   - Never touches any unrelated site, panel config, or firewall rule.
#   - This only ever runs when an operator explicitly invokes it
#     (`bluestream-manager migrate run`) - install.sh itself never performs
#     this automatically; it only detects the situation and tells the
#     operator to run it (or follow the printed manual steps).
#
# BlueStream Relay Pro 0.1.0 (foundation). See LICENSE for terms.

if [ -n "${BLUESTREAM_MIGRATE_LOADED:-}" ]; then
    return 0 2>/dev/null || exit 0
fi
BLUESTREAM_MIGRATE_LOADED=1

BLUESTREAM_MIGRATE_MARKER="BlueStream Relay Pro"
BLUESTREAM_MIGRATE_MANIFEST_VERSION="1"
BLUESTREAM_MIGRATE_ACTIVE_BACKUP=""

# Content-verified detection - never inferred from filename alone.
migrate_legacy_site_present() {
    [ -f "$NGINX_SITE_FILE" ] && grep -q "$BLUESTREAM_MIGRATE_MARKER" "$NGINX_SITE_FILE" 2>/dev/null
}

migrate_legacy_site_enabled() {
    [ -L "$NGINX_SITE_LINK" ] \
        && [ "$(readlink -f "$NGINX_SITE_LINK" 2>/dev/null)" = "$(readlink -f "$NGINX_SITE_FILE" 2>/dev/null)" ] \
        && migrate_legacy_site_present
}

migrate_legacy_rtmp_present() {
    [ -f "$NGINX_RTMP_INCLUDE" ] && grep -q "$BLUESTREAM_MIGRATE_MARKER" "$NGINX_RTMP_INCLUDE" 2>/dev/null
}

migrate_legacy_rtmp_disabled_present() {
    [ -f "${NGINX_RTMP_INCLUDE}.migrated-disabled" ] \
        && grep -q "$BLUESTREAM_MIGRATE_MARKER" "${NGINX_RTMP_INCLUDE}.migrated-disabled" 2>/dev/null
}

# True when BlueStream's own owned-mode files are present on the SYSTEM
# nginx, regardless of what server.conf currently records. This is the
# signal install.sh / bluestream-manager use to know a migration may be
# needed (a reinstall alone never removes these; only this module does).
migrate_owned_present() {
    migrate_legacy_site_enabled || migrate_legacy_rtmp_present
}

# A completed migration is established by live/configured resources, not by
# an inactive legacy file retained under sites-available for rollback. This
# deliberately requires the isolated unit to be active so a broken install is
# never reported as successful merely because the old listeners are absent.
migrate_isolated_complete() {
    [ "${BLUESTREAM_NGINX_STRATEGY:-}" = "isolated" ] \
        && bs_valid_port "${BLUESTREAM_ISOLATED_HTTP_PORT:-}" \
        && [ -f "$BLUESTREAM_ISOLATED_CONF_FILE" ] \
        && grep -q "$BLUESTREAM_MIGRATE_MARKER" "$BLUESTREAM_ISOLATED_CONF_FILE" 2>/dev/null \
        && grep -q "listen 127.0.0.1:${BLUESTREAM_ISOLATED_HTTP_PORT};" "$BLUESTREAM_ISOLATED_SITE_FILE" 2>/dev/null \
        && nginx_isolated_test >/dev/null 2>&1 \
        && nginx_isolated_running \
        && bs_port_in_use "$BLUESTREAM_RTMP_PORT" \
        && ! migrate_legacy_site_enabled \
        && ! migrate_legacy_rtmp_present
}

migrate_incomplete_state_present() {
    [ "${BLUESTREAM_NGINX_STRATEGY:-}" = "isolated" ] \
        || migrate_legacy_rtmp_disabled_present \
        || { migrate_legacy_site_present && ! migrate_legacy_site_enabled; }
}

migrate_status() {
    bs_step "Owned -> Isolated migration status"
    if migrate_isolated_complete; then
        bs_ok "Migration already completed. No migration required."
        printf '  Internal streaming gateway : 127.0.0.1:%s (HTTP)\n' "${BLUESTREAM_ISOLATED_HTTP_PORT:-<unset>}"
        printf '  Public HTTPS               : provided by the hosting panel\n'
        printf '  RTMP listener              : %s:%s\n' "$BLUESTREAM_RTMP_BIND" "$BLUESTREAM_RTMP_PORT"
        return 0
    fi
    if ! migrate_owned_present; then
        if migrate_incomplete_state_present; then
            bs_error "Migration state is incomplete: no active legacy listeners are available, but the isolated service is not verified active."
            bs_error "Do not run migration again. Restore from the printed migration backup with this checkout, or repair the isolated service after reviewing 'nginx -t -c $BLUESTREAM_ISOLATED_CONF_FILE'."
            return 1
        fi
        bs_ok "No active legacy BlueStream-owned nginx resources detected. No migration required."
        return 0
    fi
    bs_warn "Legacy BlueStream-owned nginx files ARE present on the system nginx:"
    migrate_legacy_site_enabled && bs_warn "  $NGINX_SITE_LINK -> $NGINX_SITE_FILE"
    migrate_legacy_rtmp_present && bs_warn "  $NGINX_RTMP_INCLUDE"
    printf '  Current server.conf strategy : %s\n' "${BLUESTREAM_NGINX_STRATEGY:-owned}"
    bs_info "Re-checking the live environment (read-only)..."
    bs_env_detect
    printf '  Environment right now        : %s\n' "$(bs_env_mode_label)"
    if [ "$BS_NGINX_STRATEGY" = "isolated" ]; then
        bs_warn "The environment currently requires ISOLATED mode. From this NEW checkout, run:"
        bs_warn "  sudo bash $BS_ROOT/bluestream-manager migrate run"
        bs_warn "(backs up the legacy files first and can be rolled back). Recovery commands are printed below."
    else
        bs_info "The environment right now looks safe for owned mode. Migration is probably not needed; investigate why"
        bs_info "legacy files still exist (e.g. a panel was removed again) before deciding."
    fi
    migrate_print_manual_procedure
    return 0
}

migrate_print_manual_procedure() {
    local backup_dir="${1:-}"
    cat <<EOF

SAFE RECOVERY PROCEDURE (use the SAME NEW checkout; the installed old manager
may not contain these commands):
  1. Do not repeat migration and do not delete any *.migrated-disabled file.
  2. For the backup directory printed by migrate run, invoke verified rollback:
       sudo bash $BS_ROOT/bluestream-manager migrate rollback <backup-dir>
  3. Verify both configuration and the intended listeners:
       sudo nginx -t
       sudo systemctl is-active nginx
       sudo ss -ltnp | grep -E '127\\.0\\.0\\.1:(1935|<legacy-http-port>)'
     Then publish a real test source and confirm a changing HLS playlist and
     segment; HTTP 200 or nginx -t alone does not prove RTMP ingestion.
  4. If rollback refuses a missing entry/checksum/conflicting path, STOP.
     Preserve the entire backup directory and current disabled file, collect:
       sudo cat <backup-dir>/manifest
       sudo sha256sum <backup-dir>/*.bak
       sudo nginx -T
       sudo journalctl -u nginx -u bluestream-nginx.service --no-pager -n 200
     Restore only after reconciling those values with the manifest. Never
     invent a missing RTMP configuration or overwrite an unrelated panel file.
EOF
    if [ -n "$backup_dir" ]; then
        printf '\nAn automatic migration attempt backed up files to: %s\n' "$backup_dir"
    fi
}

# Backups are transactions, not just loose copies.  The manifest records the
# exact pre-migration state so rollback never guesses whether an RTMP include
# or site symlink used to exist. Copies made with cp -a retain mode/owner.
migrate_manifest_get() {
    local backup_dir="$1" key="$2"
    sed -n "s/^${key}=//p" "$backup_dir/manifest" 2>/dev/null | head -n1
}

migrate_file_sha256() {
    sha256sum "$1" 2>/dev/null | awk '{print $1}'
}

migrate_verify_backup() {
    local backup_dir="$1" version site_present site_enabled rtmp_active rtmp_disabled expected actual file
    [ -f "$backup_dir/manifest" ] || {
        bs_error "Recovery cannot be verified: $backup_dir/manifest is missing. No rollback changes were made."
        return 1
    }
    version="$(migrate_manifest_get "$backup_dir" VERSION)"
    [ "$version" = "$BLUESTREAM_MIGRATE_MANIFEST_VERSION" ] || {
        bs_error "Recovery cannot be verified: unsupported migration manifest version '${version:-missing}'. No rollback changes were made."
        return 1
    }
    site_present="$(migrate_manifest_get "$backup_dir" SITE_PRESENT)"
    site_enabled="$(migrate_manifest_get "$backup_dir" SITE_ENABLED)"
    rtmp_active="$(migrate_manifest_get "$backup_dir" RTMP_ACTIVE)"
    rtmp_disabled="$(migrate_manifest_get "$backup_dir" RTMP_DISABLED)"
    case "$site_present:$site_enabled:$rtmp_active:$rtmp_disabled" in
        [01]:[01]:[01]:[01]) ;;
        *) bs_error "Recovery cannot be verified: migration manifest flags are invalid."; return 1 ;;
    esac
    if [ "$site_enabled" = "1" ] && [ "$site_present" != "1" ]; then
        bs_error "Recovery cannot be verified: manifest records an enabled site without a site backup."
        return 1
    fi
    for file in site rtmp-active rtmp-disabled; do
        case "$file" in
            site)          [ "$site_present" = "1" ] || continue; expected="$(migrate_manifest_get "$backup_dir" SITE_SHA256)" ;;
            rtmp-active)   [ "$rtmp_active" = "1" ] || continue; expected="$(migrate_manifest_get "$backup_dir" RTMP_ACTIVE_SHA256)" ;;
            rtmp-disabled) [ "$rtmp_disabled" = "1" ] || continue; expected="$(migrate_manifest_get "$backup_dir" RTMP_DISABLED_SHA256)" ;;
        esac
        [ -f "$backup_dir/$file.bak" ] || {
            bs_error "Recovery cannot be verified: required backup entry $file.bak is missing."
            return 1
        }
        grep -q "$BLUESTREAM_MIGRATE_MARKER" "$backup_dir/$file.bak" 2>/dev/null || {
            bs_error "Recovery cannot be verified: $file.bak is not a BlueStream-owned configuration."
            return 1
        }
        actual="$(migrate_file_sha256 "$backup_dir/$file.bak")"
        if [ -z "$expected" ] || [ "$actual" != "$expected" ]; then
            bs_error "Recovery cannot be verified: checksum mismatch for $file.bak."
            return 1
        fi
    done
    return 0
}

migrate_create_backup() {
    local backup_dir="$1" site_present=0 site_enabled=0 rtmp_active=0 rtmp_disabled=0
    local site_sha="" rtmp_active_sha="" rtmp_disabled_sha="" manifest_tmp

    if [ -L "$NGINX_SITE_FILE" ] || [ -L "$NGINX_RTMP_INCLUDE" ] || \
       [ -L "${NGINX_RTMP_INCLUDE}.migrated-disabled" ]; then
        bs_error "Refusing migration: a managed nginx configuration path is an unexpected symlink."
        return 1
    fi

    if migrate_legacy_site_present; then
        site_present=1
        cp -a "$NGINX_SITE_FILE" "$backup_dir/site.bak" || return 1
        site_sha="$(migrate_file_sha256 "$backup_dir/site.bak")"
    fi
    migrate_legacy_site_enabled && site_enabled=1
    if migrate_legacy_rtmp_present; then
        rtmp_active=1
        cp -a "$NGINX_RTMP_INCLUDE" "$backup_dir/rtmp-active.bak" || return 1
        rtmp_active_sha="$(migrate_file_sha256 "$backup_dir/rtmp-active.bak")"
    fi
    if [ -e "${NGINX_RTMP_INCLUDE}.migrated-disabled" ]; then
        if ! migrate_legacy_rtmp_disabled_present; then
            bs_error "Refusing migration: ${NGINX_RTMP_INCLUDE}.migrated-disabled exists but is not a verified BlueStream configuration."
            return 1
        fi
        rtmp_disabled=1
        cp -a "${NGINX_RTMP_INCLUDE}.migrated-disabled" "$backup_dir/rtmp-disabled.bak" || return 1
        rtmp_disabled_sha="$(migrate_file_sha256 "$backup_dir/rtmp-disabled.bak")"
    fi
    # An active and an already-disabled copy is ambiguous: moving with -f
    # would destroy the old disabled copy. Preserve both and stop untouched.
    if [ "$rtmp_active" = "1" ] && [ "$rtmp_disabled" = "1" ]; then
        bs_error "Refusing migration: both active and already-disabled BlueStream RTMP includes exist. Backups were made, but neither file was changed. Resolve the duplicate explicitly."
        return 1
    fi

    manifest_tmp="$backup_dir/.manifest.tmp.$$"
    {
        printf 'VERSION=%s\n' "$BLUESTREAM_MIGRATE_MANIFEST_VERSION"
        printf 'SITE_PRESENT=%s\n' "$site_present"
        printf 'SITE_ENABLED=%s\n' "$site_enabled"
        printf 'SITE_SHA256=%s\n' "$site_sha"
        printf 'RTMP_ACTIVE=%s\n' "$rtmp_active"
        printf 'RTMP_ACTIVE_SHA256=%s\n' "$rtmp_active_sha"
        printf 'RTMP_DISABLED=%s\n' "$rtmp_disabled"
        printf 'RTMP_DISABLED_SHA256=%s\n' "$rtmp_disabled_sha"
    } > "$manifest_tmp" || return 1
    chmod 0600 "$manifest_tmp" || return 1
    mv -f "$manifest_tmp" "$backup_dir/manifest" || return 1
    migrate_verify_backup "$backup_dir"
}

# Stop and disable the isolated unit before restoring the legacy owned
# listeners. A failed systemd operation is not ignored: restoring competing
# listeners while the isolated unit can still run is unsafe.
migrate_stop_isolated_for_rollback() {
    if ! systemctl stop "$BLUESTREAM_ISOLATED_UNIT" 2>/dev/null; then
        # systemctl commonly returns success for an already-stopped unit, but
        # independently verify state before deciding this is fatal.
        [ "$(systemctl is-active "$BLUESTREAM_ISOLATED_UNIT" 2>/dev/null)" = "active" ] && {
            bs_error "Cannot roll back safely: failed to stop $BLUESTREAM_ISOLATED_UNIT."
            return 1
        }
    fi
    if ! systemctl disable "$BLUESTREAM_ISOLATED_UNIT" 2>/dev/null; then
        [ "$(systemctl is-enabled "$BLUESTREAM_ISOLATED_UNIT" 2>/dev/null)" = "enabled" ] && {
            bs_error "Cannot roll back safely: failed to disable $BLUESTREAM_ISOLATED_UNIT."
            return 1
        }
    fi
    if [ "$(systemctl is-active "$BLUESTREAM_ISOLATED_UNIT" 2>/dev/null)" = "active" ]; then
        bs_error "Cannot roll back safely: $BLUESTREAM_ISOLATED_UNIT is still active. The legacy listeners were not restored."
        return 1
    fi
    if [ "$(systemctl is-enabled "$BLUESTREAM_ISOLATED_UNIT" 2>/dev/null)" = "enabled" ]; then
        bs_error "Cannot roll back safely: $BLUESTREAM_ISOLATED_UNIT is still enabled. The legacy listeners were not restored."
        return 1
    fi
    return 0
}

# Preflight current paths before stopping anything. An unexpected non-
# BlueStream file at one of our historical paths is never overwritten.
migrate_restore_preflight() {
    local backup_dir="$1" site_present rtmp_active rtmp_disabled
    site_present="$(migrate_manifest_get "$backup_dir" SITE_PRESENT)"
    rtmp_active="$(migrate_manifest_get "$backup_dir" RTMP_ACTIVE)"
    rtmp_disabled="$(migrate_manifest_get "$backup_dir" RTMP_DISABLED)"
    if [ -L "$NGINX_SITE_FILE" ]; then
        bs_error "Refusing rollback: $NGINX_SITE_FILE is unexpectedly a symlink."
        return 1
    fi
    if [ -e "$NGINX_SITE_FILE" ] && ! migrate_legacy_site_present; then
        bs_error "Refusing rollback: $NGINX_SITE_FILE is no longer a verified BlueStream file."
        return 1
    fi
    if { [ -e "$NGINX_SITE_LINK" ] || [ -L "$NGINX_SITE_LINK" ]; } \
        && [ "$(readlink -f "$NGINX_SITE_LINK" 2>/dev/null)" != "$(readlink -f "$NGINX_SITE_FILE" 2>/dev/null)" ]; then
        bs_error "Refusing rollback: $NGINX_SITE_LINK now points to an unrelated site."
        return 1
    fi
    if [ -L "$NGINX_RTMP_INCLUDE" ] || [ -L "${NGINX_RTMP_INCLUDE}.migrated-disabled" ]; then
        bs_error "Refusing rollback: an RTMP configuration path is unexpectedly a symlink."
        return 1
    fi
    if [ -e "$NGINX_RTMP_INCLUDE" ] && ! migrate_legacy_rtmp_present; then
        bs_error "Refusing rollback: $NGINX_RTMP_INCLUDE is no longer a verified BlueStream file."
        return 1
    fi
    if [ -e "${NGINX_RTMP_INCLUDE}.migrated-disabled" ] && ! migrate_legacy_rtmp_disabled_present; then
        bs_error "Refusing rollback: ${NGINX_RTMP_INCLUDE}.migrated-disabled is not a verified BlueStream file."
        return 1
    fi
    if [ "$site_present" = "0" ] && migrate_legacy_site_present; then
        bs_error "Refusing rollback: the backup records no original site file, but a BlueStream site file exists now. It was not deleted."
        return 1
    fi
    if [ "$rtmp_active" = "0" ] && migrate_legacy_rtmp_present; then
        bs_error "Refusing rollback: the backup records no original active RTMP include, but one exists now. It was not deleted."
        return 1
    fi
    if [ "$rtmp_active" = "0" ] && [ "$rtmp_disabled" = "0" ] && migrate_legacy_rtmp_disabled_present; then
        bs_error "Refusing rollback: the backup contains no original RTMP configuration, but a disabled copy exists now. It was not deleted."
        return 1
    fi
    return 0
}

migrate_restore_backup() {
    local backup_dir="$1" site_present site_enabled rtmp_active rtmp_disabled expected actual i
    migrate_verify_backup "$backup_dir" || {
        bs_error "Safe automatic recovery is unavailable. Preserve $backup_dir and restore only verified files manually; do not delete any .migrated-disabled RTMP file."
        return 1
    }
    migrate_restore_preflight "$backup_dir" || {
        bs_error "Safe automatic recovery stopped before changing services or files. Preserve $backup_dir for manual recovery."
        return 1
    }
    site_present="$(migrate_manifest_get "$backup_dir" SITE_PRESENT)"
    site_enabled="$(migrate_manifest_get "$backup_dir" SITE_ENABLED)"
    rtmp_active="$(migrate_manifest_get "$backup_dir" RTMP_ACTIVE)"
    rtmp_disabled="$(migrate_manifest_get "$backup_dir" RTMP_DISABLED)"

    bs_warn "Rolling back the system nginx to its verified pre-migration state..."
    migrate_stop_isolated_for_rollback || return 1

    if [ "$site_present" = "1" ]; then
        cp -a "$backup_dir/site.bak" "$NGINX_SITE_FILE" || return 1
    fi
    if [ "$site_enabled" = "1" ]; then
        mkdir -p "$(dirname "$NGINX_SITE_LINK")" || return 1
        ln -sfn "$NGINX_SITE_FILE" "$NGINX_SITE_LINK" || return 1
    elif [ -L "$NGINX_SITE_LINK" ]; then
        rm -f "$NGINX_SITE_LINK" || return 1
    fi

    if [ "$rtmp_active" = "1" ]; then
        cp -a "$backup_dir/rtmp-active.bak" "$NGINX_RTMP_INCLUDE" || return 1
        expected="$(migrate_manifest_get "$backup_dir" RTMP_ACTIVE_SHA256)"
        actual="$(migrate_file_sha256 "$NGINX_RTMP_INCLUDE")"
        [ "$actual" = "$expected" ] || {
            bs_error "Restored RTMP include failed checksum verification; the disabled copy was preserved."
            return 1
        }
    fi
    if [ "$rtmp_disabled" = "1" ]; then
        cp -a "$backup_dir/rtmp-disabled.bak" "${NGINX_RTMP_INCLUDE}.migrated-disabled" || return 1
    elif [ "$rtmp_active" = "1" ] && [ -f "${NGINX_RTMP_INCLUDE}.migrated-disabled" ]; then
        # Delete only after a verified active replacement exists. This is the
        # critical guard against losing the sole remaining RTMP configuration.
        rm -f "${NGINX_RTMP_INCLUDE}.migrated-disabled" || return 1
    fi

    if ! nginx -t; then
        bs_error "Restored files failed 'nginx -t'. The backup remains at $backup_dir; the system nginx was not reloaded."
        return 1
    fi
    if ! systemctl reload nginx 2>/dev/null && ! systemctl restart nginx 2>/dev/null; then
        bs_error "Restored configuration is valid, but system nginx reload/restart failed. Run 'sudo nginx -t' and recover from $backup_dir."
        return 1
    fi
    if [ "$rtmp_active" = "1" ]; then
        i=0
        while [ "$i" -lt 10 ] && ! bs_port_in_use "$BLUESTREAM_RTMP_PORT"; do
            sleep 0.2
            i=$((i + 1))
        done
        if ! bs_port_in_use "$BLUESTREAM_RTMP_PORT"; then
            bs_error "Rollback reloaded nginx, but the intended RTMP listener $BLUESTREAM_RTMP_BIND:$BLUESTREAM_RTMP_PORT is not accepting connections. Streaming recovery is not verified; inspect nginx logs and $backup_dir."
            return 1
        fi
    fi
    bs_ok "System nginx rolled back, reloaded, and the recorded pre-migration state was restored."
    return 0
}

migrate_rollback_system_nginx() {
    migrate_restore_backup "$1"
}

migrate_transaction_disarm() {
    BLUESTREAM_MIGRATE_ACTIVE_BACKUP=""
    trap - HUP INT TERM
}

# Catch operator/session interruption after cutover begins. SIGKILL cannot be
# caught; in that case migrate_status reports the incomplete state and points
# at the manifest-backed rollback instead of recommending another migration.
migrate_transaction_interrupted() {
    local backup_dir="$BLUESTREAM_MIGRATE_ACTIVE_BACKUP"
    migrate_transaction_disarm
    bs_error "Migration was interrupted during cutover. Starting verified automatic rollback from $backup_dir."
    if [ -n "$backup_dir" ] && migrate_restore_backup "$backup_dir"; then
        BLUESTREAM_NGINX_STRATEGY="owned"
        bs_save_server_conf || bs_error "Legacy nginx was restored, but server.conf could not be reset to owned. Correct it manually before retrying."
        bs_error "Interrupted migration rolled back successfully; migration was not completed."
    else
        bs_error "AUTOMATIC ROLLBACK AFTER INTERRUPTION FAILED. Preserve all files and use the printed recovery procedure."
        migrate_print_manual_procedure "$backup_dir"
    fi
    exit 130
}

# Generate + validate (but do not start) BlueStream's isolated instance.
# Exposed standalone as `bluestream-manager migrate gen-isolated` for the
# manual procedure above, and used internally by migrate_run.
migrate_gen_isolated() {
    bs_require_root
    bs_info "Generating and validating BlueStream's isolated nginx instance (not started yet)..."
    nginx_isolated_gen_config || { bs_error "Failed to generate the isolated nginx configuration."; return 1; }
    nginx_isolated_install_unit || { bs_error "Failed to install/reload the isolated nginx systemd unit."; return 1; }
    nginx_isolated_test || { bs_error "The generated isolated nginx configuration failed 'nginx -t' validation."; return 1; }
    bs_ok "Isolated nginx configuration generated and validated (HTTP :$BLUESTREAM_ISOLATED_HTTP_PORT)."
    return 0
}

# The guided, backed-up, validated, rollback-capable automatic migration.
# Only ever invoked explicitly by an operator - never from install.sh.
migrate_run() {
    bs_require_root
    bs_step "Owned -> Isolated nginx migration"

    if migrate_isolated_complete; then
        bs_ok "Migration already completed. No migration required."
        return 0
    fi
    if ! migrate_owned_present; then
        if migrate_incomplete_state_present; then
            bs_error "Migration cannot start: the host is in an incomplete migration state with no verified active legacy service to cut over."
            bs_error "Do not repeat migration. Use 'migrate rollback <backup-dir>' with the verified backup printed by the failed run, or repair and verify the isolated service."
            return 1
        fi
        bs_ok "No active legacy BlueStream-owned nginx resources detected. No migration required."
        return 0
    fi

    bs_info "Re-checking the live environment before migrating (never trusts stale state)..."
    bs_env_detect
    if [ "$BS_NGINX_STRATEGY" != "isolated" ]; then
        bs_warn "The environment right now does not positively require isolated mode (classification: $(bs_env_mode_label))."
        bs_confirm "Legacy owned files are still present anyway. Continue migrating to isolated mode?" || {
            bs_info "Migration cancelled; nothing was changed."
            return 1
        }
    fi

    local backup_dir rollback_ok
    backup_dir="$BLUESTREAM_BACKUP_DIR/nginx-migration-$(bs_now_ts)-$$"
    if ! mkdir -m 0700 "$backup_dir"; then
        bs_error "Could not create unique backup directory $backup_dir"
        return 1
    fi
    bs_info "Backing up BlueStream's legacy nginx files to $backup_dir ..."
    if ! migrate_create_backup "$backup_dir"; then
        bs_error "Backup could not be completed and verified. The system nginx was not changed. Preserve $backup_dir for inspection."
        return 1
    fi
    bs_ok "Backup complete and manifest verified: $backup_dir"

    if ! migrate_gen_isolated; then
        bs_error "Aborting; the system nginx was not touched. Nothing was changed."
        migrate_print_manual_procedure "$backup_dir"
        return 1
    fi

    # The private RTMP bus (127.0.0.1:1935) is shared between owned/isolated
    # modes, so the legacy listener must be disabled before the isolated
    # instance can bind it. This is the narrowest possible
    # touch of the system nginx: two files BlueStream itself created and
    # verified by content marker above - never anything belonging to the
    # panel or another site.
    bs_info "Disabling BlueStream's legacy files on the system nginx (backed up above)..."
    if migrate_legacy_rtmp_present && [ -e "${NGINX_RTMP_INCLUDE}.migrated-disabled" ]; then
        bs_error "A disabled RTMP file appeared after backup verification. The system nginx was not changed; retry only after resolving that concurrent change."
        return 1
    fi
    BLUESTREAM_MIGRATE_ACTIVE_BACKUP="$backup_dir"
    trap migrate_transaction_interrupted HUP INT TERM
    if migrate_legacy_site_enabled; then
        if ! rm -f "$NGINX_SITE_LINK"; then
            migrate_transaction_disarm
            bs_error "Could not disable the verified legacy site symlink. Nothing else was changed."
            return 1
        fi
    fi
    if migrate_legacy_rtmp_present; then
        if [ -e "${NGINX_RTMP_INCLUDE}.migrated-disabled" ] \
            || ! mv "$NGINX_RTMP_INCLUDE" "${NGINX_RTMP_INCLUDE}.migrated-disabled"; then
            bs_error "Could not safely disable the verified legacy RTMP include. Starting automatic rollback."
            migrate_transaction_disarm
            migrate_rollback_system_nginx "$backup_dir" || true
            return 1
        fi
    fi

    if ! nginx -t >/dev/null 2>&1; then
        bs_error "The system nginx configuration failed validation after disabling BlueStream's legacy files."
        migrate_transaction_disarm
        if migrate_rollback_system_nginx "$backup_dir"; then rollback_ok=yes; else rollback_ok=no; fi
        [ "$rollback_ok" = "yes" ] || bs_error "AUTOMATIC ROLLBACK FAILED. Follow the recovery instructions below using $backup_dir."
        migrate_print_manual_procedure "$backup_dir"
        return 1
    fi

    if ! systemctl reload nginx 2>/dev/null; then
        bs_error "Reloading the system nginx failed."
        migrate_transaction_disarm
        if migrate_rollback_system_nginx "$backup_dir"; then rollback_ok=yes; else rollback_ok=no; fi
        [ "$rollback_ok" = "yes" ] || bs_error "AUTOMATIC ROLLBACK FAILED. Follow the recovery instructions below using $backup_dir."
        migrate_print_manual_procedure "$backup_dir"
        return 1
    fi
    bs_ok "System nginx reloaded without BlueStream's legacy site/RTMP listener."

    # nginx_isolated_enable deliberately RETURNS failure. Fresh installs turn
    # that return into bs_die in install.sh; migration must retain control so
    # it can restore the verified pre-migration state.
    if ! nginx_isolated_enable; then
        bs_error "The isolated nginx instance failed to start."
        migrate_transaction_disarm
        if migrate_rollback_system_nginx "$backup_dir"; then rollback_ok=yes; else rollback_ok=no; fi
        [ "$rollback_ok" = "yes" ] || bs_error "AUTOMATIC ROLLBACK FAILED. Follow the recovery instructions below using $backup_dir."
        migrate_print_manual_procedure "$backup_dir"
        return 1
    fi

    BLUESTREAM_NGINX_STRATEGY="isolated"
    if ! bs_save_server_conf; then
        bs_error "Could not persist isolated migration state. Starting automatic rollback."
        migrate_transaction_disarm
        migrate_rollback_system_nginx "$backup_dir" || true
        BLUESTREAM_NGINX_STRATEGY="owned"
        return 1
    fi
    if ! migrate_isolated_complete; then
        bs_error "Post-start verification did not confirm the configured gateway and RTMP listener. Starting automatic rollback."
        migrate_transaction_disarm
        migrate_rollback_system_nginx "$backup_dir" || true
        BLUESTREAM_NGINX_STRATEGY="owned"
        bs_save_server_conf || true
        return 1
    fi
    migrate_transaction_disarm
    bs_ok "Migration complete. server.conf now records NGINX_STRATEGY=isolated."
    bs_info "Backup of the disabled legacy files: $backup_dir"
    bs_info "Next: point your control panel's reverse proxy at 127.0.0.1:${BLUESTREAM_ISOLATED_HTTP_PORT:-<isolated gateway port>} - the BlueStream gateway for /console/, /player/ and /hls/."
    bs_info "Never proxy directly to Gunicorn at 127.0.0.1:${BLUESTREAM_WEB_PORT:-<internal web port>}; it serves /console/ only, so /player/ and /hls/ would return 404."
    bs_info "Rollback from this same NEW checkout if something looks wrong:"
    bs_info "  sudo bash $BS_ROOT/bluestream-manager migrate rollback $backup_dir"
    return 0
}

# Restore the pre-migration owned-mode nginx state from a backup directory
# created by migrate_run, and stop/disable the isolated instance.
migrate_rollback() {
    local backup_dir="$1"
    bs_require_root
    if [ -z "$backup_dir" ] || [ ! -d "$backup_dir" ]; then
        bs_die "Usage: migrate rollback <backup-dir> (see the path printed by 'migrate run')"
    fi
    bs_step "Rolling back the Owned -> Isolated migration from $backup_dir"

    migrate_restore_backup "$backup_dir" || return 1

    BLUESTREAM_NGINX_STRATEGY="owned"
    bs_save_server_conf || return 1
    bs_ok "server.conf now records NGINX_STRATEGY=owned again."
    return 0
}

# end of migrate.sh
