#!/usr/bin/env bash
# BlueStream Relay Pro - web console production installation helpers (GUI-1A.3B).
#
# Used by install.sh and uninstall.sh. Contains the deployment integration for
# the read-only Flask web console:
#   bluestream-web system user
#   /var/lib/bluestream/web production state
#   web-ctl installed root:root 0700
#   /etc/sudoers.d/bluestream-web (staged + visudo -cf validated, 0440)
#   bluestream-web.service (Gunicorn on 127.0.0.1:8080)
#   web-side secure_cookie deployment setting (never reads /etc/bluestream)
#
# The web console remains strictly read-only and never gains direct access to
# /etc/bluestream; engine data is reached only through web-ctl via sudo.
#
# BlueStream Relay Pro 0.1.0 (foundation). See LICENSE for terms.

if [ -n "${BLUESTREAM_WEBCONSOLE_LOADED:-}" ]; then
    return 0 2>/dev/null || exit 0
fi
BLUESTREAM_WEBCONSOLE_LOADED=1

# ---------------------------------------------------------------------------
# Web console paths (fixed, absolute; never derived from cwd/env).
# ---------------------------------------------------------------------------
BLUESTREAM_WEB_USER="bluestream-web"
BLUESTREAM_WEB_GROUP="bluestream-web"
BLUESTREAM_WEB_STATE_DIR="/var/lib/bluestream/web"
BLUESTREAM_WEB_INSTALL_ROOT="/usr/local/lib/bluestream"
BLUESTREAM_WEB_WEBCTL="${BLUESTREAM_WEB_INSTALL_ROOT}/web-ctl"
BLUESTREAM_WEB_SUDOERS="/etc/sudoers.d/bluestream-web"
BLUESTREAM_WEB_UNIT="/etc/systemd/system/bluestream-web.service"
BLUESTREAM_WEB_SERVICE="bluestream-web.service"
BLUESTREAM_WEB_GUNICORN="/usr/bin/gunicorn"
BLUESTREAM_WEB_CONF="${BLUESTREAM_WEB_STATE_DIR}/web.conf"

# ---------------------------------------------------------------------------
# Web packages (Ubuntu) - verified after installation.
# ---------------------------------------------------------------------------
web_packages() {
    printf '%s\n' "python3 python3-flask gunicorn sudo"
}

# Fail clearly if the expected Ubuntu package layout is absent. Never search
# PATH for gunicorn: the installed web-ctl service pins /usr/bin/gunicorn.
web_verify_packages() {
    if [ ! -x "$BLUESTREAM_WEB_GUNICORN" ]; then
        bs_die "Gunicorn was not installed at $BLUESTREAM_WEB_GUNICORN as expected. The Ubuntu 'gunicorn' package should provide it. Fix package installation and re-run; BlueStream will NOT search PATH for it."
    fi
    if ! python3 -c 'import flask' >/dev/null 2>&1; then
        bs_die "python3-flask is not importable by python3. Install the Ubuntu 'python3-flask' package and re-run."
    fi
    bs_ok "Web console packages available ($BLUESTREAM_WEB_GUNICORN + python3-flask)"
    return 0
}

# ---------------------------------------------------------------------------
# bluestream-web system account (idempotent).
# ---------------------------------------------------------------------------
web_create_user() {
    if ! getent group "$BLUESTREAM_WEB_GROUP" >/dev/null 2>&1; then
        groupadd --system "$BLUESTREAM_WEB_GROUP" || bs_die "Failed to create system group $BLUESTREAM_WEB_GROUP"
        bs_ok "Created system group $BLUESTREAM_WEB_GROUP"
    fi
    if ! id "$BLUESTREAM_WEB_USER" >/dev/null 2>&1; then
        useradd --system \
            --gid "$BLUESTREAM_WEB_GROUP" \
            --home-dir /nonexistent \
            --shell /usr/sbin/nologin \
            --no-create-home "$BLUESTREAM_WEB_USER" || bs_die "Failed to create system user $BLUESTREAM_WEB_USER"
        bs_ok "Created system user $BLUESTREAM_WEB_USER"
    else
        bs_ok "System user $BLUESTREAM_WEB_USER already exists (preserved)"
    fi

    # Never grant the web user privileged group membership.
    local _grp _bad=0
    for _grp in sudo root www-data; do
        if id -nG "$BLUESTREAM_WEB_USER" 2>/dev/null | grep -qw "$_grp"; then
            bs_warn "User '$BLUESTREAM_WEB_USER' is a member of group '$_grp' (unexpected for a web-only account)."
            _bad=1
        fi
    done
    unset _grp
    [ "$_bad" = "0" ] && bs_ok "bluestream-web is not a member of privileged groups"
    return 0
}

