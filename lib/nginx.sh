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

nginx_gen_config() {
    bs_require_root
    bs_require_cmd nginx
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
            https_block="$(sed -e "s|__DOMAIN__|$domain|g" "$htpl")"
        fi
    fi

    sed -e "s|__DOMAIN__|$domain|g" \
        -e "s|__HLS_ROOT__|$BLUESTREAM_HLS_ROOT|g" \
        -e "s|__WEB_ROOT__|$BLUESTREAM_WEB_DIR|g" \
        -e "s|__NGINX_SNIPPET_DIR__|$NGINX_SNIPPET_DIR|g" \
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
    if ! bs_valid_upload_mb "$BLUESTREAM_MAX_MEDIA_UPLOAD_MB"; then
        bs_error "Invalid MAX_MEDIA_UPLOAD_MB in server.conf: $BLUESTREAM_MAX_MEDIA_UPLOAD_MB"
        return 1
    fi
    sed -e "s|__CLIENT_MAX_BODY_SIZE__|client_max_body_size ${BLUESTREAM_MAX_MEDIA_UPLOAD_MB}m;|g" \
        "$BLUESTREAM_INSTALL_ROOT/config/nginx/console-location.conf" > "$NGINX_SNIPPET_DIR/console-location.conf.tmp"
    mv -f "$NGINX_SNIPPET_DIR/console-location.conf.tmp" "$NGINX_SNIPPET_DIR/console-location.conf"
    chmod 0644 "$NGINX_SNIPPET_DIR/console-location.conf"

    # HTTP-block console snippet: proxy when HTTP-only, HTTPS redirect when SSL
    # is enabled (Secure cookies must never be issued over plain HTTP).
    if [ "$BLUESTREAM_SSL_ENABLED" = "yes" ]; then
        cp -f "$BLUESTREAM_INSTALL_ROOT/config/nginx/console-redirect-http.conf" "$NGINX_SNIPPET_DIR/console-http.conf.tmp"
    else
        sed -e "s|__CLIENT_MAX_BODY_SIZE__|client_max_body_size ${BLUESTREAM_MAX_MEDIA_UPLOAD_MB}m;|g" \
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
}

nginx_test() {
    bs_require_cmd nginx
    nginx -t
}

nginx_reload() {
    bs_require_root
    nginx_test || bs_die "Nginx configuration is invalid; not reloading"
    systemctl reload nginx
    bs_ok "Nginx reloaded"
}

nginx_restart() {
    bs_require_root
    nginx_test || bs_die "Nginx configuration is invalid; not restarting"
    systemctl restart nginx
    bs_ok "Nginx restarted"
}

nginx_status() {
    systemctl is-active nginx 2>/dev/null
}

nginx_running() {
    [ "$(systemctl is-active nginx 2>/dev/null)" = "active" ]
}
