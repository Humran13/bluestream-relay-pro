#!/usr/bin/env bash
# BlueStream Relay Pro - installation information / "PORT CONFIGURATION"
# reporting, shared by install.sh (post-install output) and
# `bluestream-manager installinfo` so the two never drift apart.
#
# Every value printed here is read from the ACTUAL installed/persisted
# configuration (server.conf, live systemd/service state) - nothing is ever
# a hardcoded example port or domain. See the task notes: never falsely
# report public readiness from a merely-running local backend.
#
# BlueStream Relay Pro 0.1.0 (foundation). See LICENSE for terms.

if [ -n "${BLUESTREAM_INFO_LOADED:-}" ]; then
    return 0 2>/dev/null || exit 0
fi
BLUESTREAM_INFO_LOADED=1

# ---------------------------------------------------------------------------
# PORT CONFIGURATION section (requirement 3).
# ---------------------------------------------------------------------------
bs_print_port_configuration() {
    local strategy="${BLUESTREAM_NGINX_STRATEGY:-owned}"
    bs_step "PORT CONFIGURATION"

    if [ "$strategy" = "isolated" ]; then
        printf '  Installation mode         : ISOLATED (BlueStream runs its own independent nginx GATEWAY; the existing web server/panel is never modified)\n'
    else
        printf '  Installation mode         : OWNED (BlueStream manages the system nginx directly)\n'
    fi

    printf '  Internal Gunicorn port   : 127.0.0.1:%s (Flask app only - serves /console/ ONLY. Never point a reverse proxy at this port directly: it has no /player/ or /hls/ routes at all, so /player/ and /hls/ would 404 through it even though /console/ works.)\n' "${BLUESTREAM_WEB_PORT:-<not yet selected>}"

    if [ "$strategy" = "isolated" ]; then
        printf '  Isolated streaming gateway: 127.0.0.1:%s (BlueStream'\''s own nginx - fronts Gunicorn for /console/ AND serves /player/ and /hls/ itself)\n' "${BLUESTREAM_ISOLATED_HTTP_PORT:-<not yet selected>}"
        printf '  Public HTTPS              : provided by the hosting panel (BlueStream has no isolated HTTPS listener)\n'
        printf '  Reverse-proxy destination : 127.0.0.1:%s  <-- point your control panel / reverse proxy vhost at EXACTLY this (the gateway, NOT the Gunicorn port above)\n' "${BLUESTREAM_ISOLATED_HTTP_PORT:-<not yet selected>}"
    else
        printf '  Nginx HTTP port           : 0.0.0.0:80 (this IS the gateway in owned mode - serves /console/, /player/ and /hls/ together)\n'
        if [ "${BLUESTREAM_SSL_ENABLED:-no}" = "yes" ]; then
            printf '  Nginx HTTPS port          : 0.0.0.0:443 (Lets Encrypt certificate verified present for %s)\n' "${BLUESTREAM_DOMAIN:-<domain>}"
        else
            printf '  Nginx HTTPS port          : not configured (no verified certificate)\n'
        fi
        printf '  Reverse-proxy destination : not applicable (owned mode - nginx above already serves the public domain directly; no separate proxy needed)\n'
    fi

    printf '  Public domain             : %s\n' "${BLUESTREAM_DOMAIN:-(not set - using the bare server IP; see docs/installation.md)}"
    printf '  Public dashboard URL      : %s/console/\n' "$(bs_public_base)"
    printf '  Player URL format         : %s/player/?relay=<name>  (or ?playlist=<name>)\n' "$(bs_public_base)"
    printf '  RTMP ingest               : %s:%s (bind: %s)\n' "$BLUESTREAM_RTMP_BIND" "$BLUESTREAM_RTMP_PORT" "$BLUESTREAM_RTMP_BIND"
    printf '  RTMP external ingestion   : DISABLED - loopback-only (127.0.0.1), never internet-accessible. Only local FFmpeg processes (started by BlueStream itself) can publish to it. No firewall rule is needed or should be opened for this port; BlueStream never auto-exposes it or auto-modifies firewall rules.\n'
    printf '  HLS endpoint format       : %s/hls/relay/<name>/index.m3u8\n' "$(bs_public_base)"
    printf '                              %s/hls/playlist/<name>/index.m3u8\n' "$(bs_public_base)"

    if [ -z "${BLUESTREAM_WEB_PORT:-}" ]; then
        bs_warn "WEB_PORT is not set - the web console is not fully configured yet."
    fi
    if [ "$strategy" = "isolated" ]; then
        bs_warn "Public HTTPS is terminated by the hosting panel and cannot be verified from BlueStream's loopback-only gateway alone."
    elif [ "${BLUESTREAM_SSL_ENABLED:-no}" != "yes" ]; then
        bs_warn "HTTPS is NOT verified/configured for this install. Do not treat the dashboard as publicly HTTPS-ready."
    fi
    if [ "$strategy" = "isolated" ]; then
        bs_warn "ISOLATED MODE: point your control panel at the GATEWAY port (127.0.0.1:${BLUESTREAM_ISOLATED_HTTP_PORT:-<unset>}), never"
        bs_warn "at the bare Gunicorn port (127.0.0.1:${BLUESTREAM_WEB_PORT:-<unset>}). Pointing at Gunicorn directly is exactly the"
        bs_warn "misconfiguration that makes /console/ work (a 302) while /player/ and /hls/ both 404 - Gunicorn has no routes"
        bs_warn "for those paths at all; only the gateway serves them."
    fi
}

