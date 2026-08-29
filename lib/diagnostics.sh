#!/usr/bin/env bash
# BlueStream Relay Pro - diagnostics checklist.
#
# BlueStream Relay Pro 0.1.0 (foundation). See LICENSE for terms.

if [ -n "${BLUESTREAM_DIAGNOSTICS_LOADED:-}" ]; then
    return 0 2>/dev/null || exit 0
fi
BLUESTREAM_DIAGNOSTICS_LOADED=1

DIAG_PASS=0
DIAG_FAIL=0
DIAG_WARN=0

diag_reset() { DIAG_PASS=0; DIAG_FAIL=0; DIAG_WARN=0; }

diag_check() {
    local label="$1" result="$2" detail="${3:-}"
    case "$result" in
        PASS)
            DIAG_PASS=$((DIAG_PASS + 1))
            printf '  %s[PASS]%s %s %s\n' "$C_GREEN" "$C_RESET" "$label" "$detail"
            ;;
        FAIL)
            DIAG_FAIL=$((DIAG_FAIL + 1))
            printf '  %s[FAIL]%s %s %s\n' "$C_RED" "$C_RESET" "$label" "$detail"
            ;;
        WARN)
            DIAG_WARN=$((DIAG_WARN + 1))
            printf '  %s[WARN]%s %s %s\n' "$C_YELLOW" "$C_RESET" "$label" "$detail"
            ;;
    esac
}

