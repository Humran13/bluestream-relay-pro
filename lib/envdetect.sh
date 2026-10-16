#!/usr/bin/env bash
# BlueStream Relay Pro - installation environment detection.
#
# Classifies the host BEFORE anything is installed or modified, so install.sh
# can choose a safe integration mode instead of assuming every VPS looks the
# same. Detection is read-only: every function here only inspects the host
# (files, binaries, systemd unit state, listening sockets) and never writes
# anything.
#
# Panel-presence checks are based on each panel's PUBLICLY DOCUMENTED install
# layout (its own CLI tool name and/or install directory), not on live
# testing against a real installation of each panel - no VPS or panel
# software license was available to verify these against the real product.
# They are deliberately conservative: a markerless/ambiguous host is treated
# as MANAGED (the safer, more isolated choice) rather than guessed as FRESH.
#
# BlueStream Relay Pro 0.1.0 (foundation). See LICENSE for terms.

if [ -n "${BLUESTREAM_ENVDETECT_LOADED:-}" ]; then
    return 0 2>/dev/null || exit 0
fi
BLUESTREAM_ENVDETECT_LOADED=1

# ---------------------------------------------------------------------------
# Results (populated by bs_env_detect). Every *_PRESENT flag is "yes"/"no".
# ---------------------------------------------------------------------------
BS_ENV_NGINX_PRESENT="no"
BS_ENV_NGINX_ACTIVE="no"
BS_ENV_APACHE_PRESENT="no"
BS_ENV_CADDY_PRESENT="no"
BS_ENV_PANEL=""          # empty, or one of: cloudpanel aapanel hestiacp plesk
                         # ispconfig virtualmin nginxproxymanager
BS_ENV_DOCKER_PRESENT="no"
BS_ENV_PORT80_FREE="no"
BS_ENV_PORT443_FREE="no"
BS_ENV_BLUESTREAM_EXISTING="no"
# "yes" only when nginx is present AND already has at least one enabled site
# other than the stock Debian/Ubuntu "default" stub - i.e. POSITIVE evidence
# that something else already uses it for real. This is the deciding signal
# for owned vs. isolated when no known panel/Apache/Caddy marker fired: the
# mere ABSENCE of a recognized panel is never treated as evidence of
# ownership (see bs_env_classify).
BS_ENV_NGINX_HAS_OTHER_SITES="no"
# Final classification. One of: fresh | plain_nginx | managed | alt_webserver
# | nginx_other_sites | nginx_inactive | unknown. See bs_env_classify for
# exactly what drives each outcome.
BS_ENV_MODE="unknown"
# Derived from BS_ENV_MODE: "owned" (BlueStream installs/manages the system
# nginx directly, today's existing behavior) or "isolated" (BlueStream never
# touches the system nginx/Apache/Caddy/panel config at all and runs its own
# dedicated, independently-configured nginx instance - see lib/nginx.sh
# nginx_isolated_* functions).
BS_NGINX_STRATEGY="owned"

# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------
bs_env_check_nginx() {
    BS_ENV_NGINX_PRESENT="no"
    BS_ENV_NGINX_ACTIVE="no"
    if command -v nginx >/dev/null 2>&1 || os_pkg_installed nginx 2>/dev/null; then
        BS_ENV_NGINX_PRESENT="yes"
    fi
    if [ "$(systemctl is-active nginx 2>/dev/null)" = "active" ]; then
        BS_ENV_NGINX_ACTIVE="yes"
    fi
}

bs_env_check_apache() {
    BS_ENV_APACHE_PRESENT="no"
    if command -v apache2 >/dev/null 2>&1 || command -v httpd >/dev/null 2>&1 \
        || os_pkg_installed apache2 2>/dev/null; then
        BS_ENV_APACHE_PRESENT="yes"
    fi
    if [ "$(systemctl is-active apache2 2>/dev/null)" = "active" ] \
        || [ "$(systemctl is-active httpd 2>/dev/null)" = "active" ]; then
        BS_ENV_APACHE_PRESENT="yes"
    fi
}

bs_env_check_caddy() {
    BS_ENV_CADDY_PRESENT="no"
    if command -v caddy >/dev/null 2>&1 || os_pkg_installed caddy 2>/dev/null; then
        BS_ENV_CADDY_PRESENT="yes"
    fi
    [ "$(systemctl is-active caddy 2>/dev/null)" = "active" ] && BS_ENV_CADDY_PRESENT="yes"
}

bs_env_check_docker() {
    BS_ENV_DOCKER_PRESENT="no"
    command -v docker >/dev/null 2>&1 && BS_ENV_DOCKER_PRESENT="yes"
}

