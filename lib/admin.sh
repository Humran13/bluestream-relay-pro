#!/usr/bin/env bash
# BlueStream Relay Pro - web console administrator account management.
#
# Deliberately independent of lib/webconsole.sh, which handles the much
# heavier INSTALL-TIME-ONLY concerns (packages, system user/group creation,
# sudoers staging/validation, systemd unit rendering). bluestream-manager
# needs only what is here - checking/reporting admin status and creating
# the first admin account after install when install.sh itself could not
# reach a terminal - without pulling in webconsole.sh's full install-time
# surface (see webapp/test_install.py HttpsFollowupTests.
# test_16_no_webconsole_dependency_in_manager for why that separation is
# enforced; this split keeps that test meaningful while still shipping
# `bluestream-manager admin status|init`).
#
# Password handling rules (do not relax):
#   - Never accepted via a command-line argument/flag.
#   - Never logged, never printed, never written to disk in plaintext.
#   - Read only via a hidden (no-echo) prompt (bs_prompt_hidden, which
#     falls back from stdin to /dev/tty - see lib/common.sh - so this also
#     works under `curl ... | sudo bash`) or piped over stdin to
#     `python3 -m webapp.security init-admin` for the fully non-interactive
#     command.
#   - Hashed by the existing webapp/security.py scrypt-based implementation
#     only; no new authentication system or dependency is introduced here.
#   - An existing admin account is never overwritten, reset, or duplicated;
#     webapp.security.init_admin() itself refuses if admin.json exists,
#     which protects against a race between the existence check and the
#     write, not just this shell-level check.
#
# BlueStream Relay Pro 0.1.0 (foundation). See LICENSE for terms.

if [ -n "${BLUESTREAM_ADMIN_LOADED:-}" ]; then
    return 0 2>/dev/null || exit 0
fi
BLUESTREAM_ADMIN_LOADED=1

# Mirror lib/webconsole.sh's path constants (duplicated, not sourced from
# it, to keep this file independently loadable). Both files assign the same
# literal values, so loading either or both in either order is harmless.
BLUESTREAM_WEB_STATE_DIR="${BLUESTREAM_WEB_STATE_DIR:-/var/lib/bluestream/web}"
BLUESTREAM_WEB_INSTALL_ROOT="${BLUESTREAM_WEB_INSTALL_ROOT:-/usr/local/lib/bluestream}"
BLUESTREAM_WEB_GROUP="${BLUESTREAM_WEB_GROUP:-bluestream-web}"
ADMIN_USERNAME_DEFAULT="admin"

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

web_admin_exists() {
    [ -f "$BLUESTREAM_WEB_STATE_DIR/admin.json" ]
}

# Human-readable admin status for summaries (never prints the password/hash).
web_admin_status() {
    if web_admin_exists; then
        local _uname
        _uname="$(python3 -c '
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
    print(d.get("username", ""))
except Exception:
    print("")
' "$BLUESTREAM_WEB_STATE_DIR/admin.json" 2>/dev/null)"
        if [ -n "$_uname" ]; then
            printf 'configured (username: %s)' "$_uname"
        else
            printf 'present but unreadable/malformed'
        fi
    else
        printf 'pending (no admin account created yet)'
    fi
}

