#!/usr/bin/env bash
# BlueStream Relay Pro - end-to-end self-test.
#
# Generates a tiny H.264/AAC sample, creates a temporary relay, starts it
# through the real systemd path, verifies HLS output and the public URL,
# then removes only the exact temporary artifacts. Real relays and media
# are never touched.
#
# BlueStream Relay Pro 0.1.0 (foundation). See LICENSE for terms.

if [ -n "${BLUESTREAM_SELFTEST_LOADED:-}" ]; then
    return 0 2>/dev/null || exit 0
fi
BLUESTREAM_SELFTEST_LOADED=1

run_selftest() {
    bs_require_root
    bs_require_cmd ffmpeg
    bs_require_cmd ffprobe
    bs_require_cmd curl

    local ts="$$"
    local sample="$BLUESTREAM_MEDIA_DIR/.selftest-$ts.mp4"
    local name="selftest-$ts"
    local m3u8dir pid user code base i passed=0 failed=0
    local unit comm settled rtmp_ok rtmp_pidnote rtmp_lines

    check() {
        local label="$1" result="$2"
        if [ "$result" = "0" ]; then
            passed=$((passed + 1))
            bs_ok "$label"
        else
            failed=$((failed + 1))
            bs_error "$label"
        fi
    }

    bs_step "BlueStream Relay Pro self-test (test id: $name)"
    bs_warn "This creates a temporary relay named '$name' and removes it afterwards."

    # Clear any leftovers from a previous aborted run with the same id.
    systemctl stop "$(bs_unit relay "$name")" 2>/dev/null || true
    rm -f "$sample"
    rm -rf "$(bs_hls_dir_for relay "$name")"
    rm -f "$BLUESTREAM_RELAY_CONF_DIR/$name.conf"

    # 1. Generate a tiny compatible sample under the managed media directory
    #    (NOT host /tmp - the service runs with PrivateTmp).
    ffmpeg -y -v error -f lavfi -i testsrc2=size=320x240:rate=15 -f lavfi \
        -i sine=frequency=440:sample_rate=44100 -t 6 \
        -c:v libx264 -profile:v baseline -pix_fmt yuv420p -g 15 \
        -c:a aac -b:a 96k -shortest "$sample" >/dev/null 2>&1
    if [ -s "$sample" ]; then
        check "generate test media sample" 0
    else
        check "generate test media sample" 1
    fi

    # 2. Create the temporary relay config (bypassing interactive prompts).
    RELAY_NAME="$name"
    RELAY_TYPE="local-file"
    RELAY_URL="$sample"
    RELAY_LOOP="yes"
    RELAY_RESTART_SEC="5"
    RELAY_ENABLED="no"
    RELAY_NOTE="self-test"
    RELAY_CREATED="$(bs_now_ts)"
    if relay_config_validate && relay_save_config "$name"; then
        check "create temporary relay config" 0
    else
        check "create temporary relay config" 1
    fi

    # Verify the exact argv that run-relay.sh will hand to FFmpeg: separate
    # [-c:v][copy][-c:a][copy] elements and a private loopback RTMP output.
    if relay_build_ffmpeg_args && bs_verify_ffmpeg_args; then
        check "ffmpeg argv valid ([-c:v][copy][-c:a][copy] -> rtmp://127.0.0.1:1935/bluestream-relay/<name>)" 0
    else
        bs_error "FFmpeg argv failed structural verification:"
        bs_dump_ffmpeg_args
        check "ffmpeg argv valid ([-c:v][copy][-c:a][copy] -> rtmp://127.0.0.1:1935/bluestream-relay/<name>)" 1
    fi

    # 3. Start through the real systemd path.
    bs_ensure_hls_dir relay "$name" || true
    systemctl start "$(bs_unit relay "$name")" 2>/dev/null
    check "start relay via systemd" "$?"

    # 4. Verify the stable production FFmpeg process runs as bluestream-relay.
    #    Wait for the service to settle on an actual FFmpeg process (MainPID
    #    comm == 'ffmpeg'). The temporary root bootstrap wrapper (bash/setpriv)
    #    must never satisfy this check, and a crash/restart loop must be
    #    reported explicitly rather than misread as "FFmpeg runs as root".
    unit="$(bs_unit relay "$name")"
    settled=""
    for i in $(seq 1 30); do
        case "$(systemctl is-active "$unit" 2>/dev/null)" in
            active)
                pid="$(systemctl show -p MainPID --value "$unit" 2>/dev/null)"
                comm="$(ps -o comm= -p "${pid:-0}" 2>/dev/null | tr -d ' ')"
                if [ "$comm" = "ffmpeg" ]; then
                    settled=1
                    break
                fi
                ;;
            failed)
                bs_error "Relay service entered failed state (crashed / restart-limited); cannot verify FFmpeg user."
                check "ffmpeg runs as $BLUESTREAM_USER (service stable)" 1
                check "relay process running" 1
                break
                ;;
        esac
        sleep 1
    done
    if [ -n "$settled" ]; then
        user="$(ps -o user= -p "$pid" 2>/dev/null | tr -d ' ')"
        if [ "$user" = "$BLUESTREAM_USER" ]; then
            check "ffmpeg runs as $BLUESTREAM_USER (service stable)" 0
        else
            bs_error "ffmpeg (pid $pid) runs as '$user', want '$BLUESTREAM_USER'"
            check "ffmpeg runs as $BLUESTREAM_USER (service stable)" 1
        fi
        check "relay process running" 0
    elif [ "$(systemctl is-active "$unit" 2>/dev/null)" != "failed" ]; then
        bs_error "Relay service did not settle to a running FFmpeg process within 30s (may be crash-looping)."
        check "ffmpeg runs as $BLUESTREAM_USER (service stable)" 1
        check "relay process running" 1
    fi

    # 4b. Verify FFmpeg is publishing to the private loopback RTMP application.
    #     Settle-aware: poll up to 15s for an ESTABLISHED client connection to
    #     127.0.0.1:1935. A LISTEN socket alone is never sufficient (nginx is
    #     always listening); we require an actual client connection. ss output
    #     is captured first and matched with `case`, avoiding `grep -q` inside
    #     a pipeline (grep -q exits early and is not pipefail-safe).
    if [ -n "$settled" ]; then
        rtmp_ok=""
        rtmp_pidnote=""
        if command -v ss >/dev/null 2>&1; then
            for i in $(seq 1 15); do
                rtmp_lines="$(ss -tnp 2>/dev/null | grep "127.0.0.1:${BLUESTREAM_RTMP_PORT}" || true)"
                case "$rtmp_lines" in
                    *ESTAB*)
                        rtmp_ok=1
                        case "$rtmp_lines" in
                            *"pid=$pid"*) rtmp_pidnote=" (ffmpeg pid $pid)" ;;
                        esac
                        break
                        ;;
                esac
                sleep 1
            done
            if [ -n "$rtmp_ok" ]; then
                check "ffmpeg publishing to private RTMP (127.0.0.1:1935)$rtmp_pidnote" 0
            else
                bs_error "No ESTABLISHED client connection from FFmpeg to 127.0.0.1:1935 within 15s"
                check "ffmpeg publishing to private RTMP (127.0.0.1:1935)" 1
            fi
        else
            bs_warn "ss not available; skipping RTMP connection check"
        fi
    fi

    # 5. Wait for HLS playlist and segments.
    m3u8dir="$(bs_hls_dir_for relay "$name")"
    for i in $(seq 1 45); do
        bs_hls_is_fresh "$m3u8dir" 60 && break
        sleep 1
    done
    if bs_hls_is_fresh "$m3u8dir" 60; then
        check "HLS playlist fresh" 0
    else
        check "HLS playlist fresh" 1
    fi
    # Wait for a few segments (the -re loop produces them in real time).
    for i in $(seq 1 30); do
        [ "$(bs_hls_segment_count "$m3u8dir")" -ge 3 ] && break
        sleep 1
    done
    if [ "$(bs_hls_segment_count "$m3u8dir")" -ge 3 ]; then
        check "HLS segments present" 0
    else
        check "HLS segments present" 1
    fi
    # The HLS files must be created by nginx-rtmp (www-data), not FFmpeg.
    if [ -f "$m3u8dir/index.m3u8" ] && [ "$(stat -c %U "$m3u8dir/index.m3u8" 2>/dev/null)" = "$BLUESTREAM_NGINX_USER" ]; then
        check "HLS output created by nginx-rtmp (www-data)" 0
    else
        check "HLS output created by nginx-rtmp (www-data)" 1
    fi

    # 6. Fetch the public M3U8 URL via nginx. Connects to loopback directly
    #    and sets the Host header explicitly (matching the web-console checks
    #    further below) so this never depends on the configured domain
    #    actually resolving in DNS - it still exercises the real nginx
    #    server_name vhost match. The HLS/player locations on port 80 are
    #    served directly regardless of SSL state (only /console/ redirects to
    #    HTTPS when SSL is enabled - see bluestream-site.conf.template), so a
    #    plain http://127.0.0.1 request is correct in both cases.
    m3u8_url="http://127.0.0.1/hls/relay/$name/index.m3u8"
    host_header=()
    ffprobe_headers=()
    if [ -n "$BLUESTREAM_DOMAIN" ]; then
        host_header=(-H "Host: $BLUESTREAM_DOMAIN")
        # ffmpeg/ffprobe's -headers value needs a REAL CRLF terminator, not
        # the two literal characters '\' 'r' '\' 'n' a double-quoted string
        # would produce - use ANSI-C quoting ($'...') so bash expands \r\n to
        # actual control characters before ffprobe ever sees the argument.
        ffprobe_headers=(-headers $'Host: '"$BLUESTREAM_DOMAIN"$'\r\n')
    fi
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "${host_header[@]}" "$m3u8_url" 2>/dev/null)"
    if [ "$code" = "200" ]; then
        check "public M3U8 URL reachable (HTTP 200)" 0
    else
        bs_error "Public URL returned HTTP ${code:-unreachable}"
        check "public M3U8 URL reachable (HTTP 200)" 1
    fi

    # 7. Confirm the output playlist is decodable.
    if ffprobe -v error "${ffprobe_headers[@]}" "$m3u8_url" >/dev/null 2>&1; then
        check "output decodable by ffprobe" 0
    else
        check "output decodable by ffprobe" 1
    fi

    # 8. Phase 1C: outbound publisher. The destination points back at THIS
    #    SAME private nginx-rtmp bus under a different stream name - a real
    #    loopback RTMP publish/subscribe round trip with no external
    #    dependency, mirroring the deterministic local-sink acceptance test.
    #    Verifies: the publisher unit activates, status reaches LIVE (real
    #    data flow, not just "unit exists"), detaching stops it, and the
    #    relay/website HLS output is completely unaffected throughout.
    if command -v dest_create >/dev/null 2>&1 && command -v publisher_sync_target >/dev/null 2>&1; then
        local destid="selftest-pub-$ts" destsink="selftest-sink-$ts" pubunit pub_ok="" i
        dest_exists "$destid" && dest_delete "$destid" >/dev/null 2>&1
        if dest_create "$destid" "Self-test sink" "custom" \
            "${BLUESTREAM_RTMP_BASE}/${BLUESTREAM_RTMP_APP_RELAY}" "$destsink" "yes" >/dev/null 2>&1; then
            check "create temporary publisher test destination" 0
        else
            check "create temporary publisher test destination" 1
        fi
        if relay_set_destinations "$name" "$destid" >/dev/null 2>&1; then
            check "attach test destination to relay" 0
        else
            check "attach test destination to relay" 1
        fi
        pubunit="$(publisher_unit relay "$name" "$destid")"
        for i in $(seq 1 20); do
            [ "$(systemctl is-active "$pubunit" 2>/dev/null)" = "active" ] && { pub_ok=1; break; }
            sleep 1
        done
        check "publisher unit activates ($pubunit)" "$([ -n "$pub_ok" ] && printf 0 || printf 1)"
        if [ -n "$pub_ok" ]; then
            local live_ok=""
            for i in $(seq 1 20); do
                publisher_status relay "$name" "$destid"
                [ "$PUBLISHER_STATE" = "LIVE" ] && { live_ok=1; break; }
                sleep 1
            done
            check "publisher reaches LIVE (real data flow observed)" "$([ -n "$live_ok" ] && printf 0 || printf 1)"
        fi
        # Detach: the publisher must stop; the relay/HLS must be unaffected.
        relay_set_destinations "$name" "" >/dev/null 2>&1
        local detach_ok=""
        for i in $(seq 1 10); do
            [ "$(systemctl is-active "$pubunit" 2>/dev/null)" != "active" ] && { detach_ok=1; break; }
            sleep 1
        done
        check "publisher stops on detach" "$([ -n "$detach_ok" ] && printf 0 || printf 1)"
        check "relay/HLS output unaffected by publisher lifecycle" "$([ "$(systemctl is-active "$unit" 2>/dev/null)" = "active" ] && printf 0 || printf 1)"
        publisher_remove relay "$name" "$destid" >/dev/null 2>&1
        dest_delete "$destid" >/dev/null 2>&1
        rm -rf "$(bs_hls_dir_for relay "$destsink")" 2>/dev/null || true
    else
        bs_warn "Phase 1C publisher libraries not loaded; skipping publisher self-test"
    fi

    # Cleanup: remove only the exact temporary test artifacts.
    systemctl stop "$(bs_unit relay "$name")" 2>/dev/null || true
    sleep 1
    rm -f "$sample"
    rm -rf "$m3u8dir"
    rm -f "$BLUESTREAM_RELAY_CONF_DIR/$name.conf"
    rm -f "$BLUESTREAM_RUN_DIR/$name.concat.txt"

    # --- web console (GUI-1A.3B) ---
    # Only exercised when the production web console is actually installed;
    # never requires authentication and never creates credentials.
    if [ -x /usr/bin/gunicorn ] && id bluestream-web >/dev/null 2>&1 \
        && [ -f /etc/sudoers.d/bluestream-web ] && command -v python3 >/dev/null 2>&1; then
        local webctl="/usr/local/lib/bluestream/web-ctl" wout wloc wrc wcode sslines
        # Real privilege boundary: run as bluestream-web, sudo -n elevates to
        # root web-ctl, output must be valid JSON with the expected version.
        wout="$(runuser -u bluestream-web -- /usr/bin/sudo -n "$webctl" version 2>/dev/null)"
        wrc=$?
        if [ "$wrc" -eq 0 ] && printf '%s' "$wout" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("ok") is True and d.get("data", {}).get("version") == "0.1.0"' >/dev/null 2>&1; then
            check "web-ctl via sudo boundary (bluestream-web -> root) returns valid JSON" 0
        else
            check "web-ctl via sudo boundary (bluestream-web -> root) returns valid JSON" 1
        fi
        local wport="${BLUESTREAM_WEB_PORT:-8080}"
        if command -v ss >/dev/null 2>&1 && bs_valid_port "$wport"; then
            sslines="$(ss -ltn 2>/dev/null | grep ":${wport} " || true)"
            case "$sslines" in
                *"127.0.0.1:${wport}"*)
                    check "Gunicorn listener on 127.0.0.1:${wport}" 0
                    case "$sslines" in
                        *"0.0.0.0:${wport}"*|*"[::]:${wport}"*) check "Gunicorn listener loopback only" 1 ;;
                        *) check "Gunicorn listener loopback only" 0 ;;
                    esac
                    ;;
                *) check "Gunicorn listener on 127.0.0.1:${wport}" 1 ;;
            esac
        else
            bs_warn "ss not available (or no valid web port configured); skipping web console listener checks"
        fi
        if bs_have_cmd curl && nginx_running; then
            # Local loopback check against the configured BlueStream domain.
            # When HTTPS is enabled, nginx intentionally redirects HTTP
            # /console/ to HTTPS (301); otherwise it serves directly (200).
            if [ -n "${BLUESTREAM_DOMAIN:-}" ] && bs_valid_domain "$BLUESTREAM_DOMAIN"; then
                wout="$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' --max-time 15 \
                    -H "Host: $BLUESTREAM_DOMAIN" \
                    "http://127.0.0.1/console/login" 2>/dev/null)"
                wcode="${wout%% *}"
                wloc="${wout#* }"
                if [ "$BLUESTREAM_SSL_ENABLED" = "yes" ]; then
                    # Exact redirect target; any other 301/Location is rejected.
                    if [ "$wcode" = "301" ] && [ "$wloc" = "https://$BLUESTREAM_DOMAIN/console/login" ]; then
                        check "web console HTTP->HTTPS redirect valid (HTTP 301)" 0
                    else
                        check "web console HTTP->HTTPS redirect valid (HTTP 301)" 1 "HTTP ${wcode:-unreachable} Location=${wloc:-none}"
                    fi
                else
                    if [ "$wcode" = "200" ]; then
                        check "web console login page reachable via nginx (HTTP 200)" 0
                    else
                        check "web console login page reachable via nginx (HTTP 200)" 1 "HTTP ${wcode:-unreachable}"
                    fi
                fi
            else
                bs_warn "No valid BlueStream domain configured; skipping web console HTTP check"
            fi
        else
            bs_warn "curl/nginx unavailable; skipping web console HTTP check"
        fi
    else
        bs_warn "Web console prerequisites not present; skipping web console self-test"
    fi

    bs_step "Self-test complete: $passed passed, $failed failed"
    [ "$failed" -eq 0 ]
}
