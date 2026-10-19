#!/usr/bin/env bash
# BlueStream Relay Pro - nginx configuration management.
#
# Generates the site config from a template, enables the site, validates
# with `nginx -t` before any reload/restart.
#
# BlueStream Relay Pro 0.1.0 (foundation). See LICENSE for terms.

if [ -n "${BLUESTREAM_NGINX_LOADED:-}" ]; then
    return 0 2>/dev/null || exit 0
fi
BLUESTREAM_NGINX_LOADED=1

NGINX_SITE_FILE="/etc/nginx/sites-available/bluestream"
NGINX_SITE_LINK="/etc/nginx/sites-enabled/bluestream"
NGINX_SNIPPET_DIR="/etc/nginx/bluestream"
# Main-context include that pulls the rtmp{} block AFTER ngx_rtmp_module is
# loaded (50-mod-rtmp.conf sorts before this file in modules-enabled).
NGINX_RTMP_INCLUDE="/etc/nginx/modules-enabled/60-bluestream-rtmp.conf"

nginx_site_template() {
    printf '%s' "$BLUESTREAM_INSTALL_ROOT/config/nginx/bluestream-site.conf.template"
}

nginx_https_template() {
    printf '%s' "$BLUESTREAM_INSTALL_ROOT/config/nginx/bluestream-https-block.conf.template"
}

nginx_rtmp_template() {
    printf '%s' "$BLUESTREAM_INSTALL_ROOT/config/nginx/rtmp.conf.template"
}

# Fail-closed: verify no unresolved template placeholders remain in any
# generated BlueStream nginx file. Reports the affected file and returns 1.
nginx_assert_no_placeholders() {
    local f bad
    for f in \
        "$NGINX_SITE_FILE" \
        "$NGINX_SNIPPET_DIR/hls-location.conf" \
        "$NGINX_SNIPPET_DIR/player-location.conf" \
        "$NGINX_SNIPPET_DIR/console-location.conf" \
        "$NGINX_SNIPPET_DIR/console-http.conf" \
        "$NGINX_SNIPPET_DIR/rtmp.conf"; do
        [ -f "$f" ] || { bs_error "Generated nginx file missing: $f"; return 1; }
        bad="$(grep -oE '__[A-Z_][A-Z0-9_]*__' "$f" 2>/dev/null | sort -u | tr '\n' ' ')"
        if [ -n "$bad" ]; then
            bs_error "Unresolved placeholder(s) in $f: $bad"
            return 1
        fi
    done
    return 0
}

# Phase 1C: nginx-rtmp-module keeps each live application's publisher/player
# state PER WORKER PROCESS (there is no cross-worker shared memory for live
# stream routing). With the package default (`worker_processes auto;` - one
# worker per CPU core) an outbound publisher's PLAY connection to the private
# RTMP bus is accepted by whichever worker next calls accept() - often a
# DIFFERENT worker than the one holding the source relay/playlist's PUBLISH
# connection - and the two are never linked: the publisher process connects
# successfully but never receives any data and hangs until its own read
# timeout. Pinning nginx to a single worker is the standard, documented fix
# for this nginx-rtmp-module constraint. This only affects nginx's own HTTP
# serving concurrency (BlueStream's HLS/console traffic is lightweight; the
# real bottleneck is always FFmpeg CPU/bandwidth, never nginx) - if this host
# runs other, independent, high-traffic nginx sites, review this trade-off
# before installing BlueStream alongside them.
nginx_ensure_single_worker() {
    local conf="/etc/nginx/nginx.conf"
    [ -f "$conf" ] || return 0
    # Defense-in-depth (see lib/envdetect.sh / the legacy-ownership fix): this
    # function edits the SYSTEM /etc/nginx/nginx.conf, which is only ever
    # correct when BlueStream genuinely owns nginx on this host. It must
    # never run against a panel-managed or otherwise non-owned nginx, even if
    # some future bug calls it directly - re-verify right here, independent
    # of whatever the caller already decided, rather than trusting it blindly.
    if [ "${BLUESTREAM_NGINX_STRATEGY:-owned}" != "owned" ]; then
        bs_die "Refusing to edit $conf: current nginx strategy is '${BLUESTREAM_NGINX_STRATEGY:-unknown}', not 'owned'. This is a safety guard against ever editing a panel-managed/non-owned system nginx.conf - see lib/envdetect.sh."
    fi
    if type bs_env_check_panel >/dev/null 2>&1; then
        local _recheck_panel="$BS_ENV_PANEL"
        bs_env_check_panel
        if [ -n "$BS_ENV_PANEL" ]; then
            bs_die "Refusing to edit $conf: a control panel ('$BS_ENV_PANEL') is present on this host right now. BlueStream must never edit a panel-managed nginx.conf, even in a strategy that was previously recorded as 'owned'. Run 'sudo bluestream-manager migrate status' / see docs/installation.md 'Safe Owned -> Isolated migration'."
        fi
        BS_ENV_PANEL="$_recheck_panel"
    fi
    if grep -qE '^[[:space:]]*worker_processes[[:space:]]' "$conf"; then
        if grep -qE '^[[:space:]]*worker_processes[[:space:]]+1;[[:space:]]*$' "$conf"; then
            return 0
        fi
        sed -i -E 's/^[[:space:]]*worker_processes[[:space:]]+[^;]+;/worker_processes 1;/' "$conf"
    else
        sed -i '1i worker_processes 1;' "$conf"
    fi
    bs_info "nginx worker_processes pinned to 1 (required for the private RTMP bus: nginx-rtmp keeps live-stream state per worker, so a publisher's play connection and the source's publish connection must land on the same worker)."
    return 0
}

