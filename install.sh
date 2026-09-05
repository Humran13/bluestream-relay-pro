#!/usr/bin/env bash
#
# BlueStream Relay Pro - installer for Ubuntu Server.
#
# Idempotent: re-running repairs/updates the installation without
# destroying existing relays, playlists or media.
#
# Usage:
#   sudo bash install.sh
#   sudo bash install.sh --domain video.example.com --email admin@example.com \
#                        --with-ssl --with-ufw --assume-yes
#
# Options:
#   --domain <name>     Public domain (e.g. video.example.com)
#   --email <addr>      Administrator email (required with --with-ssl)
#   --with-ssl          Install certbot and request a Let's Encrypt cert
#   --no-ssl            Do not configure SSL (default when unset)
#   --with-ufw          Enable and configure UFW firewall
#   --no-ufw            Do not configure the firewall (default when unset)
#   --no-apt-update     Skip `apt-get update`
#   --assume-yes        Answer yes to prompts (uses safe defaults)
#   --non-interactive   No prompts; use flag values or defaults
#   --help              Show this help
#
# BlueStream Relay Pro 0.1.0 (foundation). See LICENSE for terms.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BS_ROOT="$SCRIPT_DIR"

# ---------------------------------------------------------------------------
# Option defaults
# ---------------------------------------------------------------------------
ASSUME_YES=0
NON_INTERACTIVE=0
SSL_REQUESTED=0      # 0 unset, 1 yes, 2 no
UFW_REQUESTED=0      # 0 unset, 1 yes, 2 no
APT_UPDATE=1
DOMAIN=""
EMAIL=""

show_help() {
    sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'
}

parse_args() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --domain)        bs_val2 DOMAIN --domain "$@"; shift 2 ;;
            --email)         bs_val2 EMAIL --email "$@"; shift 2 ;;
            --with-ssl)      SSL_REQUESTED=1; shift ;;
            --no-ssl)        SSL_REQUESTED=2; shift ;;
            --with-ufw)      UFW_REQUESTED=1; shift ;;
            --no-ufw)        UFW_REQUESTED=2; shift ;;
            --no-apt-update) APT_UPDATE=0; shift ;;
            --assume-yes)    ASSUME_YES=1; NON_INTERACTIVE=1; shift ;;
            --non-interactive) NON_INTERACTIVE=1; shift ;;
            --help|-h)       show_help; exit 0 ;;
            *) printf 'Unknown option: %s\n' "$1" >&2; show_help >&2; exit 1 ;;
        esac
    done
}

# ---------------------------------------------------------------------------
# Load libraries (parse_args runs after bs_val2 is available)
# ---------------------------------------------------------------------------
if [ ! -f "$BS_ROOT/lib/common.sh" ]; then
    printf 'install.sh must run from the BlueStream Relay Pro project directory.\n' >&2
    exit 1
fi
for _bs_lib in common osdetect probe health relay playlist media nginx ssl firewall backup diagnostics selftest webconsole; do
    # shellcheck source=lib/common.sh
    source "$BS_ROOT/lib/$_bs_lib.sh" || { printf 'Failed to load library %s\n' "$_bs_lib" >&2; exit 1; }
done
unset _bs_lib
bs_setup
parse_args "$@"

