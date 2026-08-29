#!/usr/bin/env bash
# BlueStream Relay Pro - SSL (Let's Encrypt / Certbot) management.
#
# BlueStream Relay Pro 0.1.0 (foundation). See LICENSE for terms.

if [ -n "${BLUESTREAM_SSL_LOADED:-}" ]; then
    return 0 2>/dev/null || exit 0
fi
BLUESTREAM_SSL_LOADED=1

ssl_issue() {
    bs_require_root
    [ -n "$BLUESTREAM_DOMAIN" ] || bs_die "No domain configured (see Project Information / server.conf)."
    [ -n "$BLUESTREAM_ADMIN_EMAIL" ] || bs_die "No administrator email configured."
    bs_require_cmd certbot || bs_die "certbot is not installed"
    bs_require_cmd nginx

    # Ensure the HTTP site is live so the ACME HTTP-01 challenge can run.
    nginx_gen_config
    nginx_site_enable
    nginx_test || bs_die "Nginx configuration is invalid; fix before requesting SSL"
    systemctl start nginx 2>/dev/null || true
    systemctl reload nginx 2>/dev/null || true

    bs_step "Requesting Let's Encrypt certificate for $BLUESTREAM_DOMAIN"
    certbot --nginx -d "$BLUESTREAM_DOMAIN" \
        --non-interactive --agree-tos -m "$BLUESTREAM_ADMIN_EMAIL" \
        --keep-until-expiring --redirect \
        || bs_die "certbot failed. Check the output above."

    BLUESTREAM_SSL_ENABLED="yes"
    bs_save_server_conf

    # Regenerate our own managed config (now with the HTTPS block) and reload.
    nginx_gen_config
    nginx_site_enable
    if nginx_test; then
        systemctl reload nginx 2>/dev/null || true
        # Apply the new secure_cookie state to a running web console.
        ssl_restart_web_console
        bs_ok "HTTPS enabled for $BLUESTREAM_DOMAIN"
    else
        bs_warn "Nginx failed validation after SSL setup; review /etc/nginx/sites-available/bluestream"
        return 1
    fi
    return 0
}

# Restart an ACTIVE bluestream-web.service after HTTPS is enabled so the
# running Flask process reloads /var/lib/bluestream/web/web.conf
# (secure_cookie=yes) and starts issuing Secure cookies. Absent/inactive
# services are never started; a real restart failure is reported visibly
# because cookie security depends on the new state being applied.
ssl_restart_web_console() {
    local unit="bluestream-web.service"
    [ -f "/etc/systemd/system/$unit" ] || return 0  # web console not installed
    if [ "$(systemctl is-active "$unit" 2>/dev/null)" != "active" ]; then
        bs_info "Web console service is not active; leaving it as-is."
        return 0
    fi
    if systemctl restart "$unit" 2>/dev/null; then
        bs_ok "Web console service restarted to apply Secure cookie state."
    else
        bs_warn "Web console service restart FAILED; HTTPS is enabled but the running console may still issue non-Secure cookies."
        bs_warn "Run manually: systemctl restart $unit"
    fi
    return 0
}

ssl_status() {
    bs_require_cmd certbot || bs_die "certbot is not installed"
    certbot certificates 2>/dev/null || true
}

ssl_renew() {
    bs_require_root
    bs_require_cmd certbot || bs_die "certbot is not installed"
    certbot renew --quiet || true
    if nginx_running; then
        systemctl reload nginx 2>/dev/null || true
    fi
    bs_ok "Certificate renewal pass complete."
}

ssl_menu() {
    local choice
    bs_select "SSL Management" choice \
        "Issue / refresh SSL certificate (Let's Encrypt)" \
        "Show certificate status" \
        "Renew certificates now" || return 1
    case "$choice" in
        Issue*) ssl_issue ;;
        Show*) ssl_status ;;
        Renew*) ssl_renew ;;
    esac
    return 0
}