nginx_gen_config() {
    bs_require_root
    bs_require_cmd nginx
    nginx_ensure_single_worker
    local template dest domain https_block
    template="$(nginx_site_template)"
    dest="$NGINX_SITE_FILE"
    [ -f "$template" ] || bs_die "Nginx template not found: $template"

    domain="${BLUESTREAM_DOMAIN:-_}"
    https_block=""
    if [ "$BLUESTREAM_SSL_ENABLED" = "yes" ] && [ -n "$BLUESTREAM_DOMAIN" ] && \
        [ -f "/etc/letsencrypt/live/$BLUESTREAM_DOMAIN/fullchain.pem" ]; then
        local htpl
        htpl="$(nginx_https_template)"
        if [ -f "$htpl" ]; then
            https_block="$(sed -e "s|__DOMAIN__|$domain|g" \
                -e "s|__HTTPS_PORT__|443|g" \
                -e "s|__NGINX_SNIPPET_DIR__|$NGINX_SNIPPET_DIR|g" "$htpl")"
        fi
    fi

    sed -e "s|__DOMAIN__|$domain|g" \
        -e "s|__HLS_ROOT__|$BLUESTREAM_HLS_ROOT|g" \
        -e "s|__WEB_ROOT__|$BLUESTREAM_WEB_DIR|g" \
        -e "s|__NGINX_SNIPPET_DIR__|$NGINX_SNIPPET_DIR|g" \
        -e "s|__HTTP_PORT__|80|g" \
        "$template" | grep -v '^__HTTPS_BLOCK__$' > "$dest.tmp"
    if [ -n "$https_block" ]; then
        printf '\n%s\n' "$https_block" >> "$dest.tmp"
    fi
    mv -f "$dest.tmp" "$dest"
    chmod 0644 "$dest"

    # Render shared location snippets (they contain placeholders and must NOT
    # be copied verbatim).
    mkdir -p "$NGINX_SNIPPET_DIR" 2>/dev/null
    sed -e "s|__HLS_ROOT__|$BLUESTREAM_HLS_ROOT|g" \
        "$BLUESTREAM_INSTALL_ROOT/config/nginx/hls-location.conf" > "$NGINX_SNIPPET_DIR/hls-location.conf.tmp"
    mv -f "$NGINX_SNIPPET_DIR/hls-location.conf.tmp" "$NGINX_SNIPPET_DIR/hls-location.conf"
    sed -e "s|__WEB_ROOT__|$BLUESTREAM_WEB_DIR|g" \
        "$BLUESTREAM_INSTALL_ROOT/config/nginx/player-location.conf" > "$NGINX_SNIPPET_DIR/player-location.conf.tmp"
    mv -f "$NGINX_SNIPPET_DIR/player-location.conf.tmp" "$NGINX_SNIPPET_DIR/player-location.conf"
    chmod 0644 "$NGINX_SNIPPET_DIR/hls-location.conf" "$NGINX_SNIPPET_DIR/player-location.conf"

    # Web console reverse-proxy snippet (fixed loopback upstream). The upload
    # body cap is rendered from the SINGLE authoritative MAX_MEDIA_UPLOAD_MB
    # value (validated digits-only, so it can never inject nginx directives).
    # Step 8: the proxy target port is rendered from the SINGLE authoritative
    # BLUESTREAM_WEB_PORT value (validated digits-only in range), matching
    # exactly the port Gunicorn is told to bind in bluestream-web.service.
    if ! bs_valid_upload_mb "$BLUESTREAM_MAX_MEDIA_UPLOAD_MB"; then
        bs_error "Invalid MAX_MEDIA_UPLOAD_MB in server.conf: $BLUESTREAM_MAX_MEDIA_UPLOAD_MB"
        return 1
    fi
    if ! bs_valid_port "${BLUESTREAM_WEB_PORT:-}"; then
        bs_error "Invalid or unset WEB_PORT in server.conf: ${BLUESTREAM_WEB_PORT:-<empty>}"
        return 1
    fi
    sed -e "s|__CLIENT_MAX_BODY_SIZE__|client_max_body_size ${BLUESTREAM_MAX_MEDIA_UPLOAD_MB}m;|g" \
        -e "s|__WEB_PORT__|${BLUESTREAM_WEB_PORT}|g" \
        "$BLUESTREAM_INSTALL_ROOT/config/nginx/console-location.conf" > "$NGINX_SNIPPET_DIR/console-location.conf.tmp"
    mv -f "$NGINX_SNIPPET_DIR/console-location.conf.tmp" "$NGINX_SNIPPET_DIR/console-location.conf"
    chmod 0644 "$NGINX_SNIPPET_DIR/console-location.conf"

    # HTTP-block console snippet: proxy when HTTP-only, HTTPS redirect when SSL
    # is enabled (Secure cookies must never be issued over plain HTTP).
    if [ "$BLUESTREAM_SSL_ENABLED" = "yes" ]; then
        sed -e "s|__HTTPS_PORT__|443|g" \
            "$BLUESTREAM_INSTALL_ROOT/config/nginx/console-redirect-http.conf" > "$NGINX_SNIPPET_DIR/console-http.conf.tmp"
    else
        sed -e "s|__CLIENT_MAX_BODY_SIZE__|client_max_body_size ${BLUESTREAM_MAX_MEDIA_UPLOAD_MB}m;|g" \
            -e "s|__WEB_PORT__|${BLUESTREAM_WEB_PORT}|g" \
            "$BLUESTREAM_INSTALL_ROOT/config/nginx/console-location.conf" > "$NGINX_SNIPPET_DIR/console-http.conf.tmp"
    fi
    mv -f "$NGINX_SNIPPET_DIR/console-http.conf.tmp" "$NGINX_SNIPPET_DIR/console-http.conf"
    chmod 0644 "$NGINX_SNIPPET_DIR/console-http.conf"

    # Render the private local RTMP ingest (nginx MAIN context).
    sed -e "s|__HLS_ROOT__|$BLUESTREAM_HLS_ROOT|g" \
        -e "s|__RTMP_BIND__|$BLUESTREAM_RTMP_BIND|g" \
        -e "s|__RTMP_PORT__|$BLUESTREAM_RTMP_PORT|g" \
        "$(nginx_rtmp_template)" > "$NGINX_SNIPPET_DIR/rtmp.conf.tmp"
    mv -f "$NGINX_SNIPPET_DIR/rtmp.conf.tmp" "$NGINX_SNIPPET_DIR/rtmp.conf"
    chmod 0644 "$NGINX_SNIPPET_DIR/rtmp.conf"

    # Main-context include so the rtmp{} block is parsed AFTER ngx_rtmp_module
    # has been loaded (50-mod-rtmp.conf sorts before 60-bluestream-rtmp.conf).
    {
        printf '# BlueStream Relay Pro - nginx RTMP main-context include.\n'
        printf '# Sorts after 50-mod-rtmp.conf so ngx_rtmp_module is loaded first.\n'
        printf 'include %s/rtmp.conf;\n' "$NGINX_SNIPPET_DIR"
    } > "$NGINX_RTMP_INCLUDE"
    chmod 0644 "$NGINX_RTMP_INCLUDE"

    # Fail closed: no unresolved placeholders may remain in any generated file.
    if ! nginx_assert_no_placeholders; then
        bs_error "Nginx configuration contains unresolved placeholders; refusing to activate."
        return 1
    fi

    # Keep the web console's non-secret deployment setting in sync with the
    # current SSL state (install AND `bluestream-manager ssl issue`, both of
    # which run nginx_gen_config). The web process never reads server.conf.
    nginx_sync_web_conf

    bs_ok "Nginx site configuration written: $dest"
}

