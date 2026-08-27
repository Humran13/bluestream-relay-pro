#!/usr/bin/env bash
# BlueStream Relay Pro - ffprobe helpers.
#
# Source probing, codec compatibility reporting, and bandwidth estimation
# primitives. Parsing uses ffprobe key=value output (no JSON parser needed).
#
# BlueStream Relay Pro 0.1.0 (foundation). See LICENSE for terms.

if [ -n "${BLUESTREAM_PROBE_LOADED:-}" ]; then
    return 0 2>/dev/null || exit 0
fi
BLUESTREAM_PROBE_LOADED=1

PROBE_OK=0
PV_CODEC=""; PV_WIDTH=""; PV_HEIGHT=""; PV_FPS=""; PV_FPS_RAW=""
PV_PIXFMT=""; PV_PROFILE=""; PV_BITRATE=""
PA_CODEC=""; PA_SAMPLE_RATE=""; PA_CHANNELS=""; PA_CHANNEL_LAYOUT=""
PF_FORMAT=""; PF_DURATION=""; PF_BITRATE=""; PF_SIZE=""; PF_NB_STREAMS=""

STREAM_COPY_RECOMMENDED="no"
STREAM_COPY_NOTE=""

probe_reset() {
    PROBE_OK=0
    PV_CODEC=""; PV_WIDTH=""; PV_HEIGHT=""; PV_FPS=""; PV_FPS_RAW=""
    PV_PIXFMT=""; PV_PROFILE=""; PV_BITRATE=""
    PA_CODEC=""; PA_SAMPLE_RATE=""; PA_CHANNELS=""; PA_CHANNEL_LAYOUT=""
    PF_FORMAT=""; PF_DURATION=""; PF_BITRATE=""; PF_SIZE=""; PF_NB_STREAMS=""
    STREAM_COPY_RECOMMENDED="no"
    STREAM_COPY_NOTE=""
}

bs_fps_display() {
    local r="$1" num den
    case "$r" in
        ''|0/0|0/1|N/A) printf 'unknown'; return ;;
    esac
    case "$r" in
        */*)
            num="${r%%/*}"
            den="${r##*/}"
            if [ "$den" = "0" ]; then printf 'unknown'; return; fi
            awk -v n="$num" -v d="$den" 'BEGIN{ printf "%.2f", n/d }'
            ;;
        *)
            printf '%s' "$r"
            ;;
    esac
}

probe_target() {
    local target="$1"
    probe_reset
    local opts=()
    case "$target" in
        rtsp://*)            opts+=( -rtsp_transport tcp -rw_timeout 15000000 ) ;;
        rtmp://*|rtmps://*)  opts+=( -rw_timeout 15000000 ) ;;
        http://*|https://*)  opts+=( -timeout 12000000 -rw_timeout 12000000 ) ;;
    esac
    opts+=( -analyzeduration 5000000 -probesize 5000000 )

    local vout aout fout
    vout="$(ffprobe -v error -of default=noprint_wrappers=1 \
        -select_streams v:0 \
        -show_entries stream=codec_name,width,height,r_frame_rate,avg_frame_rate,pix_fmt,profile,bit_rate \
        "${opts[@]}" "$target" 2>/dev/null)" || return 1
    aout="$(ffprobe -v error -of default=noprint_wrappers=1 \
        -select_streams a:0 \
        -show_entries stream=codec_name,sample_rate,channels,channel_layout \
        "${opts[@]}" "$target" 2>/dev/null)" || return 1
    fout="$(ffprobe -v error -of default=noprint_wrappers=1 \
        -show_entries format=format_name,duration,bit_rate,size,nb_streams \
        "${opts[@]}" "$target" 2>/dev/null)" || return 1

    PV_CODEC="$(printf '%s\n' "$vout" | sed -n 's/^codec_name=//p' | head -n1)"
    PV_WIDTH="$(printf '%s\n' "$vout" | sed -n 's/^width=//p' | head -n1)"
    PV_HEIGHT="$(printf '%s\n' "$vout" | sed -n 's/^height=//p' | head -n1)"
    PV_PIXFMT="$(printf '%s\n' "$vout" | sed -n 's/^pix_fmt=//p' | head -n1)"
    PV_PROFILE="$(printf '%s\n' "$vout" | sed -n 's/^profile=//p' | head -n1)"
    PV_BITRATE="$(printf '%s\n' "$vout" | sed -n 's/^bit_rate=//p' | head -n1)"

    local rfr afr
    rfr="$(printf '%s\n' "$vout" | sed -n 's/^r_frame_rate=//p' | head -n1)"
    afr="$(printf '%s\n' "$vout" | sed -n 's/^avg_frame_rate=//p' | head -n1)"
    PV_FPS_RAW="$rfr"
    if [ -z "$rfr" ] || [ "$rfr" = "0/1" ] || [ "$rfr" = "0/0" ]; then
        PV_FPS_RAW="$afr"
    fi
    PV_FPS="$(bs_fps_display "$PV_FPS_RAW")"

    PA_CODEC="$(printf '%s\n' "$aout" | sed -n 's/^codec_name=//p' | head -n1)"
    PA_SAMPLE_RATE="$(printf '%s\n' "$aout" | sed -n 's/^sample_rate=//p' | head -n1)"
    PA_CHANNELS="$(printf '%s\n' "$aout" | sed -n 's/^channels=//p' | head -n1)"
    PA_CHANNEL_LAYOUT="$(printf '%s\n' "$aout" | sed -n 's/^channel_layout=//p' | head -n1)"

    PF_FORMAT="$(printf '%s\n' "$fout" | sed -n 's/^format_name=//p' | head -n1)"
    PF_DURATION="$(printf '%s\n' "$fout" | sed -n 's/^duration=//p' | head -n1)"
    PF_BITRATE="$(printf '%s\n' "$fout" | sed -n 's/^bit_rate=//p' | head -n1)"
    PF_SIZE="$(printf '%s\n' "$fout" | sed -n 's/^size=//p' | head -n1)"
    PF_NB_STREAMS="$(printf '%s\n' "$fout" | sed -n 's/^nb_streams=//p' | head -n1)"

    PROBE_OK=1
    return 0
}