# Shared prompt+create flow (used by both install-time and
# `bluestream-manager admin init`). Caller must already have confirmed no
# admin record exists and that an interactive source is reachable
# (bs_input_source). Returns 0 on success, 1 on any failure/cancellation
# (caller decides how to report it; no secret is ever left behind on a
# logged message).
web_admin_prompt_and_create() {
    local _username="" _pw1="" _pw2=""
    bs_prompt _username "Web admin username" "$ADMIN_USERNAME_DEFAULT" || _username="$ADMIN_USERNAME_DEFAULT"
    [ -n "$_username" ] || _username="$ADMIN_USERNAME_DEFAULT"
    case "$_username" in
        *[!A-Za-z0-9_-]*)
            bs_warn "Invalid username '$_username' (letters, digits, '-', '_' only); using default '$ADMIN_USERNAME_DEFAULT'."
            _username="$ADMIN_USERNAME_DEFAULT"
            ;;
    esac

    bs_prompt_hidden _pw1 "Enter web admin password" || return 1
    if [ -z "$_pw1" ]; then
        unset _pw1
        return 1
    fi
    bs_prompt_hidden _pw2 "Confirm web admin password" || { unset _pw1; return 1; }
    if [ "$_pw1" != "$_pw2" ]; then
        unset _pw1 _pw2
        return 1
    fi

    # Pipe the password to security.py via stdin only; it never appears in
    # argv, is never logged, and is never written to disk in plaintext.
    if printf '%s\n' "$_pw1" | (cd "$BLUESTREAM_WEB_INSTALL_ROOT" && python3 -m webapp.security init-admin --state-dir "$BLUESTREAM_WEB_STATE_DIR" --username "$_username") >/dev/null 2>&1; then
        unset _pw1 _pw2
        bs_ok "Web admin account '$_username' initialized."
        # Permission fix (root created admin.json/secret_key): web-only read
        # access, never writable by the unprivileged web process.
        chown root:"$BLUESTREAM_WEB_GROUP" "$BLUESTREAM_WEB_STATE_DIR/admin.json" "$BLUESTREAM_WEB_STATE_DIR/secret_key" 2>/dev/null
        chmod 0640 "$BLUESTREAM_WEB_STATE_DIR/admin.json" "$BLUESTREAM_WEB_STATE_DIR/secret_key" 2>/dev/null
        return 0
    fi
    unset _pw1 _pw2
    return 1
}

# Install-time entry point (called from install.sh's configure_web_console).
web_init_admin() {
    if [ -f "$BLUESTREAM_WEB_STATE_DIR/admin.json" ]; then
        if python3 -c 'import json,sys; json.load(open(sys.argv[1], encoding="utf-8"))' \
            "$BLUESTREAM_WEB_STATE_DIR/admin.json" >/dev/null 2>&1; then
            bs_ok "Existing web admin credentials preserved ($BLUESTREAM_WEB_STATE_DIR/admin.json)"
        else
            bs_warn "Existing $BLUESTREAM_WEB_STATE_DIR/admin.json appears malformed; the console will fail login safely."
            bs_warn "To reset the admin password, remove that file and re-run 'sudo bluestream-manager admin init'."
        fi
        return 0
    fi
    if [ -f "$BLUESTREAM_WEB_STATE_DIR/secret_key" ]; then
        bs_ok "Existing web secret key preserved"
    fi

    if ! bs_input_source >/dev/null 2>&1; then
        bs_warn "No web admin account exists, and no interactive terminal (stdin or /dev/tty) is reachable."
        bs_warn "BlueStream Relay Pro IS installed, but admin onboarding is NOT complete - the dashboard cannot be"
        bs_warn "logged into yet. Finish it with (password via stdin only - never argv, never shell history):"
        bs_warn "  printf '%s\\n' '<password>' | (cd $BLUESTREAM_WEB_INSTALL_ROOT && python3 -m webapp.security init-admin --state-dir $BLUESTREAM_WEB_STATE_DIR --username <name>)"
        bs_warn "or simply, once bluestream-manager is installed: sudo bluestream-manager admin init"
        return 0
    fi

    bs_info "Setting up the web admin account (reads from your terminal via /dev/tty if needed, even under curl|bash)."
    if ! web_admin_prompt_and_create; then
        bs_warn "Admin setup did not complete (cancelled, empty, or mismatched password)."
        bs_warn "BlueStream Relay Pro IS installed, but admin onboarding is NOT complete yet. Finish it with:"
        bs_warn "  sudo bluestream-manager admin init"
    fi
    return 0
}