# Write /var/lib/bluestream/web/web.conf (secure_cookie=yes|no and the
# max_media_upload_mb deployment setting) from the current root config. Root-only
# write; the web user gets read-only access. The web process never reads
# /etc/bluestream/server.conf directly.
nginx_sync_web_conf() {
    local web_state="/var/lib/bluestream/web" sc="no"
    id bluestream-web >/dev/null 2>&1 || return 0  # web console not installed
    if [ "$BLUESTREAM_SSL_ENABLED" = "yes" ]; then
        sc="yes"
    fi
    mkdir -p "$web_state"
    {
        printf '# BlueStream Relay Pro web console deployment settings (non-secret).\n'
        printf '# Regenerated from the current BlueStream SSL state and server config.\n'
        printf 'secure_cookie=%s\n' "$sc"
        printf 'max_media_upload_mb=%s\n' "$BLUESTREAM_MAX_MEDIA_UPLOAD_MB"
    } > "$web_state/web.conf.tmp"
    chown root:bluestream-web "$web_state/web.conf.tmp"
    chmod 0640 "$web_state/web.conf.tmp"
    mv -f "$web_state/web.conf.tmp" "$web_state/web.conf"
    chown root:bluestream-web "$web_state/web.conf"
    chmod 0640 "$web_state/web.conf"
    return 0
}