probe_media_path() {
    local p="$1"
    [ -f "$p" ] || return 1
    probe_target "$p"
}

probe_url() {
    local u="$1"
    bs_valid_url "$u" || return 1
    probe_target "$u"
}

# Returns 0 if the target looks like a decodable media source.
probe_quick_valid() {
    local target="$1"
    local opts=()
    case "$target" in
        rtsp://*)           opts+=( -rtsp_transport tcp -rw_timeout 15000000 ) ;;
        rtmp://*|rtmps://*) opts+=( -rw_timeout 15000000 ) ;;
        http://*|https://*) opts+=( -timeout 12000000 -rw_timeout 12000000 ) ;;
    esac
    ffprobe -v error -of default=noprint_wrappers=1 -show_entries format=format_name \
        "${opts[@]}" "$target" >/dev/null 2>&1
}

probe_compat_report() {
    local src_label="$1"
    STREAM_COPY_RECOMMENDED="no"
    STREAM_COPY_NOTE=""

    local res="unknown"
    if [ -n "${PV_WIDTH:-}" ] && [ -n "${PV_HEIGHT:-}" ]; then
        res="${PV_WIDTH}x${PV_HEIGHT}"
    fi

    bs_step "Probe report: $src_label"
    printf '  Video codec        : %s\n' "${PV_CODEC:-unknown}"
    printf '  Resolution         : %s\n' "$res"
    printf '  Frame rate         : %s\n' "${PV_FPS:-unknown}"
    printf '  Pixel format       : %s\n' "${PV_PIXFMT:-unknown}"
    printf '  Video bitrate      : %s bps\n' "${PV_BITRATE:-n/a}"
    printf '  Audio codec        : %s\n' "${PA_CODEC:-none}"
    printf '  Audio sample rate  : %s\n' "${PA_SAMPLE_RATE:-n/a}"
    printf '  Audio channels     : %s\n' "${PA_CHANNELS:-n/a}"
    printf '  Container format   : %s\n' "${PF_FORMAT:-unknown}"
    printf '  Duration (seconds) : %s\n' "${PF_DURATION:-n/a}"
    printf '  Overall bitrate    : %s bps\n' "${PF_BITRATE:-n/a}"

    case "$PV_CODEC" in
        h264|avc1)
            if [ -z "$PA_CODEC" ] || [ "$PA_CODEC" = "aac" ]; then
                STREAM_COPY_RECOMMENDED="yes"
                STREAM_COPY_NOTE="H.264 video and AAC audio: stream copy (remux) is the recommended default."
            else
                STREAM_COPY_RECOMMENDED="warn"
                STREAM_COPY_NOTE="Video is H.264 but audio is '${PA_CODEC}'. Stream copy is technically possible, but player compatibility may vary."
            fi
            ;;
        '')
            STREAM_COPY_RECOMMENDED="no"
            STREAM_COPY_NOTE="No video stream detected. This does not look like a supported media source."
            ;;
        *)
            STREAM_COPY_RECOMMENDED="warn"
            STREAM_COPY_NOTE="Video codec '${PV_CODEC}' is not H.264. HLS/player compatibility may vary. No automatic transcode will be performed."
            ;;
    esac

    case "$PV_CODEC" in
        hevc|h265|av1|vp9|vp8|mpeg2video)
            bs_warn "Video codec '${PV_CODEC}' has limited HLS/HTML5 player support."
            ;;
    esac

    if [ "$STREAM_COPY_RECOMMENDED" = "yes" ]; then
        bs_ok "Stream copy: YES (H.264 + AAC) - no transcoding required"
    else
        bs_warn "Stream copy: USE WITH CAUTION - $STREAM_COPY_NOTE"
        bs_warn "BlueStream will NOT automatically start an expensive software transcode."
        bs_warn "A 4K software transcode in particular can be extremely slow and CPU-intensive."
    fi
    return 0
}

# Bitrate used for bandwidth estimation (bits per second).
probe_pick_bitrate() {
    local b="${PF_BITRATE:-}"
    case "$b" in ''|N/A|0) b="${PV_BITRATE:-}" ;; esac
    case "$b" in ''|N/A|0) b=0 ;; esac
    printf '%s' "$b"
}

# estimate_bandwidth <source_bitrate_mbps> <viewers>
estimate_bandwidth() {
    local mbps="$1" viewers="$2"
    awk -v b="$mbps" -v v="$viewers" 'BEGIN{ printf "%.2f", b*v }'
}

bandwidth_mbps_from_bps() {
    local bps="$1"
    awk -v b="$bps" 'BEGIN{ printf "%.2f", (b/1000000) }'
}

bandwidth_show_explanation() {
    bs_step "Bandwidth model"
    printf '%s\n' \
        "Outbound bandwidth ~= source bitrate x number of viewers" \
        "when every viewer fetches HLS directly from this VPS." \
        "Example: a 4 Mbps source with 25 direct viewers needs ~100 Mbps outbound." \
        "" \
        "A small VPS cannot serve unlimited viewers." \
        "For large audiences, place a CDN or reverse proxy in front of" \
        "the public M3U8 URL so each viewer does not hit this VPS directly."
}