# ---------------------------------------------------------------------------
# Concrete reverse-proxy instructions for the detected/recorded panel
# (requirement 4). Printed once, right after PORT CONFIGURATION, whenever
# strategy=isolated.
# ---------------------------------------------------------------------------
bs_print_panel_instructions() {
    [ "${BLUESTREAM_NGINX_STRATEGY:-owned}" = "isolated" ] || return 0
    # The reverse-proxy target is ALWAYS the isolated streaming GATEWAY
    # (BlueStream's own nginx), never the bare Gunicorn port: Gunicorn/Flask
    # only implements /console/ routes - it has no /player/ or /hls/ routes
    # at all, so pointing a reverse proxy straight at it makes /console/
    # work while /player/ and /hls/ both 404. The gateway fronts Gunicorn
    # for /console/ AND serves /player/ and /hls/ itself (see
    # nginx_isolated_gen_config in lib/nginx.sh).
    local port="${BLUESTREAM_ISOLATED_HTTP_PORT:-<gateway-port>}"
    local panel="${BS_ENV_PANEL:-}"

    bs_step "CONTROL PANEL / REVERSE PROXY SETUP"
    bs_info "Target BlueStream's isolated streaming GATEWAY port (127.0.0.1:${port}) below - NOT the Gunicorn port"
    bs_info "(127.0.0.1:${BLUESTREAM_WEB_PORT:-<web-port>}). The gateway serves /console/, /player/ and /hls/ together;"
    bs_info "Gunicorn alone only serves /console/."
    case "$panel" in
        cloudpanel)
            cat <<EOF
  CloudPanel detected. In CloudPanel:
    Sites -> (your site) -> Vhost -> add/edit a reverse proxy location:
      location / {
          proxy_pass http://127.0.0.1:${port};
          proxy_set_header Host \$host;
          proxy_set_header X-Real-IP \$remote_addr;
          proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
          proxy_set_header X-Forwarded-Proto \$scheme;
      }
    Save and let CloudPanel issue/attach its own certificate as usual.
EOF
            ;;
        aapanel|hestiacp|plesk|ispconfig|virtualmin|nginxproxymanager)
            cat <<EOF
  Control panel detected: $panel. Add a reverse-proxy / custom vhost rule in
  its UI pointing at: http://127.0.0.1:${port}/
  (forward Host, X-Real-IP, X-Forwarded-For and X-Forwarded-Proto headers;
  let the panel terminate HTTPS as it does for any other site it manages.)
EOF
            ;;
        *)
            cat <<EOF
  No specific control panel was recognized, but an existing web server/nginx
  configuration was detected, so BlueStream is running in ISOLATED mode.
  Add a reverse-proxy vhost in whatever manages this host's nginx, e.g.:
    server {
        listen 443 ssl;
        server_name ${BLUESTREAM_DOMAIN:-stream.example.com};
        # ... your existing certificate directives ...
        location / {
            proxy_pass http://127.0.0.1:${port};
            proxy_set_header Host \$host;
            proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto \$scheme;
        }
    }
  TCP vs UDP: this is plain HTTP/HTTPS reverse proxying (TCP only). The RTMP
  ingest (127.0.0.1:${BLUESTREAM_RTMP_PORT}) is also TCP and is loopback-only;
  it is never reverse-proxied and never needs a firewall rule.