nginx_site_enable() {
    bs_require_root
    [ -f "$NGINX_SITE_FILE" ] || bs_die "Site file missing: $NGINX_SITE_FILE"
    mkdir -p /etc/nginx/sites-enabled 2>/dev/null || true
    ln -sf "$NGINX_SITE_FILE" "$NGINX_SITE_LINK"
    # The stock Ubuntu nginx package ships sites-enabled/default (server_name
    # _; a catch-all vhost). Left enabled, it silently answers any request
    # whose Host header doesn't exactly match the configured BlueStream
    # domain (a bare IP, a typo, DNS not yet propagated) with nginx's default
    # page/404 instead of BlueStream's site. Only the ENABLED symlink is
    # removed - sites-available/default is left completely untouched, so this
    # is trivially reversible (re-create the symlink) and never deletes data.
    if [ -L /etc/nginx/sites-enabled/default ]; then
        rm -f /etc/nginx/sites-enabled/default
        bs_info "Disabled the stock nginx default site (sites-available/default is preserved, untouched)."
    fi
}

# Strategy-aware from here down: every caller (bluestream-manager, lib/
# diagnostics.sh, lib/selftest.sh) goes through these instead of hard-coding
# "nginx.service", so isolated-mode installs are checked/managed correctly
# everywhere without needing to touch each call site individually.
nginx_test() {
    bs_require_cmd nginx
    if [ "${BLUESTREAM_NGINX_STRATEGY:-}" = "isolated" ]; then
        nginx_isolated_test
    else
        nginx -t
    fi
}

nginx_reload() {
    bs_require_root
    nginx_test || bs_die "Nginx configuration is invalid; not reloading"
    if [ "${BLUESTREAM_NGINX_STRATEGY:-}" = "isolated" ]; then
        systemctl reload "$BLUESTREAM_ISOLATED_UNIT" 2>/dev/null || systemctl restart "$BLUESTREAM_ISOLATED_UNIT"
        bs_ok "Isolated nginx reloaded"
    else
        systemctl reload nginx
        bs_ok "Nginx reloaded"
    fi
}

nginx_restart() {
    bs_require_root
    nginx_test || bs_die "Nginx configuration is invalid; not restarting"
    if [ "${BLUESTREAM_NGINX_STRATEGY:-}" = "isolated" ]; then
        systemctl restart "$BLUESTREAM_ISOLATED_UNIT"
        bs_ok "Isolated nginx restarted"
    else
        systemctl restart nginx
        bs_ok "Nginx restarted"
    fi
}

nginx_status() {
    if [ "${BLUESTREAM_NGINX_STRATEGY:-}" = "isolated" ]; then
        systemctl is-active "$BLUESTREAM_ISOLATED_UNIT" 2>/dev/null
    else
        systemctl is-active nginx 2>/dev/null
    fi
}

nginx_running() {
    [ "$(nginx_status)" = "active" ]
}

