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

nginx_site_template() {
    printf '%s' "$BLUESTREAM_INSTALL_ROOT/config/nginx/bluestream-site.conf.template"
}

nginx_https_template() {
    printf '%s' "$BLUESTREAM_INSTALL_ROOT/config/nginx/bluestream-https-block.conf.template"
}

# Fail-closed: verify no unresolved template placeholders remain in any
# generated BlueStream nginx file. Reports the affected file and returns 1.
nginx_assert_no_placeholders() {
    local f bad
    for f in "$NGINX_SITE_FILE" "$NGINX_SNIPPET_DIR/hls-location.conf" "$NGINX_SNIPPET_DIR/player-location.conf"; do
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

    # Fail closed: no unresolved placeholders may remain in any generated file.
    if ! nginx_assert_no_placeholders; then
        bs_error "Nginx configuration contains unresolved placeholders; refusing to activate."
        return 1
    fi

    bs_ok "Nginx site configuration written: $dest"
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