# ---------------------------------------------------------------------------
# Production web state directory and deployment settings.
# ---------------------------------------------------------------------------
web_create_state() {
    # The web user needs READ access only: the installer (root) pre-creates
    # admin.json, secret_key and web.conf; the running Flask process must never
    # be able to replace credentials, the session-signing key, or the Secure
    # cookie decision. Files: root:bluestream-web 0640; dir: root:bluestream-web
    # 0750 (web user can read, cannot create/replace).
    mkdir -p "$BLUESTREAM_WEB_STATE_DIR"
    chown root:"$BLUESTREAM_WEB_GROUP" "$BLUESTREAM_WEB_STATE_DIR"
    chmod 0750 "$BLUESTREAM_WEB_STATE_DIR"
    local _f
    for _f in admin.json secret_key web.conf; do
        if [ -f "$BLUESTREAM_WEB_STATE_DIR/$_f" ]; then
            chown root:"$BLUESTREAM_WEB_GROUP" "$BLUESTREAM_WEB_STATE_DIR/$_f"
            chmod 0640 "$BLUESTREAM_WEB_STATE_DIR/$_f"
        fi
    done
    unset _f

    # sudo's timestamp directory: ensure it exists (root:root 0755) so a
    # NOPASSWD `sudo -n` under the hardened service can always write its
    # per-user timestamp. systemd would also create it via ReadWritePaths,
    # but making it deterministic is safer.
    mkdir -p /run/sudo
    chown root:root /run/sudo
    chmod 0755 /run/sudo
    return 0
}

# Write the non-secret deployment setting for the Flask app. Derived from the
# final BlueStream SSL state (read by the installer as root); the unprivileged
# web process never reads /etc/bluestream/server.conf. Delegated to the nginx
# pipeline so the value also re-syncs whenever SSL is later enabled via
# `bluestream-manager ssl issue` (see nginx_sync_web_conf in lib/nginx.sh).
web_configure_state() {
    nginx_sync_web_conf
    # GUI-1C.1: narrowly writable upload staging area. This is the ONLY path
    # the unprivileged bluestream-web process may write to; the root web-ctl
    # bridge imports staged uploads from here into /var/lib/bluestream/media.
    # 0770 root:bluestream-web keeps it private to root and the web user.
    mkdir -p "$BLUESTREAM_WEB_STATE_DIR/upload" 2>/dev/null
    chown root:"$BLUESTREAM_WEB_GROUP" "$BLUESTREAM_WEB_STATE_DIR/upload"
    chmod 0770 "$BLUESTREAM_WEB_STATE_DIR/upload"
    # GUI-1C.1: root-only quarantine for staged uploads. The root bridge
    # atomically renames the staged entry here BEFORE ffprobe/import, so
    # bluestream-web can never swap or symlink the object that gets imported.
    # The quarantine lives under the existing /var/lib/bluestream/run tree
    # (root:root 0700; that parent directory is 0750 and not accessible to
    # bluestream-web) and is covered by the service ReadWritePaths entry for
    # /var/lib/bluestream/run.
    mkdir -p "$BLUESTREAM_MEDIA_QUARANTINE_DIR" 2>/dev/null
    chown root:root "$BLUESTREAM_MEDIA_QUARANTINE_DIR"
    chmod 0700 "$BLUESTREAM_MEDIA_QUARANTINE_DIR"
    return 0
}