# ---------------------------------------------------------------------------
# ISOLATED nginx instance (universal install compat).
#
# Used instead of everything above when lib/envdetect.sh classified this
# host as already running a control panel, Apache, or Caddy (BS_NGINX_
# STRATEGY=isolated). BlueStream never writes to /etc/nginx, never touches
# the system nginx.service/package/sites, and never changes a global nginx
# setting shared with anything else - it installs and runs its OWN separate
# nginx process tree instead, entirely self-contained under
# /etc/bluestream/nginx, with its own systemd unit, pidfile and ports.
#
# The RTMP bus content and the HLS/player/console site content are IDENTICAL
# to the owned-mode ones (same templates, same location snippets); only the
# destination paths and the HTTP(S) ports differ, since 80/443 are assumed
# to already belong to the existing web server/panel. Deliberately
# duplicates a modest amount of rendering logic from nginx_gen_config rather
# than sharing it, so the thoroughly-tested owned-mode path is never put at
# risk by a refactor made for this one.
# ---------------------------------------------------------------------------
BLUESTREAM_ISOLATED_ROOT="/etc/bluestream/nginx"
BLUESTREAM_ISOLATED_SNIPPET_DIR="$BLUESTREAM_ISOLATED_ROOT/snippets"
BLUESTREAM_ISOLATED_CONF_FILE="$BLUESTREAM_ISOLATED_ROOT/nginx.conf"
BLUESTREAM_ISOLATED_SITE_FILE="$BLUESTREAM_ISOLATED_ROOT/site.conf"
BLUESTREAM_ISOLATED_PIDFILE="/run/bluestream-nginx.pid"
BLUESTREAM_ISOLATED_LOG_DIR="/var/log/bluestream-nginx"
BLUESTREAM_ISOLATED_UNIT="bluestream-nginx.service"
# The isolated gateway is an internal loopback upstream for the hosting
# panel.  Never auto-claim the public web ports: a temporarily-stopped panel
# still owns 80/443, and taking either port during that window would make the
# next panel start fail.  TLS termination belongs to the panel; the isolated
# instance intentionally has no HTTPS listener.
BLUESTREAM_ISOLATED_HTTP_CANDIDATES="8088 8188 8288 8388 18080"

nginx_isolated_select_ports() {
    local p
    if [ -n "${BLUESTREAM_ISOLATED_HTTP_PORT:-}" ] && bs_valid_port "$BLUESTREAM_ISOLATED_HTTP_PORT"; then
        case "$BLUESTREAM_ISOLATED_HTTP_PORT" in
            80|443)
                bs_error "Refusing persisted isolated gateway port $BLUESTREAM_ISOLATED_HTTP_PORT: ports 80 and 443 are reserved for the hosting panel. Choose a safe loopback port explicitly; BlueStream will not silently change a working proxy destination."
                return 1
                ;;
        esac
        # A running isolated service legitimately owns its persisted port.
        # Otherwise a collision means fail rather than silently changing the
        # reverse-proxy destination operators already configured.
        if ! nginx_isolated_running && bs_port_in_use "$BLUESTREAM_ISOLATED_HTTP_PORT"; then
            bs_error "Persisted isolated gateway port $BLUESTREAM_ISOLATED_HTTP_PORT is already in use. The configured proxy destination was not changed."
            return 1
        fi
    else
        BLUESTREAM_ISOLATED_HTTP_PORT=""
        for p in $BLUESTREAM_ISOLATED_HTTP_CANDIDATES; do
            if ! bs_port_in_use "$p"; then BLUESTREAM_ISOLATED_HTTP_PORT="$p"; break; fi
        done
        [ -n "$BLUESTREAM_ISOLATED_HTTP_PORT" ] || return 1
    fi
    return 0
}

# Locate the RTMP module .so shipped by the Ubuntu/Debian libnginx-mod-rtmp
# package. Tries the standard install path first, then falls back to
# whatever the package's own "modules-available" snippet actually points at
# (defensive against a future package layout change).
nginx_isolated_rtmp_module_path() {
    local f rel
    for f in /usr/lib/nginx/modules/ngx_rtmp_module.so \
             /usr/lib64/nginx/modules/ngx_rtmp_module.so; do
        [ -f "$f" ] && { printf '%s' "$f"; return 0; }
    done
    if [ -f /usr/share/nginx/modules-available/mod-rtmp.conf ]; then
        rel="$(grep -oE 'modules/[^ ;]+\.so' /usr/share/nginx/modules-available/mod-rtmp.conf 2>/dev/null | head -n1)"
        if [ -n "$rel" ] && [ -f "/usr/lib/nginx/$rel" ]; then
            printf '/usr/lib/nginx/%s' "$rel"
            return 0
        fi
    fi
    return 1
}