EOF
            ;;
    esac
    bs_warn "BlueStream never edits a panel-owned config file automatically and never opens firewall ports for you -"
    bs_warn "the reverse-proxy rule above and any firewall changes are applied manually by you, in your panel/firewall."
}

# ---------------------------------------------------------------------------
# Full installation summary (requirements 7/8): version, OS, environment,
# strategy, ports/URLs, admin status, service status, firewall, commands.
# Used by both install.sh's post_install_info and
# `bluestream-manager installinfo`. Never prints secrets/keys/tokens.
# ---------------------------------------------------------------------------
bs_print_install_summary() {
    # Re-detect the live environment (read-only, cheap) so "Hosting
    # environment" and the panel-specific reverse-proxy instructions below
    # are always current - this function is called both right after
    # install.sh's own detect_environment() (redundant but harmless there)
    # and from `bluestream-manager installinfo`, which otherwise would never
    # populate BS_ENV_MODE/BS_ENV_PANEL at all and show stale/blank values.
    # This NEVER changes BLUESTREAM_NGINX_STRATEGY - the persisted strategy in
    # server.conf remains authoritative for what is actually installed;
    # this only refreshes the informational detection fields for display.
    if command -v bs_env_detect >/dev/null 2>&1; then
        bs_env_detect >/dev/null 2>&1
    fi
    bs_step "BlueStream Relay Pro - Installation Summary"
    printf '  Version                  : %s\n' "$(bs_version)"
    printf '  OS                       : %s %s\n' "${OS_NAME:-unknown}" "${OS_VERSION:-}"
    printf '  Hosting environment      : %s\n' "$(bs_env_mode_label 2>/dev/null || printf 'unknown')"
    printf '  Nginx strategy           : %s\n' "${BLUESTREAM_NGINX_STRATEGY:-owned}"
    printf '\n'
    bs_print_port_configuration
    printf '\n'

    local admin_status web_active nginx_active
    if command -v web_admin_status >/dev/null 2>&1; then
        admin_status="$(web_admin_status)"
    else
        admin_status="unknown"
    fi
    web_active="$(systemctl is-active bluestream-web.service 2>/dev/null || printf 'unknown')"
    if command -v nginx_status >/dev/null 2>&1; then
        nginx_active="$(nginx_status)"
    else
        nginx_active="unknown"
    fi

    printf '  Admin account            : %s\n' "$admin_status"
    printf '  Web console service      : %s\n' "$web_active"
    printf '  Nginx                    : %s\n' "$nginx_active"
    printf '  Firewall                 : %s\n' "${BLUESTREAM_FIREWALL:-none}"

    case "$admin_status" in
        pending*)
            bs_warn "Admin onboarding is PENDING - the application is installed but the dashboard cannot be logged into yet."
            bs_warn "Finish it with: sudo bluestream-manager admin init"
            ;;
    esac
    if [ "$web_active" != "active" ]; then
        bs_warn "bluestream-web.service is not active - the dashboard is not currently reachable."
    fi
    if [ "$nginx_active" != "active" ]; then
        bs_warn "Nginx (${BLUESTREAM_NGINX_STRATEGY:-owned} strategy) is not active - public HLS/console delivery is not currently reachable."
    fi
    if [ "${BLUESTREAM_NGINX_STRATEGY:-owned}" = "isolated" ]; then
        bs_warn "Public reachability depends on a reverse-proxy rule in your control panel that BlueStream cannot verify from here."
    fi
    bs_warn "This summary reflects local service state only; it is never a substitute for verifying the public URL in a browser."

    printf '\n  Manager       : sudo bluestream-manager\n'
    printf '  Diagnostics   : sudo bluestream-manager diagnostics\n'
    printf '  Self-test     : sudo bluestream-manager selftest\n'
    printf '  Install info  : sudo bluestream-manager installinfo\n'
}

# end of info.sh