# Create the per-instance secret key if missing; preserve an existing one.
# Runs as root through the existing security.py logic (never reimplemented).
web_ensure_secret_key() {
    local _keyfile="$BLUESTREAM_WEB_STATE_DIR/secret_key"
    if [ -s "$_keyfile" ]; then
        bs_ok "Existing web secret key preserved ($_keyfile)"
        return 0
    fi
    if (cd "$BLUESTREAM_WEB_INSTALL_ROOT" && python3 -c 'import sys
from webapp.security import ensure_secret_key
ensure_secret_key(sys.argv[1])' "$BLUESTREAM_WEB_STATE_DIR") >/dev/null 2>&1; then
        bs_ok "Web secret key generated."
    else
        bs_warn "Web secret key generation failed; the web service will fail closed at startup."
        return 1
    fi
    return 0
}

# ---------------------------------------------------------------------------
# Install web application code + web-ctl (root-only).
# ---------------------------------------------------------------------------
web_install_files() {
    local _libdir="$BLUESTREAM_WEB_INSTALL_ROOT"

    # Web application package (root-owned; never writable by bluestream-web).
    mkdir -p "$_libdir/webapp"
    cp -a "$BS_ROOT"/webapp/. "$_libdir/webapp/"
    find "$_libdir/webapp" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
    chown -R root:root "$_libdir/webapp"
    find "$_libdir/webapp" -type d -exec chmod 0755 {} + 2>/dev/null || true
    find "$_libdir/webapp" -type f -exec chmod 0644 {} + 2>/dev/null || true

    # The read-only bridge: root-only executable. sudo (running as root) can
    # execute a root:root 0700 script; bluestream-web cannot execute it directly.
    install -o root -g root -m 0700 "$BS_ROOT/web-ctl" "$_libdir/web-ctl"

    # Verify the installed web-ctl exactly.
    local _owner _mode
    _owner="$(stat -c '%U:%G' "$_libdir/web-ctl" 2>/dev/null)"
    _mode="$(stat -c '%a' "$_libdir/web-ctl" 2>/dev/null)"
    if [ "$_owner" != "root:root" ] || [ "$_mode" != "700" ]; then
        bs_die "Installed web-ctl has unexpected ownership/mode: $_owner $_mode (want root:root 0700)"
    fi
    unset _owner _mode

    # sudoers source (installed/validated separately) and systemd unit.
    mkdir -p "$_libdir/config/sudoers"
    cp -f "$BS_ROOT"/config/sudoers/* "$_libdir/config/sudoers/"
    install -o root -g root -m 0644 "$BS_ROOT/config/systemd/bluestream-web.service" "$BLUESTREAM_WEB_UNIT"
    return 0
}

# ---------------------------------------------------------------------------
# sudoers installation: stage -> visudo -cf -> keep only if valid.
# ---------------------------------------------------------------------------
web_install_sudoers() {
    local _src="$BLUESTREAM_WEB_INSTALL_ROOT/config/sudoers/bluestream-web"
    local _staged="${BLUESTREAM_WEB_SUDOERS}.staged"
    [ -f "$_src" ] || bs_die "sudoers source missing: $_src"
    command -v visudo >/dev/null 2>&1 || bs_die "visudo not found; cannot validate the sudoers policy safely."
    install -o root -g root -m 0440 "$_src" "$_staged"
    if ! visudo -cf "$_staged" >/dev/null 2>&1; then
        rm -f "$_staged"
        bs_die "sudoers candidate failed 'visudo -cf' validation. The web console will not be granted sudo. Fix config/sudoers/bluestream-web and re-run."
    fi
    mv -f "$_staged" "$BLUESTREAM_WEB_SUDOERS"
    chown root:root "$BLUESTREAM_WEB_SUDOERS"
    chmod 0440 "$BLUESTREAM_WEB_SUDOERS"
    bs_ok "sudoers policy installed and validated ($BLUESTREAM_WEB_SUDOERS, root:root 0440)"
    return 0
}

# ---------------------------------------------------------------------------
# First admin account (preserve existing). Password via stdin only.
# ---------------------------------------------------------------------------
web_init_admin() {
    if [ -f "$BLUESTREAM_WEB_STATE_DIR/admin.json" ]; then
        if python3 -c 'import json,sys; json.load(open(sys.argv[1], encoding="utf-8"))' \
            "$BLUESTREAM_WEB_STATE_DIR/admin.json" >/dev/null 2>&1; then
            bs_ok "Existing web admin credentials preserved ($BLUESTREAM_WEB_STATE_DIR/admin.json)"
        else
            bs_warn "Existing $BLUESTREAM_WEB_STATE_DIR/admin.json appears malformed; the console will fail login safely."
            bs_warn "To reset the admin password, remove that file and re-run install.sh (or run init-admin manually)."
        fi
        return 0
    fi
    if [ -f "$BLUESTREAM_WEB_STATE_DIR/secret_key" ]; then
        bs_ok "Existing web secret key preserved"
    fi

    if [ "${NON_INTERACTIVE:-0}" = "1" ] || [ ! -t 0 ]; then
        bs_warn "No web admin account exists and this is a non-interactive install."
        bs_warn "Create it after install (password via stdin, never argv):"
        bs_warn "  printf '%s\\n' '<password>' | (cd $BLUESTREAM_WEB_INSTALL_ROOT && python3 -m webapp.security init-admin --state-dir $BLUESTREAM_WEB_STATE_DIR)"
        return 0
    fi

    local _pw1="" _pw2=""
    printf '%s' "Enter web admin password: " >&2
    read -rs _pw1 || { printf '\n' >&2; return 1; }
    printf '\n' >&2
    if [ -z "$_pw1" ]; then
        bs_warn "Empty password; skipping admin creation (run init-admin manually afterwards)."
        return 0
    fi
    printf '%s' "Confirm web admin password: " >&2
    read -rs _pw2 || { printf '\n' >&2; return 1; }
    printf '\n' >&2
    if [ "$_pw1" != "$_pw2" ]; then
        bs_warn "Passwords did not match; skipping admin creation (run init-admin manually afterwards)."
        return 0
    fi

    # Pipe the password to security.py via stdin; it never appears in argv.
    if printf '%s\n' "$_pw1" | (cd "$BLUESTREAM_WEB_INSTALL_ROOT" && python3 -m webapp.security init-admin --state-dir "$BLUESTREAM_WEB_STATE_DIR") >/dev/null 2>&1; then
        bs_ok "Web admin account initialized."
    else
        bs_warn "Web admin initialization failed; run init-admin manually afterwards."
    fi
    unset _pw1 _pw2
    return 0
}

# ---------------------------------------------------------------------------
# systemd service: reload, enable, start.
# ---------------------------------------------------------------------------
web_manage_service() {
    systemctl daemon-reload 2>/dev/null || true
    systemctl enable "$BLUESTREAM_WEB_SERVICE" 2>/dev/null || bs_warn "Could not enable $BLUESTREAM_WEB_SERVICE"
    # An upgrade over an already-active service must load the just-installed
    # application code/templates: systemctl start is a no-op on an active unit,
    # so restart instead (daemon-reload above already covered any unit changes).
    # Fresh installs are unaffected - nothing is active yet, so start normally.
    if systemctl is-active "$BLUESTREAM_WEB_SERVICE" >/dev/null 2>&1; then
        if ! systemctl restart "$BLUESTREAM_WEB_SERVICE" 2>/dev/null; then
            bs_error "Failed to restart $BLUESTREAM_WEB_SERVICE; recent journal:"
            journalctl -u "$BLUESTREAM_WEB_SERVICE" -n 20 --no-pager 2>/dev/null >&2 || true
            bs_die "bluestream-web service failed to restart. Fix the issue and re-run install.sh."
        fi
        bs_ok "$BLUESTREAM_WEB_SERVICE restarted with updated application files"
        return 0
    fi
    if ! systemctl start "$BLUESTREAM_WEB_SERVICE" 2>/dev/null; then
        bs_error "Failed to start $BLUESTREAM_WEB_SERVICE; recent journal:"
        journalctl -u "$BLUESTREAM_WEB_SERVICE" -n 20 --no-pager 2>/dev/null >&2 || true
        bs_die "bluestream-web service failed to start. Fix the issue and re-run install.sh."
    fi
    bs_ok "$BLUESTREAM_WEB_SERVICE started"
    return 0
}

# ---------------------------------------------------------------------------
# Uninstall (used by uninstall.sh; matches preserve-vs-purge conventions).
# ---------------------------------------------------------------------------
web_uninstall() {
    printf 'Stopping and disabling the web console service...\n'
    systemctl stop "$BLUESTREAM_WEB_SERVICE" 2>/dev/null || true
    systemctl disable "$BLUESTREAM_WEB_SERVICE" 2>/dev/null || true
    rm -f "$BLUESTREAM_WEB_UNIT"
    rm -f "$BLUESTREAM_WEB_SUDOERS"
    rm -f "${BLUESTREAM_WEB_SUDOERS}.staged"
    systemctl daemon-reload 2>/dev/null || true
    printf 'Web console service unit, sudoers policy and nginx console snippet removed.\n'
    printf 'Web state (%s) follows the standard preserve/purge rules.\n' "$BLUESTREAM_WEB_STATE_DIR"
    return 0
}