nginx_isolated_gen_config() {
    bs_require_root
    if ! command -v nginx >/dev/null 2>&1; then
        bs_error "Required command not found: nginx"
        return 1
    fi
    if ! nginx_isolated_select_ports; then
        bs_error "Could not select a safe isolated gateway port (tried: $BLUESTREAM_ISOLATED_HTTP_CANDIDATES)."
        return 1
    fi

    local modpath domain
    if ! modpath="$(nginx_isolated_rtmp_module_path)"; then
        bs_error "nginx RTMP module (libnginx-mod-rtmp) not found; cannot configure the isolated streaming engine."
        return 1
    fi
    domain="${BLUESTREAM_DOMAIN:-_}"

    if ! bs_valid_upload_mb "$BLUESTREAM_MAX_MEDIA_UPLOAD_MB"; then
        bs_error "Invalid MAX_MEDIA_UPLOAD_MB in server.conf: $BLUESTREAM_MAX_MEDIA_UPLOAD_MB"
        return 1
    fi
    if ! bs_valid_port "${BLUESTREAM_WEB_PORT:-}"; then
        bs_error "Invalid or unset WEB_PORT in server.conf: ${BLUESTREAM_WEB_PORT:-<empty>}"
        return 1
    fi

    mkdir -p "$BLUESTREAM_ISOLATED_ROOT" "$BLUESTREAM_ISOLATED_SNIPPET_DIR" "$BLUESTREAM_ISOLATED_LOG_DIR" 2>/dev/null
    chmod 0755 "$BLUESTREAM_ISOLATED_ROOT" "$BLUESTREAM_ISOLATED_SNIPPET_DIR" "$BLUESTREAM_ISOLATED_LOG_DIR"
    chown "$BLUESTREAM_NGINX_USER:$BLUESTREAM_NGINX_USER" "$BLUESTREAM_ISOLATED_LOG_DIR" 2>/dev/null || true

    # Location snippets - identical content to the owned-mode ones.
    sed -e "s|__HLS_ROOT__|$BLUESTREAM_HLS_ROOT|g" \
        "$BLUESTREAM_INSTALL_ROOT/config/nginx/hls-location.conf" > "$BLUESTREAM_ISOLATED_SNIPPET_DIR/hls-location.conf.tmp"
    mv -f "$BLUESTREAM_ISOLATED_SNIPPET_DIR/hls-location.conf.tmp" "$BLUESTREAM_ISOLATED_SNIPPET_DIR/hls-location.conf"
    sed -e "s|__WEB_ROOT__|$BLUESTREAM_WEB_DIR|g" \
        "$BLUESTREAM_INSTALL_ROOT/config/nginx/player-location.conf" > "$BLUESTREAM_ISOLATED_SNIPPET_DIR/player-location.conf.tmp"
    mv -f "$BLUESTREAM_ISOLATED_SNIPPET_DIR/player-location.conf.tmp" "$BLUESTREAM_ISOLATED_SNIPPET_DIR/player-location.conf"
    sed -e "s|__CLIENT_MAX_BODY_SIZE__|client_max_body_size ${BLUESTREAM_MAX_MEDIA_UPLOAD_MB}m;|g" \
        -e "s|__WEB_PORT__|${BLUESTREAM_WEB_PORT}|g" \
        "$BLUESTREAM_INSTALL_ROOT/config/nginx/console-location.conf" > "$BLUESTREAM_ISOLATED_SNIPPET_DIR/console-location.conf.tmp"
    mv -f "$BLUESTREAM_ISOLATED_SNIPPET_DIR/console-location.conf.tmp" "$BLUESTREAM_ISOLATED_SNIPPET_DIR/console-location.conf"
    # The isolated instance never auto-issues its own SSL (see ssl_issue /
    # docs/installation.md); console-http.conf is always the plain proxy.
    cp -f "$BLUESTREAM_ISOLATED_SNIPPET_DIR/console-location.conf" "$BLUESTREAM_ISOLATED_SNIPPET_DIR/console-http.conf"
    chmod 0644 "$BLUESTREAM_ISOLATED_SNIPPET_DIR"/*.conf

    # Site file (same template as owned mode, isolated ports/paths).
    sed -e "s|__DOMAIN__|$domain|g" \
        -e "s|__HLS_ROOT__|$BLUESTREAM_HLS_ROOT|g" \
        -e "s|__WEB_ROOT__|$BLUESTREAM_WEB_DIR|g" \
        -e "s|__NGINX_SNIPPET_DIR__|$BLUESTREAM_ISOLATED_SNIPPET_DIR|g" \
        -e "s|__HTTP_PORT__|$BLUESTREAM_ISOLATED_HTTP_PORT|g" \
        "$(nginx_site_template)" | grep -v '^__HTTPS_BLOCK__$' \
        | sed -e "s|^[[:space:]]*listen ${BLUESTREAM_ISOLATED_HTTP_PORT};|    listen 127.0.0.1:${BLUESTREAM_ISOLATED_HTTP_PORT};|" \
              -e "/^[[:space:]]*listen \[::\]:${BLUESTREAM_ISOLATED_HTTP_PORT};/d" \
        > "$BLUESTREAM_ISOLATED_SITE_FILE.tmp"
    mv -f "$BLUESTREAM_ISOLATED_SITE_FILE.tmp" "$BLUESTREAM_ISOLATED_SITE_FILE"
    chmod 0644 "$BLUESTREAM_ISOLATED_SITE_FILE"

    # RTMP bus: identical content/ports to owned mode - always loopback-only
    # and never conflicts with anything, regardless of strategy.
    sed -e "s|__HLS_ROOT__|$BLUESTREAM_HLS_ROOT|g" \
        -e "s|__RTMP_BIND__|$BLUESTREAM_RTMP_BIND|g" \
        -e "s|__RTMP_PORT__|$BLUESTREAM_RTMP_PORT|g" \
        "$(nginx_rtmp_template)" > "$BLUESTREAM_ISOLATED_ROOT/rtmp.conf.tmp"
    mv -f "$BLUESTREAM_ISOLATED_ROOT/rtmp.conf.tmp" "$BLUESTREAM_ISOLATED_ROOT/rtmp.conf"
    chmod 0644 "$BLUESTREAM_ISOLATED_ROOT/rtmp.conf"

    # Self-contained main nginx.conf - fully independent of the system
    # nginx.conf / Debian modules-enabled convention (we own ordering
    # directly), never installed under /etc/nginx.
    {
        printf '# BlueStream Relay Pro - ISOLATED nginx instance (generated).\n'
        printf '# Independent of the system nginx; never installed to /etc/nginx and\n'
        printf '# never read by it. Safe to run alongside any other web server/panel.\n'
        printf 'user %s;\n' "$BLUESTREAM_NGINX_USER"
        printf 'pid %s;\n' "$BLUESTREAM_ISOLATED_PIDFILE"
        printf 'error_log %s/error.log warn;\n' "$BLUESTREAM_ISOLATED_LOG_DIR"
        printf 'worker_processes 1;\n'
        printf 'load_module %s;\n' "$modpath"
        printf '\n'
        printf 'events {\n    worker_connections 1024;\n}\n'
        printf '\n'
        printf 'include %s/rtmp.conf;\n' "$BLUESTREAM_ISOLATED_ROOT"
        printf '\n'
        printf 'http {\n'
        printf '    include /etc/nginx/mime.types;\n'
        printf '    default_type application/octet-stream;\n'
        printf '    access_log %s/access.log;\n' "$BLUESTREAM_ISOLATED_LOG_DIR"
        printf '    sendfile on;\n'
        printf '    add_header X-Content-Type-Options nosniff always;\n'
        printf '    include %s;\n' "$BLUESTREAM_ISOLATED_SITE_FILE"
        printf '}\n'
    } > "$BLUESTREAM_ISOLATED_CONF_FILE.tmp"
    mv -f "$BLUESTREAM_ISOLATED_CONF_FILE.tmp" "$BLUESTREAM_ISOLATED_CONF_FILE"
    chmod 0644 "$BLUESTREAM_ISOLATED_CONF_FILE"

    # Fail closed: no unresolved placeholders anywhere in the isolated tree.
    local bad
    bad="$(grep -orE '__[A-Z_][A-Z0-9_]*__' "$BLUESTREAM_ISOLATED_ROOT" 2>/dev/null | cut -d: -f2- | sort -u | tr '\n' ' ')"
    if [ -n "$bad" ]; then
        bs_error "Unresolved placeholder(s) in the isolated nginx config: $bad"
        return 1
    fi

    nginx_sync_web_conf
    bs_ok "Isolated nginx configuration written: $BLUESTREAM_ISOLATED_CONF_FILE (loopback HTTP 127.0.0.1:$BLUESTREAM_ISOLATED_HTTP_PORT; public HTTPS is provided by the hosting panel)"
    return 0
}

nginx_isolated_test() {
    bs_require_cmd nginx
    nginx -t -c "$BLUESTREAM_ISOLATED_CONF_FILE"
}

nginx_isolated_install_unit() {
    bs_require_root
    if [ ! -f "$BLUESTREAM_INSTALL_ROOT/config/systemd/bluestream-nginx.service" ]; then
        bs_error "Isolated nginx unit template missing."
        return 1
    fi
    install -o root -g root -m 0644 \
        "$BLUESTREAM_INSTALL_ROOT/config/systemd/bluestream-nginx.service" \
        "/etc/systemd/system/$BLUESTREAM_ISOLATED_UNIT" || return 1
    if ! systemctl daemon-reload; then
        bs_error "systemd daemon-reload failed while installing $BLUESTREAM_ISOLATED_UNIT"
        return 1
    fi
    return 0
}

# Recheck both listeners at the last safe moment before starting a stopped
# isolated instance.  This closes the race between initial selection/config
# generation and cutover. A start failure still remains observable and is
# handled by the migration transaction's rollback path.
nginx_isolated_validate_start_ports() {
    local attempts=0 max_attempts="${BLUESTREAM_PORT_RELEASE_ATTEMPTS:-25}"
    nginx_isolated_running && return 0
    if bs_port_in_use "$BLUESTREAM_ISOLATED_HTTP_PORT"; then
        bs_error "Cannot start isolated nginx: loopback gateway port $BLUESTREAM_ISOLATED_HTTP_PORT is now in use."
        return 1
    fi
    # A graceful system-nginx reload can leave its old worker holding the
    # just-removed RTMP listener for a few seconds. Give that verified legacy
    # listener a bounded drain window; a persistent or newly-arrived owner is
    # a real collision and must fail the transaction.
    while [ "$attempts" -lt "$max_attempts" ] && bs_port_in_use "$BLUESTREAM_RTMP_PORT"; do
        sleep 0.2
        attempts=$((attempts + 1))
    done
    if bs_port_in_use "$BLUESTREAM_RTMP_PORT"; then
        bs_error "Cannot start isolated nginx: RTMP port $BLUESTREAM_RTMP_BIND:$BLUESTREAM_RTMP_PORT is still in use."
        return 1
    fi
    return 0
}

nginx_isolated_enable() {
    bs_require_root
    if ! nginx_isolated_test; then
        bs_error "Isolated nginx configuration failed validation"
        return 1
    fi
    nginx_isolated_validate_start_ports || return 1
    if ! systemctl enable "$BLUESTREAM_ISOLATED_UNIT"; then
        bs_error "Failed to enable $BLUESTREAM_ISOLATED_UNIT"
        return 1
    fi
    if systemctl is-active "$BLUESTREAM_ISOLATED_UNIT" >/dev/null 2>&1; then
        if ! systemctl restart "$BLUESTREAM_ISOLATED_UNIT"; then
            bs_error "Failed to restart $BLUESTREAM_ISOLATED_UNIT"
            return 1
        fi
    else
        if ! systemctl start "$BLUESTREAM_ISOLATED_UNIT"; then
            bs_error "Failed to start $BLUESTREAM_ISOLATED_UNIT"
            return 1
        fi
    fi
    if [ "$(systemctl is-active "$BLUESTREAM_ISOLATED_UNIT" 2>/dev/null)" != "active" ]; then
        bs_error "$BLUESTREAM_ISOLATED_UNIT did not reach active state"
        return 1
    fi
    bs_ok "$BLUESTREAM_ISOLATED_UNIT started (BlueStream's own independent nginx instance)"
    return 0
}

nginx_isolated_reload() {
    bs_require_root
    nginx_isolated_test || bs_die "Isolated nginx configuration is invalid; not reloading"
    systemctl reload "$BLUESTREAM_ISOLATED_UNIT" 2>/dev/null || systemctl restart "$BLUESTREAM_ISOLATED_UNIT"
    bs_ok "Isolated nginx reloaded"
}

nginx_isolated_running() {
    [ "$(systemctl is-active "$BLUESTREAM_ISOLATED_UNIT" 2>/dev/null)" = "active" ]
}