# Positive-ownership check: does nginx already serve something real? Looks
# at sites-enabled (Debian/Ubuntu convention) for anything besides the stock
# "default" symlink, and at conf.d for anything besides BlueStream's own
# generated main-context RTMP include (60-bluestream-rtmp.conf) and the
# package's own default.conf, if present. ANY match means nginx is already
# in real use by something BlueStream did not create, so ownership can never
# be assumed from "no recognized panel" alone - see bs_env_classify.
bs_env_check_nginx_vhosts() {
    BS_ENV_NGINX_HAS_OTHER_SITES="no"
    local f base
    if [ -d /etc/nginx/sites-enabled ]; then
        shopt -s nullglob
        for f in /etc/nginx/sites-enabled/*; do
            base="$(basename "$f")"
            case "$base" in
                default|bluestream) continue ;;
            esac
            BS_ENV_NGINX_HAS_OTHER_SITES="yes"
        done
        shopt -u nullglob
    fi
    if [ -d /etc/nginx/conf.d ]; then
        shopt -s nullglob
        for f in /etc/nginx/conf.d/*.conf; do
            base="$(basename "$f")"
            case "$base" in
                default.conf|bluestream.conf) continue ;;
            esac
            BS_ENV_NGINX_HAS_OTHER_SITES="yes"
        done
        shopt -u nullglob
    fi
}

# A panel-specific CLI tool, if present, is the strongest and most stable
# signal (panels routinely restructure their own internal directories across
# versions; their documented CLI entry point changes far less often).
bs_env_check_panel() {
    BS_ENV_PANEL=""
    if command -v clpctl >/dev/null 2>&1 || [ -d /etc/cloudpanel ] \
        || [ -d /home/clp ]; then
        BS_ENV_PANEL="cloudpanel"; return 0
    fi
    if [ -d /www/server/panel ] || command -v bt >/dev/null 2>&1; then
        BS_ENV_PANEL="aapanel"; return 0
    fi
    if [ -d /usr/local/hestia ] || command -v v-list-users >/dev/null 2>&1; then
        BS_ENV_PANEL="hestiacp"; return 0
    fi
    if [ -d /usr/local/psa ] || command -v plesk >/dev/null 2>&1; then
        BS_ENV_PANEL="plesk"; return 0
    fi
    if [ -d /usr/local/ispconfig ]; then
        BS_ENV_PANEL="ispconfig"; return 0
    fi
    if [ -d /usr/share/webmin ] || [ -d /etc/webmin ]; then
        BS_ENV_PANEL="virtualmin"; return 0
    fi
    # Nginx Proxy Manager is normally run as a Docker container, not a host
    # package, so detection here is best-effort and requires `docker` to be
    # usable without a password prompt (a locked-down docker CLI simply
    # yields no match, which is the safe direction to fail in).
    if [ "$BS_ENV_DOCKER_PRESENT" = "yes" ]; then
        if docker ps --format '{{.Image}}' 2>/dev/null | grep -qiE 'nginx-proxy-manager|jc21/nginx-proxy-manager'; then
            BS_ENV_PANEL="nginxproxymanager"; return 0
        fi
    fi
    return 1
}

bs_env_check_ports() {
    BS_ENV_PORT80_FREE="no"
    BS_ENV_PORT443_FREE="no"
    bs_port_in_use 80  || BS_ENV_PORT80_FREE="yes"
    bs_port_in_use 443 || BS_ENV_PORT443_FREE="yes"
}

bs_env_check_bluestream_existing() {
    BS_ENV_BLUESTREAM_EXISTING="no"
    if [ -f "$BLUESTREAM_SERVER_CONF" ] || [ -d /usr/local/lib/bluestream ]; then
        BS_ENV_BLUESTREAM_EXISTING="yes"
    fi
}

# ---------------------------------------------------------------------------
# Run every check, then classify.
# ---------------------------------------------------------------------------
bs_env_detect() {
    bs_env_check_nginx
    bs_env_check_nginx_vhosts
    bs_env_check_apache
    bs_env_check_caddy
    bs_env_check_docker
    bs_env_check_panel
    bs_env_check_ports
    bs_env_check_bluestream_existing
    bs_env_classify
}

# Decide MODE and the nginx strategy that follows from it.
#
# CRITICAL SAFETY RULE: an existing nginx is never assumed to be
# BlueStream-owned just because no *known* panel marker fired. Ownership
# must be POSITIVELY established - here, that means nginx is present,
# active, AND has no enabled site/conf.d entry BlueStream didn't create
# (bs_env_check_nginx_vhosts). The absence of evidence (no recognized
# panel) is NOT evidence of absence (of something else managing it); when
# ownership can't be positively confirmed, isolated is always the fallback.
#
#   fresh             - no nginx, no Apache/Caddy, no panel: install/manage
#                       nginx directly (today's behavior). strategy=owned
#   plain_nginx       - nginx present, ACTIVE, no panel/Apache/Caddy, AND
#                       positively confirmed to have no other site already
#                       configured on it: safe to manage directly.
#                       strategy=owned
#   nginx_other_sites - nginx present and active, but something BlueStream
#                       did not create is already configured on it (a real,
#                       positive signal of existing use) - ownership cannot
#                       be safely assumed even with no recognized panel
#                       marker. strategy=isolated
#   nginx_inactive    - nginx present but not running: too ambiguous to
#                       assume ownership (could be mid-configuration by
#                       something else, or disabled on purpose).
#                       strategy=isolated
#   managed           - a known control panel was detected: NEVER touch its
#                       nginx/Apache config. strategy=isolated
#   alt_webserver     - Apache and/or Caddy present as the primary web
#                       server (no panel marker, but a non-nginx server is
#                       doing the real work): NEVER touch it.
#                       strategy=isolated
#   unknown           - any other ambiguous combination: isolated, never
#                       guessed as fresh.
bs_env_classify() {
    if [ "$BS_ENV_BLUESTREAM_EXISTING" = "yes" ]; then
        # A prior BlueStream install already recorded (and the operator is
        # re-running install.sh to repair/update/upgrade it) is authoritative
        # over any fresh classification: reuse whatever strategy it already
        # used, recorded alongside the rest of server.conf. Distinct MODE
        # values from a first-time "managed"/"plain_nginx" classification -
        # this is a REUSE decision, not a fresh re-detection, and the label
        # must say so plainly rather than implying a panel was just found.
        if [ "${BLUESTREAM_NGINX_STRATEGY:-}" = "isolated" ]; then
            BS_ENV_MODE="reinstall_isolated"
            BS_NGINX_STRATEGY="isolated"
        else
            BS_ENV_MODE="reinstall_owned"
            BS_NGINX_STRATEGY="owned"
        fi
        return 0
    fi
    if [ -n "$BS_ENV_PANEL" ]; then
        BS_ENV_MODE="managed"
        BS_NGINX_STRATEGY="isolated"
        return 0
    fi
    if [ "$BS_ENV_APACHE_PRESENT" = "yes" ] || [ "$BS_ENV_CADDY_PRESENT" = "yes" ]; then
        BS_ENV_MODE="alt_webserver"
        BS_NGINX_STRATEGY="isolated"
        return 0
    fi
    if [ "$BS_ENV_NGINX_PRESENT" = "no" ]; then
        BS_ENV_MODE="fresh"
        BS_NGINX_STRATEGY="owned"
        return 0
    fi
    if [ "$BS_ENV_NGINX_ACTIVE" != "yes" ]; then
        BS_ENV_MODE="nginx_inactive"
        BS_NGINX_STRATEGY="isolated"
        return 0
    fi
    if [ "$BS_ENV_NGINX_HAS_OTHER_SITES" = "yes" ]; then
        BS_ENV_MODE="nginx_other_sites"
        BS_NGINX_STRATEGY="isolated"
        return 0
    fi
    # Positively confirmed: nginx is present, active, and nothing else is
    # configured on it yet. Safe to take ownership.
    BS_ENV_MODE="plain_nginx"
    BS_NGINX_STRATEGY="owned"
    return 0
}

bs_env_mode_label() {
    case "$BS_ENV_MODE" in
        fresh)              printf 'Fresh VPS (no existing web server)' ;;
        plain_nginx)        printf 'Standard Nginx, confirmed empty (no control panel, no existing sites)' ;;
        nginx_other_sites)  printf 'Existing Nginx already serving other site(s) - ownership not assumed' ;;
        nginx_inactive)     printf 'Nginx present but not running - too ambiguous to assume ownership' ;;
        managed)            printf 'Managed environment (%s detected)' "${BS_ENV_PANEL:-unknown panel}" ;;
        alt_webserver)      printf 'Alternative web server (Apache/Caddy, no Nginx)' ;;
        reinstall_isolated) printf 'Existing BlueStream install - reusing its isolated nginx strategy' ;;
        reinstall_owned)    printf 'Existing BlueStream install - reusing its owned nginx strategy' ;;
        *)                  printf 'Unknown/custom environment' ;;
    esac
}

# Human-readable summary printed before anything is installed/modified.
bs_env_report() {
    bs_step "Detected environment"
    printf '  Classification  : %s\n' "$(bs_env_mode_label)"
    printf '  Nginx present   : %s (active: %s, other sites: %s)\n' "$BS_ENV_NGINX_PRESENT" "$BS_ENV_NGINX_ACTIVE" "$BS_ENV_NGINX_HAS_OTHER_SITES"
    printf '  Apache present  : %s\n' "$BS_ENV_APACHE_PRESENT"
    printf '  Caddy present   : %s\n' "$BS_ENV_CADDY_PRESENT"
    printf '  Control panel   : %s\n' "${BS_ENV_PANEL:-none detected}"
    printf '  Port 80 free    : %s\n' "$BS_ENV_PORT80_FREE"
    printf '  Port 443 free   : %s\n' "$BS_ENV_PORT443_FREE"
    printf '  Existing install: %s\n' "$BS_ENV_BLUESTREAM_EXISTING"
    if [ "$BS_NGINX_STRATEGY" = "isolated" ]; then
        bs_warn "An existing web server/control panel was detected (or the environment is ambiguous)."
        bs_warn "BlueStream will NOT modify it. A separate, independent nginx instance will be installed"
        bs_warn "for BlueStream's own streaming engine and web console instead - see docs/installation.md"
        bs_warn "'Existing Nginx / control panel compatibility' for the manual reverse-proxy step."
    else
        bs_info "BlueStream will manage its own nginx site configuration directly (no conflicting panel detected)."
    fi
}

# end of envdetect.sh