banner() {
    printf '%s==============================================%s\n' "$C_BOLD" "$C_RESET"
    printf '%s   BlueStream Relay Pro v%s - installer%s\n' "$C_CYAN" "$(bs_version)" "$C_RESET"
    printf '%s==============================================%s\n' "$C_BOLD" "$C_RESET"
}

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------
preflight() {
    banner
    [ "$(id -u)" -eq 0 ] || { printf 'install.sh must run as root (sudo bash install.sh).\n' >&2; exit 1; }

    os_detect
    if ! os_is_ubuntu; then
        printf 'BlueStream Relay Pro targets Ubuntu Server. Detected: %s %s\n' "$OS_NAME" "$OS_VERSION" >&2
        printf 'Installation aborted.\n' >&2
        exit 1
    fi
    if ! os_version_supported; then
        printf 'Ubuntu version %s is not on the supported list (20.04, 22.04, 24.04, 26.04).\n' "$OS_VERSION_ID" >&2
        printf 'Ubuntu 18.04 may work but is not officially supported by version 0.1.0.\n' >&2
        exit 1
    fi
    if [ "$OS_VERSION_ID" = "18.04" ]; then
        bs_warn "Ubuntu 18.04 is not officially supported by 0.1.0; continuing at your own risk."
    fi

    os_have_apt || { printf 'apt-get not found. This installer requires Ubuntu/Debian.\n' >&2; exit 1; }
    os_have_systemd || { printf 'systemd not detected. This installer requires systemd.\n' >&2; exit 1; }

    bs_info "Detected: $OS_NAME $OS_VERSION"
    return 0
}

# ---------------------------------------------------------------------------
# Public-webpage resolver (yt-dlp) - SAFE, maintainable install/update.
#
# yt-dlp is OPTIONAL: it is needed ONLY for public-webpage source URLs
# (TYPE=youtube / TYPE=web-resolver). Every direct media source (HLS, HTTP
# media, RTMP/RTMPS, RTSP, local media) works without it, so resolver
# installation can never fail the whole BlueStream install.
#
# Strategy (no curl|bash, no stale Ubuntu package as the long-term answer, no
# system-Python mutation):
#   1. If yt-dlp is already on PATH: report it (with version) and stop.
#   2. Otherwise install/refresh yt-dlp inside an ISOLATED virtualenv at
#      /usr/local/lib/bluestream/resolver-venv and expose it through a
#      /usr/local/bin/yt-dlp symlink. The venv never touches system Python
#      and can be updated later with:
#        /usr/local/lib/bluestream/resolver-venv/bin/python -m pip install -U yt-dlp
#   3. Fallbacks (still isolated where possible): pipx, then the Ubuntu
#      package, each reported as best-effort.
# ---------------------------------------------------------------------------
RESOLVER_VENV="/usr/local/lib/bluestream/resolver-venv"

resolver_install_venv() {
    # Create/refresh the isolated venv and install the CURRENT yt-dlp from
    # PyPI. Requires python3-venv + python3-pip (installed on demand).
    command -v python3 >/dev/null 2>&1 || return 1
    if [ ! -x "$RESOLVER_VENV/bin/python" ]; then
        os_pkg_install python3-venv python3-pip >/dev/null 2>&1 || true
        python3 -m venv "$RESOLVER_VENV" 2>/dev/null || return 1
    fi
    "$RESOLVER_VENV/bin/python" -m pip install --disable-pip-version-check \
        --quiet --upgrade yt-dlp 2>/dev/null || return 1
    if [ ! -x "$RESOLVER_VENV/bin/yt-dlp" ]; then
        return 1
    fi
    if [ ! -e /usr/local/bin/yt-dlp ] || [ ! -L /usr/local/bin/yt-dlp ]; then
        ln -sf "$RESOLVER_VENV/bin/yt-dlp" /usr/local/bin/yt-dlp 2>/dev/null || return 1
    fi
    return 0
}