run_diagnostics() {
    diag_reset
    bs_step "BlueStream Relay Pro diagnostics"
    local names name dir pid user code owner mode

    # --- base tools ---
    if bs_have_cmd nginx; then diag_check "Nginx installed" PASS; else diag_check "Nginx installed" FAIL; fi
    if nginx_running; then diag_check "Nginx running" PASS; else diag_check "Nginx running" FAIL; fi
    if nginx -t >/dev/null 2>&1; then diag_check "Nginx config valid" PASS; else diag_check "Nginx config valid" FAIL; fi

    # --- nginx RTMP module (private local ingest -> HLS) ---
    if [ -f /etc/nginx/modules-enabled/50-mod-rtmp.conf ] && [ -f /usr/lib/nginx/modules/ngx_rtmp_module.so ]; then
        diag_check "nginx RTMP module loaded" PASS
    else
        diag_check "nginx RTMP module loaded" FAIL "libnginx-mod-rtmp not enabled"
    fi
    if [ -f "$NGINX_SNIPPET_DIR/rtmp.conf" ] && [ -f "$NGINX_RTMP_INCLUDE" ]; then
        diag_check "BlueStream RTMP configuration present" PASS
    else
        diag_check "BlueStream RTMP configuration present" FAIL
    fi
    if command -v ss >/dev/null 2>&1; then
        if ss -ltn "sport = :${BLUESTREAM_RTMP_PORT}" 2>/dev/null | grep -q "127.0.0.1:${BLUESTREAM_RTMP_PORT}"; then
            diag_check "local RTMP listener exists (loopback)" PASS
        else
            diag_check "local RTMP listener exists (loopback)" FAIL "nothing listening on 127.0.0.1:1935"
        fi
        if ! ss -ltn "sport = :${BLUESTREAM_RTMP_PORT}" 2>/dev/null | grep -qE '0\.0\.0\.0:1935|\[::\]:1935'; then
            diag_check "RTMP listener loopback only" PASS
        else
            diag_check "RTMP listener loopback only" FAIL "RTMP listener must not be public"
        fi
    else
        diag_check "RTMP listener check" WARN "ss not available"
    fi
    if bs_have_cmd ffmpeg; then
        diag_check "FFmpeg available" PASS "$(ffmpeg -version 2>/dev/null | head -n1 | cut -c1-70)"
    else
        diag_check "FFmpeg available" FAIL
    fi
    if bs_have_cmd ffprobe; then diag_check "ffprobe available" PASS; else diag_check "ffprobe available" FAIL; fi

    # --- systemd ---
    if [ -f /etc/systemd/system/bluestream-relay@.service ]; then
        diag_check "systemd relay template installed" PASS
    else
        diag_check "systemd relay template installed" FAIL
    fi
    if [ -f /etc/systemd/system/bluestream-playlist@.service ]; then
        diag_check "systemd playlist template installed" PASS
    else
        diag_check "systemd playlist template installed" FAIL
    fi

    # --- user ---
    if id "$BLUESTREAM_USER" >/dev/null 2>&1; then
        diag_check "bluestream-relay user exists" PASS
    else
        diag_check "bluestream-relay user exists" FAIL
    fi

    # --- directories ---
    for d in \
        "$BLUESTREAM_ETC_DIR" "$BLUESTREAM_RELAY_CONF_DIR" "$BLUESTREAM_PLAYLIST_CONF_DIR" \
        "$BLUESTREAM_MEDIA_DIR" "$BLUESTREAM_RUN_DIR" \
        "$BLUESTREAM_HLS_ROOT" "$BLUESTREAM_HLS_RELAY_DIR" "$BLUESTREAM_HLS_PLAYLIST_DIR" \
        "$BLUESTREAM_WEB_DIR" "$BLUESTREAM_BACKUP_DIR"; do
        if [ -d "$d" ]; then
            diag_check "Directory exists: $d" PASS
        else
            diag_check "Directory exists: $d" FAIL
        fi
    done

    # config dir ownership and mode
    if [ -d "$BLUESTREAM_ETC_DIR" ]; then
        owner="$(stat -c '%U:%G' "$BLUESTREAM_ETC_DIR" 2>/dev/null)"
        mode="$(stat -c '%a' "$BLUESTREAM_ETC_DIR" 2>/dev/null)"
        if [ "$owner" = "root:root" ] && [ "$mode" = "700" ]; then
            diag_check "/etc/bluestream ownership/mode" PASS "root:root 0700"
        else
            diag_check "/etc/bluestream ownership/mode" FAIL "found $owner $mode (want root:root 0700)"
        fi
    fi

    # HLS tree owned by the nginx worker (nginx-rtmp writes it).
    if [ -d "$BLUESTREAM_HLS_ROOT" ]; then
        owner="$(stat -c '%U:%G' "$BLUESTREAM_HLS_ROOT" 2>/dev/null)"
        mode="$(stat -c '%a' "$BLUESTREAM_HLS_ROOT" 2>/dev/null)"
        if [ "$owner" = "$BLUESTREAM_NGINX_USER:$BLUESTREAM_NGINX_USER" ] && [ "$mode" = "750" ]; then
            diag_check "HLS root ownership/mode (nginx worker)" PASS "$owner $mode"
        else
            diag_check "HLS root ownership/mode (nginx worker)" WARN "found $owner $mode (want $BLUESTREAM_NGINX_USER:$BLUESTREAM_NGINX_USER 0750)"
        fi
    fi

    # --- relays ---
    names="$(relay_list_names)"
    if [ -z "$names" ]; then
        diag_check "Relays configured" WARN "none"
    fi
    for name in $names; do
        relay_load_config "$name" || continue
        health_state relay "$name"
        if [ "$HEALTH_STATE" = "HEALTHY" ]; then
            diag_check "Relay '$name' health" PASS
        else
            diag_check "Relay '$name' health" WARN "$HEALTH_STATE - $HEALTH_DETAILS"
        fi
        # source accessibility
        case "$RELAY_TYPE" in
            local-file)
                if [ -f "$RELAY_URL" ]; then
                    diag_check "Relay '$name' local source" PASS
                else
                    diag_check "Relay '$name' local source" FAIL "missing: $RELAY_URL"
                fi
                ;;
            http-file|remote-hls)
                if bs_have_cmd curl; then
                    code="$(curl -s -o /dev/null -w '%{http_code}' -I --max-time 15 "$RELAY_URL" 2>/dev/null)"
                    case "$code" in
                        200|206) diag_check "Relay '$name' source HTTP" PASS "HTTP $code" ;;
                        *)       diag_check "Relay '$name' source HTTP" WARN "HTTP ${code:-unreachable}" ;;
                    esac
                fi
                ;;
            *) diag_check "Relay '$name' source ($RELAY_TYPE)" WARN "not checked" ;;
        esac
        # ffmpeg process user + HLS freshness
        dir="$(bs_hls_dir_for relay "$name")"
        if bs_hls_is_fresh "$dir" 60; then
            pid="$(systemctl show -p MainPID --value "$(bs_unit relay "$name")" 2>/dev/null)"
            if [ -n "$pid" ] && [ "$pid" != "0" ]; then
                user="$(ps -o user= -p "$pid" 2>/dev/null | tr -d ' ')"
                if [ "$user" = "$BLUESTREAM_USER" ]; then
                    diag_check "Relay '$name' ffmpeg user" PASS "$user"
                else
                    diag_check "Relay '$name' ffmpeg user" FAIL "$user (want $BLUESTREAM_USER)"
                fi
            fi
            diag_check "Relay '$name' HLS freshness" PASS
        else
            diag_check "Relay '$name' HLS freshness" WARN "not fresh / not running"
        fi
    done

    # --- playlists ---
    names="$(playlist_list_names)"
    if [ -z "$names" ]; then
        diag_check "Playlists configured" WARN "none"
    fi
    for name in $names; do
        playlist_load_config "$name" || continue
        health_state playlist "$name"
        diag_check "Playlist '$name' health" "$([ "$HEALTH_STATE" = "HEALTHY" ] && printf 'PASS' || printf 'WARN')" "$HEALTH_STATE"
    done

    # --- HTTPS ---
    if [ -n "$BLUESTREAM_DOMAIN" ]; then
        if bs_have_cmd curl; then
            code="$(curl -s -o /dev/null -w '%{http_code}' -k --max-time 15 "https://$BLUESTREAM_DOMAIN/player/" 2>/dev/null)"
            case "$code" in
                200|301|302) diag_check "HTTPS reachable" PASS "HTTP $code" ;;
                *) diag_check "HTTPS reachable" WARN "HTTP ${code:-unreachable}" ;;
            esac
        fi
    else
        diag_check "HTTPS configured" WARN "no domain set"
    fi

    # --- web console (GUI-1A.3B) ---
    local web_user="bluestream-web" web_state="/var/lib/bluestream/web" wco wcm wso wsm wcode wact
    if id "$web_user" >/dev/null 2>&1; then
        diag_check "bluestream-web user exists" PASS
    else
        diag_check "bluestream-web user exists" FAIL
    fi
    if [ -x /usr/bin/gunicorn ]; then
        diag_check "Gunicorn binary (/usr/bin/gunicorn)" PASS
    else
        diag_check "Gunicorn binary (/usr/bin/gunicorn)" FAIL
    fi
    if [ -f /etc/systemd/system/bluestream-web.service ]; then
        diag_check "bluestream-web.service installed" PASS
    else
        diag_check "bluestream-web.service installed" FAIL
    fi
    wact="$(systemctl is-active bluestream-web.service 2>/dev/null)"
    if [ "$wact" = "active" ]; then
        diag_check "bluestream-web.service active" PASS
    else
        diag_check "bluestream-web.service active" WARN "${wact:-inactive}"
    fi
    if command -v ss >/dev/null 2>&1; then
        if ss -ltn "sport = :8080" 2>/dev/null | grep -q "127.0.0.1:8080"; then
            diag_check "Gunicorn listener (127.0.0.1:8080)" PASS
        else
            diag_check "Gunicorn listener (127.0.0.1:8080)" FAIL "nothing listening on loopback 8080"
        fi
        if ! ss -ltn "sport = :8080" 2>/dev/null | grep -qE '0\.0\.0\.0:8080|\[::\]:8080'; then
            diag_check "Gunicorn listener loopback only" PASS
        else
            diag_check "Gunicorn listener loopback only" FAIL "port 8080 must not be public"
        fi
    else
        diag_check "Gunicorn listener check" WARN "ss not available"
    fi
    if [ -f /usr/local/lib/bluestream/web-ctl ]; then
        wco="$(stat -c '%U:%G' /usr/local/lib/bluestream/web-ctl 2>/dev/null)"
        wcm="$(stat -c '%a' /usr/local/lib/bluestream/web-ctl 2>/dev/null)"
        if [ "$wco" = "root:root" ] && [ "$wcm" = "700" ]; then
            diag_check "web-ctl ownership/mode" PASS "root:root 0700"
        else
            diag_check "web-ctl ownership/mode" FAIL "found $wco $wcm (want root:root 0700)"
        fi
    else
        diag_check "web-ctl installed" FAIL
    fi
    if [ -f /etc/sudoers.d/bluestream-web ]; then
        diag_check "web console sudoers present" PASS
        if command -v visudo >/dev/null 2>&1; then
            if visudo -cf /etc/sudoers.d/bluestream-web >/dev/null 2>&1; then
                diag_check "web console sudoers validation (visudo -cf)" PASS
            else
                diag_check "web console sudoers validation (visudo -cf)" FAIL
            fi
        else
            diag_check "web console sudoers validation" WARN "visudo not available"
        fi
    else
        diag_check "web console sudoers present" FAIL
    fi
    if [ -d "$web_state" ]; then
        wso="$(stat -c '%U:%G' "$web_state" 2>/dev/null)"
        wsm="$(stat -c '%a' "$web_state" 2>/dev/null)"
        if [ "$wso" = "root:bluestream-web" ] && [ "$wsm" = "750" ]; then
            diag_check "web state dir ownership/mode" PASS "root:bluestream-web 0750"
        else
            diag_check "web state dir ownership/mode" WARN "found $wso $wsm (want root:bluestream-web 0750)"
        fi
    else
        diag_check "web state dir exists" FAIL
    fi
    if [ -f /etc/nginx/bluestream/console-location.conf ]; then
        diag_check "nginx console snippet present" PASS
    else
        diag_check "nginx console snippet present" FAIL
    fi
    if bs_have_cmd curl && nginx_running; then
        # Send the configured BlueStream domain as the Host header so the local
        # 127.0.0.1 request reaches the correct nginx server block (without it,
        # a default/other vhost may answer with 404). The domain comes from the
        # validated server.conf value; nothing is interpolated unvalidated.
        if [ -n "${BLUESTREAM_DOMAIN:-}" ] && bs_valid_domain "$BLUESTREAM_DOMAIN"; then
            wcode="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 \
                -H "Host: $BLUESTREAM_DOMAIN" \
                "http://127.0.0.1/console/login" 2>/dev/null)"
            if [ "$wcode" = "200" ]; then
                diag_check "web console login reachable" PASS "HTTP $wcode"
            else
                diag_check "web console login reachable" WARN "HTTP ${wcode:-unreachable}"
            fi
        else
            diag_check "web console login reachable" WARN "no valid BlueStream domain configured; check skipped"
        fi
    fi
    if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q 'Status: active'; then
        if ufw status 2>/dev/null | grep -Eq '^8080|8080/tcp|8080 '; then
            diag_check "UFW does not expose 8080" FAIL "port 8080 must stay loopback-only"
        else
            diag_check "UFW does not expose 8080" PASS
        fi
    fi

    # --- disk ---
    for p in "$BLUESTREAM_VAR_DIR" "$BLUESTREAM_WWW_DIR" /; do
        if [ -d "$p" ]; then
            diag_check "Disk space ($p)" PASS "$(bs_disk_free "$p")"
        fi
    done

    # --- bandwidth consideration ---
    diag_check "Bandwidth consideration" WARN "review source bitrate vs outbound capacity (see Bandwidth Estimator)"

    bs_step "Diagnostics summary"
    printf '  Pass: %d   Warnings: %d   Failures: %d\n' "$DIAG_PASS" "$DIAG_WARN" "$DIAG_FAIL"
    [ "$DIAG_FAIL" -eq 0 ]
}


