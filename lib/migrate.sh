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

# Content-verified detection - never inferred from filename alone.
migrate_legacy_site_present() {
    [ -f "$NGINX_SITE_FILE" ] && grep -q "$BLUESTREAM_MIGRATE_MARKER" "$NGINX_SITE_FILE" 2>/dev/null
}

migrate_legacy_rtmp_present() {
    [ -f "$NGINX_RTMP_INCLUDE" ] && grep -q "$BLUESTREAM_MIGRATE_MARKER" "$NGINX_RTMP_INCLUDE" 2>/dev/null
}

# True when BlueStream's own owned-mode files are present on the SYSTEM
# nginx, regardless of what server.conf currently records. This is the
# signal install.sh / bluestream-manager use to know a migration may be
# needed (a reinstall alone never removes these; only this module does).
migrate_owned_present() {
    migrate_legacy_site_present || migrate_legacy_rtmp_present
}

migrate_status() {
    bs_step "Owned -> Isolated migration status"
    if ! migrate_owned_present; then
        bs_ok "No legacy BlueStream-owned nginx files detected on the system nginx (verified by content marker). Nothing to migrate."
        return 0
    fi
    bs_warn "Legacy BlueStream-owned nginx files ARE present on the system nginx:"
    migrate_legacy_site_present && bs_warn "  $NGINX_SITE_FILE (and/or its sites-enabled symlink)"
    migrate_legacy_rtmp_present && bs_warn "  $NGINX_RTMP_INCLUDE"
    printf '  Current server.conf strategy : %s\n' "${BLUESTREAM_NGINX_STRATEGY:-owned}"
    bs_info "Re-checking the live environment (read-only)..."
    bs_env_detect
    printf '  Environment right now        : %s\n' "$(bs_env_mode_label)"
    if [ "$BS_NGINX_STRATEGY" = "isolated" ]; then
        bs_warn "The environment currently requires ISOLATED mode. Run 'sudo bluestream-manager migrate run' to migrate safely"
        bs_warn "(backs up the legacy files first and can be rolled back), or follow the manual procedure below."
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

MANUAL Owned -> Isolated MIGRATION PROCEDURE (use if automatic migration
did not complete, or if you prefer to do it by hand):
  1. Back up BlueStream's own files (never anything else on the system nginx):
       sudo mkdir -p /root/bluestream-nginx-backup
       sudo cp -a $NGINX_SITE_FILE /root/bluestream-nginx-backup/ 2>/dev/null
       sudo cp -a $NGINX_RTMP_INCLUDE /root/bluestream-nginx-backup/ 2>/dev/null
  2. Generate (but do not yet start) BlueStream's own isolated instance:
       sudo bluestream-manager migrate gen-isolated
  3. Remove ONLY BlueStream's own files from the system (panel-managed) nginx:
       sudo rm -f $NGINX_SITE_LINK
       sudo mv $NGINX_RTMP_INCLUDE ${NGINX_RTMP_INCLUDE}.disabled
  4. Validate and reload the system nginx (this is your panel's nginx; this
     step only removes BlueStream's own two files from it):
       sudo nginx -t && sudo systemctl reload nginx
  5. Start BlueStream's isolated instance:
       sudo systemctl enable --now bluestream-nginx.service
  6. Point your control panel's reverse proxy at 127.0.0.1:\$WEB_PORT for the
     dashboard (see: sudo bluestream-manager installinfo).
  7. If anything goes wrong, restore step 1's backups and re-run step 4, then
     stop bluestream-nginx.service.
EOF
    if [ -n "$backup_dir" ]; then
        printf '\nAn automatic migration attempt backed up files to: %s\n' "$backup_dir"
    fi
}

# Roll the SYSTEM nginx back to serving BlueStream's legacy owned files again
# (used internally on a failed automatic migration, and exposed as
# `bluestream-manager migrate rollback-system` for manual recovery
# immediately after a failed attempt in the same session).
migrate_rollback_system_nginx() {
    local disabled_link="$1" disabled_rtmp="$2"
    bs_warn "Rolling back the system nginx to its pre-migration state..."
    if [ "$disabled_link" = "1" ]; then
        ln -sf "$NGINX_SITE_FILE" "$NGINX_SITE_LINK" 2>/dev/null || true
    fi
    if [ "$disabled_rtmp" = "1" ] && [ -f "${NGINX_RTMP_INCLUDE}.migrated-disabled" ]; then
        mv -f "${NGINX_RTMP_INCLUDE}.migrated-disabled" "$NGINX_RTMP_INCLUDE" 2>/dev/null || true
    fi
    if nginx -t >/dev/null 2>&1; then
        systemctl reload nginx 2>/dev/null || systemctl restart nginx 2>/dev/null
        bs_ok "System nginx rolled back and reloaded."
    else
        bs_error "Rollback left the system nginx config invalid. Manual intervention required: check 'nginx -t' and the backup."
        return 1
    fi
    return 0
}

# Generate + validate (but do not start) BlueStream's isolated instance.
# Exposed standalone as `bluestream-manager migrate gen-isolated` for the
# manual procedure above, and used internally by migrate_run.
migrate_gen_isolated() {
    bs_require_root
    bs_info "Generating and validating BlueStream's isolated nginx instance (not started yet)..."
    nginx_isolated_gen_config || { bs_error "Failed to generate the isolated nginx configuration."; return 1; }
    nginx_isolated_install_unit
    nginx_isolated_test || { bs_error "The generated isolated nginx configuration failed 'nginx -t' validation."; return 1; }
    bs_ok "Isolated nginx configuration generated and validated (HTTP :$BLUESTREAM_ISOLATED_HTTP_PORT)."
    return 0
}

# The guided, backed-up, validated, rollback-capable automatic migration.
# Only ever invoked explicitly by an operator - never from install.sh.
migrate_run() {
    bs_require_root
    bs_step "Owned -> Isolated nginx migration"

    if ! migrate_owned_present; then
        bs_ok "No legacy BlueStream-owned nginx files detected (verified by content marker). Nothing to migrate."
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

    local backup_dir
    backup_dir="$BLUESTREAM_BACKUP_DIR/nginx-migration-$(bs_now_ts)"
    mkdir -p "$backup_dir" || bs_die "Could not create backup directory $backup_dir"
    bs_info "Backing up BlueStream's legacy nginx files to $backup_dir ..."
    local f
    for f in "$NGINX_SITE_FILE" "$NGINX_RTMP_INCLUDE"; do
        [ -e "$f" ] && cp -a "$f" "$backup_dir/$(basename "$f").bak" 2>/dev/null
    done
    bs_ok "Backup complete: $backup_dir"

    if ! migrate_gen_isolated; then
        bs_error "Aborting; the system nginx was not touched. Nothing was changed."
        migrate_print_manual_procedure "$backup_dir"
        return 1
    fi

    # The private RTMP bus (127.0.0.1:1935) and port 80 are shared between
    # owned/isolated modes, so the legacy listener must be disabled before
    # the isolated instance can bind them. This is the narrowest possible
    # touch of the system nginx: two files BlueStream itself created and
    # verified by content marker above - never anything belonging to the
    # panel or another site.
    bs_info "Disabling BlueStream's legacy files on the system nginx (backed up above)..."
    local disabled_link=0 disabled_rtmp=0
    if [ -L "$NGINX_SITE_LINK" ]; then
        rm -f "$NGINX_SITE_LINK" && disabled_link=1
    fi
    if [ -f "$NGINX_RTMP_INCLUDE" ]; then
        mv -f "$NGINX_RTMP_INCLUDE" "${NGINX_RTMP_INCLUDE}.migrated-disabled" && disabled_rtmp=1
    fi

    if ! nginx -t >/dev/null 2>&1; then
        bs_error "The system nginx configuration failed validation after disabling BlueStream's legacy files."
        migrate_rollback_system_nginx "$disabled_link" "$disabled_rtmp"
        migrate_print_manual_procedure "$backup_dir"
        return 1
    fi

    if ! systemctl reload nginx 2>/dev/null; then
        bs_error "Reloading the system nginx failed."
        migrate_rollback_system_nginx "$disabled_link" "$disabled_rtmp"
        migrate_print_manual_procedure "$backup_dir"
        return 1
    fi
    bs_ok "System nginx reloaded without BlueStream's legacy site/RTMP listener."

    if ! nginx_isolated_enable; then
        bs_error "The isolated nginx instance failed to start."
        migrate_rollback_system_nginx "$disabled_link" "$disabled_rtmp"
        migrate_print_manual_procedure "$backup_dir"
        return 1
    fi

    BLUESTREAM_NGINX_STRATEGY="isolated"
    bs_save_server_conf
    bs_ok "Migration complete. server.conf now records NGINX_STRATEGY=isolated."
    bs_info "Backup of the disabled legacy files: $backup_dir"
    bs_info "Next: point your control panel's reverse proxy at 127.0.0.1:${BLUESTREAM_WEB_PORT:-<web port>} (dashboard)"
    bs_info "and, if you want this host's panel-managed domain to serve HLS/player, at 127.0.0.1:${BLUESTREAM_ISOLATED_HTTP_PORT:-<isolated http port>}."
    bs_info "Rollback if something looks wrong: sudo bluestream-manager migrate rollback $backup_dir"
    return 0
}

# Restore the pre-migration owned-mode nginx state from a backup directory
# created by migrate_run, and stop/disable the isolated instance.
migrate_rollback() {
    local backup_dir="$1"
    bs_require_root
    [ -n "$backup_dir" ] && [ -d "$backup_dir" ] || bs_die "Usage: migrate rollback <backup-dir> (see the path printed by 'migrate run')"
    bs_step "Rolling back the Owned -> Isolated migration from $backup_dir"

    if [ -f "$backup_dir/$(basename "$NGINX_SITE_FILE").bak" ]; then
        cp -a "$backup_dir/$(basename "$NGINX_SITE_FILE").bak" "$NGINX_SITE_FILE"
    fi
    if [ -f "$backup_dir/$(basename "$NGINX_RTMP_INCLUDE").bak" ]; then
        cp -a "$backup_dir/$(basename "$NGINX_RTMP_INCLUDE").bak" "$NGINX_RTMP_INCLUDE"
    fi
    rm -f "${NGINX_RTMP_INCLUDE}.migrated-disabled" 2>/dev/null

    systemctl stop bluestream-nginx.service 2>/dev/null || true
    systemctl disable bluestream-nginx.service 2>/dev/null || true

    mkdir -p /etc/nginx/sites-enabled 2>/dev/null
    ln -sf "$NGINX_SITE_FILE" "$NGINX_SITE_LINK" 2>/dev/null

    if nginx -t >/dev/null 2>&1; then
        systemctl reload nginx 2>/dev/null || systemctl restart nginx 2>/dev/null
        bs_ok "Rollback applied: owned-mode nginx restored and reloaded."
    else
        bs_error "Rollback files failed 'nginx -t' validation. Manual intervention required; backup remains at $backup_dir"
        return 1
    fi

    BLUESTREAM_NGINX_STRATEGY="owned"
    bs_save_server_conf
    bs_ok "server.conf now records NGINX_STRATEGY=owned again."
    return 0
}

# end of migrate.sh