install_resolver() {
    bs_step "Checking the public-webpage resolver (yt-dlp)"
    local ytdlp_path="" ytdlp_ver=""
    if command -v yt-dlp >/dev/null 2>&1; then
        ytdlp_path="$(command -v yt-dlp)"
        ytdlp_ver="$(yt-dlp --version 2>/dev/null | head -n 1 || true)"
        bs_ok "yt-dlp available: ${ytdlp_path}${ytdlp_ver:+ (version ${ytdlp_ver})}"
        return 0
    fi
    # Not installed: prefer the isolated managed venv.
    if resolver_install_venv; then
        ytdlp_ver="$(yt-dlp --version 2>/dev/null | head -n 1 || true)"
        bs_ok "yt-dlp installed in an isolated venv${ytdlp_ver:+ (version ${ytdlp_ver})}"
        return 0
    fi
    # Best-effort fallbacks - never fatal.
    if command -v pipx >/dev/null 2>&1; then
        pipx install --quiet yt-dlp 2>/dev/null && {
            bs_ok "yt-dlp installed via pipx"
            return 0
        }
    fi
    os_pkg_install yt-dlp 2>/dev/null || true
    if command -v yt-dlp >/dev/null 2>&1; then
        bs_ok "yt-dlp installed from the Ubuntu package (consider the managed venv for newer versions)"
    else
        bs_warn "yt-dlp not installed - public 'webpage' source URLs (YouTube Live and other public pages supported by the resolver) will not resolve until you install it. All direct media source types are unaffected."
    fi
    return 0
}

# ---------------------------------------------------------------------------
# Package installation
# ---------------------------------------------------------------------------
install_packages() {
    bs_step "Installing base packages (nginx, ffmpeg, curl, tools)"
    if [ "$APT_UPDATE" = "1" ]; then
        bs_info "Updating package metadata..."
        os_apt_update
    fi
    # shellcheck disable=SC2046
    os_pkg_install nginx ffmpeg curl ca-certificates coreutils util-linux libnginx-mod-rtmp \
        $(web_packages) \
        || bs_die "Failed to install base packages."
    web_verify_packages

    install_resolver

    if [ "$SSL_REQUESTED" = "1" ]; then
        bs_step "Installing certbot for Let's Encrypt SSL"
        os_pkg_install certbot python3-certbot-nginx \
            || bs_die "Failed to install certbot. SSL will not be configured."
    fi

    if [ "$UFW_REQUESTED" = "1" ]; then
        bs_step "Installing UFW firewall"
        os_pkg_install ufw || bs_warn "Failed to install UFW."
    fi
    return 0
}