# Post-install admin creation entry point for `bluestream-manager admin
# init`. Refuses outright if an admin already exists (never overwrites,
# never allows a duplicate) - this is the ONLY way admin setup can be
# completed later when the install itself could not reach a terminal.
web_admin_init_cli() {
    if web_admin_exists; then
        bs_error "An admin account already exists and is preserved. Refusing to create or overwrite one."
        bs_info "Status: $(web_admin_status)"
        bs_info "To deliberately reset it, remove $BLUESTREAM_WEB_STATE_DIR/admin.json yourself first, then re-run this."
        return 1
    fi
    bs_require_root
    web_ensure_secret_key
    if ! bs_input_source >/dev/null 2>&1; then
        bs_error "No interactive terminal (stdin or /dev/tty) is reachable."
        bs_info "Pipe the password via stdin instead (never argv, never shell history):"
        bs_info "  printf '%s\\n' '<password>' | (cd $BLUESTREAM_WEB_INSTALL_ROOT && python3 -m webapp.security init-admin --state-dir $BLUESTREAM_WEB_STATE_DIR --username <name>)"
        return 1
    fi
    if web_admin_prompt_and_create; then
        return 0
    fi
    bs_error "Admin setup failed, was cancelled, or passwords did not match. No admin account was created."
    return 1
}

# ---------------------------------------------------------------------------
# Lockout recovery: reset the EXISTING admin's password without needing the
# old one. Gated on local root/sudo access (bs_require_root, same as every
# other admin-management entry point here) plus an explicit confirmation -
# this is a deliberately destructive action on an existing credential, not
# a routine one, so it is never offered as a casual default and never runs
# non-interactively without a terminal (no env-var/flag shortcut, by
# design, to keep the new password off argv/history exactly like init).
# ---------------------------------------------------------------------------
web_admin_reset_password_cli() {
    if ! web_admin_exists; then
        bs_error "No admin account exists yet - there is nothing to reset."
        bs_info "Use 'sudo bluestream-manager admin init' to create the first account instead."
        return 1
    fi
    bs_require_root
    bs_warn "This resets the password for the EXISTING admin account ($(web_admin_status))."
    bs_warn "The current password will stop working immediately. You do NOT need to know it -"
    bs_warn "this is for a locked-out admin - but you DO need local root/sudo access to run this."
    bs_confirm "Continue and reset the admin password now?" || { bs_info "Cancelled; nothing was changed."; return 1; }

    if ! bs_input_source >/dev/null 2>&1; then
        bs_error "No interactive terminal (stdin or /dev/tty) is reachable."
        bs_info "Pipe the new password via stdin instead (never argv, never shell history):"
        bs_info "  printf '%s\\n' '<new-password>' | (cd $BLUESTREAM_WEB_INSTALL_ROOT && python3 -m webapp.security reset-admin-password --state-dir $BLUESTREAM_WEB_STATE_DIR)"
        return 1
    fi

    local _pw1="" _pw2=""
    bs_prompt_hidden _pw1 "Enter the NEW web admin password" || return 1
    if [ -z "$_pw1" ]; then
        unset _pw1
        bs_error "Empty password; aborted. Nothing was changed."
        return 1
    fi
    bs_prompt_hidden _pw2 "Confirm the NEW web admin password" || { unset _pw1; return 1; }
    if [ "$_pw1" != "$_pw2" ]; then
        unset _pw1 _pw2
        bs_error "Passwords did not match; aborted. Nothing was changed."
        return 1
    fi

    if printf '%s\n' "$_pw1" | (cd "$BLUESTREAM_WEB_INSTALL_ROOT" && python3 -m webapp.security reset-admin-password --state-dir "$BLUESTREAM_WEB_STATE_DIR") >/dev/null 2>&1; then
        unset _pw1 _pw2
        chown root:"$BLUESTREAM_WEB_GROUP" "$BLUESTREAM_WEB_STATE_DIR/admin.json" 2>/dev/null
        chmod 0640 "$BLUESTREAM_WEB_STATE_DIR/admin.json" 2>/dev/null
        bs_ok "Admin password reset. The username is unchanged ($(web_admin_status))."
        return 0
    fi
    unset _pw1 _pw2
    bs_error "Password reset failed. Nothing was changed."
    return 1
}

# end of admin.sh
