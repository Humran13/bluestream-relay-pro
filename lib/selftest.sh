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
    local unit comm settled

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
    # [-c:v][copy][-c:a][copy] elements and a final index.m3u8 output.
    if relay_build_ffmpeg_args && bs_verify_ffmpeg_args; then
        check "ffmpeg argv valid ([-c:v][copy][-c:a][copy] -> index.m3u8)" 0
    else
        bs_error "FFmpeg argv failed structural verification:"
        bs_dump_ffmpeg_args
        check "ffmpeg argv valid ([-c:v][copy][-c:a][copy] -> index.m3u8)" 1
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

    # 6. Fetch the public M3U8 URL via nginx.
    if [ -n "$BLUESTREAM_DOMAIN" ]; then
        base="$(bs_public_base)"
    else
        base="http://127.0.0.1"
    fi
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$base/hls/relay/$name/index.m3u8" 2>/dev/null)"
    if [ "$code" = "200" ]; then
        check "public M3U8 URL reachable (HTTP 200)" 0
    else
        bs_error "Public URL returned HTTP ${code:-unreachable}"
        check "public M3U8 URL reachable (HTTP 200)" 1
    fi

    # 7. Confirm the output playlist is decodable.
    if ffprobe -v error "$base/hls/relay/$name/index.m3u8" >/dev/null 2>&1; then
        check "output decodable by ffprobe" 0
    else
        check "output decodable by ffprobe" 1
    fi

    # Cleanup: remove only the exact temporary test artifacts.
    systemctl stop "$(bs_unit relay "$name")" 2>/dev/null || true
    sleep 1
    rm -f "$sample"
    rm -rf "$m3u8dir"
    rm -f "$BLUESTREAM_RELAY_CONF_DIR/$name.conf"
    rm -f "$BLUESTREAM_RUN_DIR/$name.concat.txt"

    bs_step "Self-test complete: $passed passed, $failed failed"
    [ "$failed" -eq 0 ]
}