# ---------------------------------------------------------------------------
# Directory / user / permission scaffolding
# ---------------------------------------------------------------------------
create_user_and_dirs() {
    bs_step "Creating BlueStream user, directories and permissions"

    # Dedicated unprivileged service account.
    #
    # The primary group is created explicitly and is NOT left to /etc/login.defs
    # USERGROUPS_ENAB defaults, because the production runners exec
    # `setpriv --regid "$BLUESTREAM_GROUP"` and the named group must always exist.
    if ! getent group "$BLUESTREAM_GROUP" >/dev/null 2>&1; then
        groupadd --system "$BLUESTREAM_GROUP" || bs_die "Failed to create system group $BLUESTREAM_GROUP"
        bs_ok "Created system group $BLUESTREAM_GROUP"
    fi
    if ! getent group "$BLUESTREAM_GROUP" >/dev/null 2>&1; then
        bs_die "Required system group '$BLUESTREAM_GROUP' could not be created or resolved."
    fi

    if ! id "$BLUESTREAM_USER" >/dev/null 2>&1; then
        useradd --system \
            --gid "$BLUESTREAM_GROUP" \
            --home-dir /nonexistent \
            --shell /usr/sbin/nologin \
            --no-create-home "$BLUESTREAM_USER" || bs_die "Failed to create system user $BLUESTREAM_USER"
        bs_ok "Created system user $BLUESTREAM_USER (primary group: $BLUESTREAM_GROUP)"
    fi
    if ! id "$BLUESTREAM_USER" >/dev/null 2>&1; then
        bs_die "Required system user '$BLUESTREAM_USER' could not be created or resolved."
    fi

    # If the user pre-existed with a different primary group, keep its
    # memberships untouched; the runners use --regid "$BLUESTREAM_GROUP"
    # explicitly, so the named group above is authoritative.
    if [ "$(id -gn "$BLUESTREAM_USER" 2>/dev/null)" != "$BLUESTREAM_GROUP" ]; then
        bs_warn "User '$BLUESTREAM_USER' primary group is '$(id -gn "$BLUESTREAM_USER" 2>/dev/null)', not '$BLUESTREAM_GROUP'. Runners use --regid '$BLUESTREAM_GROUP' explicitly, so this is non-fatal."
    fi

    # nginx worker user (created by the nginx package; verify it exists).
    if ! id "$BLUESTREAM_NGINX_USER" >/dev/null 2>&1; then
        bs_warn "Nginx user '$BLUESTREAM_NGINX_USER' not found; HLS may not be readable by nginx."
    fi

    # Config tree (root-only).
    mkdir -p "$BLUESTREAM_ETC_DIR" "$BLUESTREAM_RELAY_CONF_DIR" "$BLUESTREAM_PLAYLIST_CONF_DIR" "$BLUESTREAM_DEST_CONF_DIR"
    chown root:root "$BLUESTREAM_ETC_DIR" "$BLUESTREAM_RELAY_CONF_DIR" "$BLUESTREAM_PLAYLIST_CONF_DIR" "$BLUESTREAM_DEST_CONF_DIR"
    chmod 0700 "$BLUESTREAM_ETC_DIR" "$BLUESTREAM_RELAY_CONF_DIR" "$BLUESTREAM_PLAYLIST_CONF_DIR" "$BLUESTREAM_DEST_CONF_DIR"

    # Managed data tree.
    mkdir -p "$BLUESTREAM_VAR_DIR" "$BLUESTREAM_MEDIA_DIR" "$BLUESTREAM_RUN_DIR" "$BLUESTREAM_BACKUP_DIR" \
        "$BLUESTREAM_PLAYLIST_CACHE_DIR"
    chown root:"$BLUESTREAM_GROUP" "$BLUESTREAM_VAR_DIR"
    # 0751: bluestream-relay group keeps read/traverse; other users (e.g.
    # bluestream-web reaching /var/lib/bluestream/web) get execute/traverse ONLY,
    # so the parent cannot be listed and each child dir remains independently
    # protected. Enforced on every re-run (idempotent).
    chmod 0751 "$BLUESTREAM_VAR_DIR"
    chown root:"$BLUESTREAM_GROUP" "$BLUESTREAM_MEDIA_DIR"
    chmod 0750 "$BLUESTREAM_MEDIA_DIR"
    chown "$BLUESTREAM_USER":"$BLUESTREAM_GROUP" "$BLUESTREAM_RUN_DIR"
    chmod 0750 "$BLUESTREAM_RUN_DIR"
    chown root:root "$BLUESTREAM_BACKUP_DIR"
    chmod 0700 "$BLUESTREAM_BACKUP_DIR"
    # Playlist normalization cache: the root playlist runtime wrapper publishes
    # baseline artifacts here; the dropped-privilege FFmpeg concat process reads
    # them through group access. Never writable by web or relay users.
    chown root:"$BLUESTREAM_GROUP" "$BLUESTREAM_PLAYLIST_CACHE_DIR"
    chmod 0750 "$BLUESTREAM_PLAYLIST_CACHE_DIR"

    # Web / HLS tree.
    mkdir -p "$BLUESTREAM_WWW_DIR" "$BLUESTREAM_WEB_DIR" "$BLUESTREAM_HLS_ROOT" \
        "$BLUESTREAM_HLS_RELAY_DIR" "$BLUESTREAM_HLS_PLAYLIST_DIR"
    chown root:root "$BLUESTREAM_WWW_DIR"
    chmod 0755 "$BLUESTREAM_WWW_DIR"
    chown root:root "$BLUESTREAM_WEB_DIR"
    chmod 0755 "$BLUESTREAM_WEB_DIR"
    # nginx-rtmp (www-data) owns the HLS output tree. FFmpeg publishes to the
    # private local RTMP socket and never writes HLS files directly.
    chown "$BLUESTREAM_NGINX_USER":"$BLUESTREAM_NGINX_USER" "$BLUESTREAM_HLS_ROOT" \
        "$BLUESTREAM_HLS_RELAY_DIR" "$BLUESTREAM_HLS_PLAYLIST_DIR"
    # GNU chmod preserves setgid on directories for numeric modes, so clear it
    # explicitly before pinning the exact 0750 mode.
    chmod g-s "$BLUESTREAM_HLS_ROOT" "$BLUESTREAM_HLS_RELAY_DIR" "$BLUESTREAM_HLS_PLAYLIST_DIR"
    chmod 0750 "$BLUESTREAM_HLS_ROOT" "$BLUESTREAM_HLS_RELAY_DIR" "$BLUESTREAM_HLS_PLAYLIST_DIR"

    # Upgrade/repair: correct any existing managed HLS output directories
    # (per-relay and per-playlist) to the nginx-worker model. Directories only;
    # generated files and customer media are left untouched. GNU chmod preserves
    # setgid on directories for numeric modes, so clear it explicitly before
    # pinning each directory to exactly 0750.
    find "$BLUESTREAM_HLS_RELAY_DIR" "$BLUESTREAM_HLS_PLAYLIST_DIR" -type d \
        -exec chown "${BLUESTREAM_NGINX_USER}:${BLUESTREAM_NGINX_USER}" {} + 2>/dev/null || true
    find "$BLUESTREAM_HLS_RELAY_DIR" "$BLUESTREAM_HLS_PLAYLIST_DIR" -type d \
        -exec chmod g-s {} + 2>/dev/null || true
    find "$BLUESTREAM_HLS_RELAY_DIR" "$BLUESTREAM_HLS_PLAYLIST_DIR" -type d \
        -exec chmod 0750 {} + 2>/dev/null || true

    # A fresh media permissions file in case of re-run on existing data.
    find "$BLUESTREAM_MEDIA_DIR" -maxdepth 1 -type f ! -name '.*' \
        -exec chown root:"$BLUESTREAM_GROUP" {} + 2>/dev/null || true
    find "$BLUESTREAM_MEDIA_DIR" -maxdepth 1 -type f ! -name '.*' \
        -exec chmod 0640 {} + 2>/dev/null || true

    # Web console account and production state (GUI-1A.3B).
    web_create_user
    web_create_state

    bs_ok "User, directories and permissions ready"
    return 0
}

# ---------------------------------------------------------------------------
# File installation
# ---------------------------------------------------------------------------
install_files() {
    bs_step "Installing BlueStream files"

    local libdir="/usr/local/lib/bluestream"
    mkdir -p "$libdir/lib" "$libdir/config/nginx" "$libdir/config/systemd" "$libdir/docs"

    # Libraries and scripts.
    cp -f "$BS_ROOT"/lib/*.sh "$libdir/lib/"
    # Root trust chain: root web-ctl sources these libraries, so the installed
    # copies must be root-owned and not writable by any unprivileged user
    # regardless of the source checkout's ownership or mode bits.
    chown root:root "$libdir"/lib/*.sh
    chmod 0644 "$libdir"/lib/*.sh
    cp -f "$BS_ROOT"/config/nginx/*.template "$libdir/config/nginx/"
    cp -f "$BS_ROOT"/config/nginx/*.conf "$libdir/config/nginx/"
    cp -f "$BS_ROOT"/config/systemd/*.service "$libdir/config/systemd/"
    # Production runner scripts: explicit ownership and mode so systemd
    # ExecStart always works regardless of the source checkout's modes.
    install -o root -g root -m 0755 "$BS_ROOT/config/systemd/run-relay.sh" "$libdir/run-relay.sh"
    install -o root -g root -m 0755 "$BS_ROOT/config/systemd/run-playlist.sh" "$libdir/run-playlist.sh"
    # GUI-8A: one-time scheduled playlist start wrapper.
    install -o root -g root -m 0755 "$BS_ROOT/config/systemd/run-playlist-start.sh" "$libdir/run-playlist-start.sh"
    cp -f "$BS_ROOT/VERSION" "$libdir/VERSION"
    cp -f "$BS_ROOT/CHANGELOG.md" "$libdir/CHANGELOG.md"
    cp -f "$BS_ROOT/README.md" "$libdir/README.md"
    cp -f "$BS_ROOT/LICENSE" "$libdir/LICENSE"
    cp -f "$BS_ROOT/uninstall.sh" "$libdir/uninstall.sh"
    cp -f "$BS_ROOT"/docs/*.md "$libdir/docs/"

    # Manager command.
    install -o root -g root -m 0755 "$BS_ROOT/bluestream-manager" /usr/local/sbin/bluestream-manager

    # Lightweight status script.
    install -o root -g root -m 0755 "$BS_ROOT/status.sh" /usr/local/bin/bluestream-status

    # systemd unit templates.
    install -o root -g root -m 0644 "$BS_ROOT/config/systemd/bluestream-relay@.service" /etc/systemd/system/bluestream-relay@.service
    install -o root -g root -m 0644 "$BS_ROOT/config/systemd/bluestream-playlist@.service" /etc/systemd/system/bluestream-playlist@.service
    # GUI-8A: one-time playlist start oneshot template (triggered only by a
    # generated bluestream-schedule-<name>.timer).
    install -o root -g root -m 0644 "$BS_ROOT/config/systemd/bluestream-playlist-start@.service" /etc/systemd/system/bluestream-playlist-start@.service

    # Web console application + web-ctl (GUI-1A.3B). web_install_files also
    # installs the systemd unit and the sudoers source; sudoers itself is
    # staged/validated/installed later by web_install_sudoers.
    web_install_files

    # Verify the production runners: present, root:root owned, executable.
    local _runner _runner_owner
    for _runner in run-relay.sh run-playlist.sh run-playlist-start.sh; do
        if [ ! -f "$libdir/$_runner" ]; then
            bs_die "Installed runner missing: $libdir/$_runner"
        fi
        _runner_owner="$(stat -c '%U:%G' "$libdir/$_runner" 2>/dev/null)"
        if [ "$_runner_owner" != "root:root" ]; then
            bs_die "Installed runner ownership incorrect: $libdir/$_runner ($_runner_owner)"
        fi
        if [ ! -x "$libdir/$_runner" ]; then
            bs_die "Installed runner is not executable: $libdir/$_runner"
        fi
    done
    unset _runner _runner_owner

    # Web player.
    cp -f "$BS_ROOT"/web/index.html "$BS_ROOT"/web/player.js "$BS_ROOT"/web/style.css "$BLUESTREAM_WEB_DIR/"
    chown root:root "$BLUESTREAM_WEB_DIR"/*
    chmod 0644 "$BLUESTREAM_WEB_DIR"/*

    systemctl daemon-reload 2>/dev/null || true

    bs_ok "BlueStream files installed"
    return 0
}

# ---------------------------------------------------------------------------
# Server configuration prompts
# ---------------------------------------------------------------------------
configure_server_conf() {
    bs_step "Server configuration"

    # Preserve existing values unless flags were provided.
    if [ -f "$BLUESTREAM_SERVER_CONF" ]; then
        bs_load_server_conf
        bs_info "Existing server configuration found and preserved."
    fi

    local new_domain="$BLUESTREAM_DOMAIN"
    local new_email="$BLUESTREAM_ADMIN_EMAIL"

    if [ -n "$DOMAIN" ]; then
        new_domain="$DOMAIN"
    elif [ "$NON_INTERACTIVE" = "0" ]; then
        bs_prompt new_domain "Public domain for HLS URLs (e.g. video.example.com)" "$new_domain" || return 1
    fi
    if [ -n "$new_domain" ]; then
        bs_valid_domain "$new_domain" || bs_die "Invalid domain: $new_domain"
    fi

    if [ -n "$EMAIL" ]; then
        new_email="$EMAIL"
    elif [ "$SSL_REQUESTED" = "1" ] && [ "$NON_INTERACTIVE" = "0" ]; then
        bs_prompt new_email "Administrator email (for Let's Encrypt)" "$new_email" || return 1
    fi
    if [ -n "$new_email" ]; then
        bs_valid_email "$new_email" || bs_die "Invalid email address."
    fi

    if [ "$SSL_REQUESTED" = "1" ] && [ -z "$new_email" ]; then
        bs_die "--with-ssl requires an administrator email (--email)."
    fi

    BLUESTREAM_DOMAIN="$new_domain"
    BLUESTREAM_ADMIN_EMAIL="$new_email"
    bs_save_server_conf
    bs_ok "Server configuration written to $BLUESTREAM_SERVER_CONF"
    return 0
}

# ---------------------------------------------------------------------------
# Nginx / SSL / firewall activation
# ---------------------------------------------------------------------------
configure_nginx() {
    bs_step "Configuring nginx"
    nginx_gen_config
    nginx_site_enable
    nginx_test || bs_die "Nginx configuration failed validation"
    systemctl enable nginx 2>/dev/null || true
    systemctl start nginx 2>/dev/null || true
    systemctl reload nginx 2>/dev/null || true
    bs_ok "Nginx configured and running"
    return 0
}

# ---------------------------------------------------------------------------
# Web console configuration (runs AFTER ssl_issue so the final SSL state is
# known when writing the secure_cookie deployment setting).
# ---------------------------------------------------------------------------
configure_web_console() {
    bs_step "Configuring the BlueStream web console"
    web_ensure_secret_key
    web_configure_state
    web_init_admin
    web_install_sudoers
    # Fix ownership of freshly-created admin.json/secret_key (security.py runs
    # as root) to root:bluestream-web 0640, and ensure /run/sudo exists.
    web_create_state
    web_manage_service
    return 0
}

# ---------------------------------------------------------------------------
# Post-install information
# ---------------------------------------------------------------------------
post_install_info() {
    banner
    printf '%s\n' \
        "" \
        "Installation complete." \
        "" \
        "  Manager : sudo bluestream-manager" \
        "  Status  : sudo bluestream-status" \
        "  Version : $(bs_version)" \
        "" \
        "Public URLs (once a relay or playlist is running):"
    printf '  M3U8   : %s\n' "$(bs_public_base)/hls/relay/<name>/index.m3u8"
    printf '          %s\n' "$(bs_public_base)/hls/playlist/<name>/index.m3u8"
    printf '  Player : %s\n' "$(bs_public_base)/player/?relay=<name>"
    printf '          %s\n' "$(bs_public_base)/player/?playlist=<name>"
    printf '  Console: %s\n' "$(bs_public_base)/console/"
    printf '%s\n' \
        "" \
        "Next steps:" \
        "  1. sudo bluestream-manager" \
        "  2. Relay Management > Add Relay (or add a source URL)" \
        "  3. Media Management > Import Media (for playlists)" \
        "  4. Run Diagnostics and the Self-Test from the manager." \
        "" \
        "Documentation: /usr/local/lib/bluestream/docs/" \
        ""
    return 0
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
    if [ "$ASSUME_YES" = "1" ]; then
        export BS_ASSUME_YES=1
    fi
    preflight
    install_packages
    if ! command -v setpriv >/dev/null 2>&1; then
        bs_die "setpriv (provided by util-linux) is required but was not found after package installation. Fix package installation and re-run. FFmpeg will never run as root."
    fi
    bs_ok "setpriv available (privilege-drop prerequisite satisfied)"
    create_user_and_dirs
    install_files
    configure_server_conf
    configure_nginx

    if [ "$SSL_REQUESTED" = "1" ]; then
        ssl_issue
    fi

    # Web console deployment integration (after final SSL state is known).
    configure_web_console

    if [ "$UFW_REQUESTED" = "1" ]; then
        firewall_configure
    fi

    post_install_info
    return 0
}

main



