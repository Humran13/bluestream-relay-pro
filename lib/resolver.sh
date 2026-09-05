#!/usr/bin/env bash
# BlueStream Relay Pro - public webpage media resolver (generic).
#
# A public webpage URL (a YouTube watch/live page, a Twitch channel page, or
# another supported public streaming website) is an HTML document, NOT a raw
# media stream, so FFmpeg cannot ingest it directly. This library turns a
# PUBLIC webpage URL into the CURRENT playable http(s) media URL using yt-dlp,
# fresh on every start. It generalizes the original YouTube-only resolver
# (lib/youtube.sh remains as a backward-compatible wrapper).
#
# Product language is intentionally safe: only "public webpage sources
# supported by the installed resolver (yt-dlp)" are claimed - never universal
# website support.
#
# Security (PUBLIC CONTENT ONLY):
#   - fixed executable argv array, no `eval` / `bash -c` / `sh -c`
#   - the webpage URL is passed strictly as one data argument after `--`
#   - yt-dlp's own config file is ignored (--ignore-config); NO cookies, NO
#     browser-cookie extraction, NO cache dir, NO login/session/account reuse,
#     NO credentials, NO members-only / private / DRM / geo bypass
#   - NO --exec and NO postprocessor commands (no arbitrary shell execution)
#   - bounded wall-clock timeout and bounded retries
#   - output is validated: exactly ONE usable http(s) media URL; empty,
#     multi-URL, garbage, unsupported-scheme and oversized output are rejected
#   - resolved URLs are EPHEMERAL and are NEVER written to any relay config;
#     the configured source stays the original public webpage URL and is
#     re-resolved on every start
#
# BlueStream Relay Pro 0.1.0 (foundation). See LICENSE for terms.

if [ -n "${BLUESTREAM_RESOLVER_LOADED:-}" ]; then
    return 0 2>/dev/null || exit 0
fi
BLUESTREAM_RESOLVER_LOADED=1

# Wall-clock ceiling for one resolve attempt (seconds). Bounded so a wedged
# resolver can never hang a relay start forever.
BLUESTREAM_YTDLP_TIMEOUT="${BLUESTREAM_YTDLP_TIMEOUT:-45}"

# Maximum accepted resolver output size (bytes). yt-dlp -g prints URLs only;
# anything near this bound is anomalous and is rejected fail-closed.
BLUESTREAM_RESOLVER_MAX_OUTPUT=65536

# Maximum number of output lines accepted (exactly one usable URL is required).
BLUESTREAM_RESOLVER_MAX_LINES=2

bs_have_ytdlp() {
    command -v yt-dlp >/dev/null 2>&1
}

# bs_web_resolve <public-webpage-url>
# Prints exactly ONE playable http(s) media URL on success (exit 0). On any
# failure nothing is printed and a small documented status is returned:
#   1 input URL is not an http(s) public webpage URL
#   2 yt-dlp is not installed (resolver unavailable)
#   3 yt-dlp failed or timed out
#   4 resolver output was empty, multi-URL, unsupported-scheme, or oversized
bs_web_resolve() {
    local page="$1" out first n line_count
    # The webpage must be an http(s) URL. Direct media/stream URLs are NOT
    # resolver sources: they must go straight to FFmpeg (relay_build_ffmpeg_args
    # handles rtmp/rtmps/rtsp/m3u8/http-file/local-file types directly).
    case "$page" in
        http://*|https://*) ;;
        *) return 1 ;;
    esac
    bs_valid_url "$page" || return 1
    bs_have_ytdlp || return 2

    local -a cmd=(
        yt-dlp
        --ignore-config
        --no-warnings
        --quiet
        --no-progress
        --no-playlist
        --no-cache-dir
        --no-cookies
        --no-cookies-from-browser
        --socket-timeout 15
        --retries 2
        -f "best[protocol^=m3u8]/best"
        -g
        --
        "$page"
    )

    # Bounded wall-clock execution. `timeout` (coreutils) is required on the
    # supported Ubuntu targets; if it is ever absent the resolve fails closed
    # rather than running without a bound.
    if ! command -v timeout >/dev/null 2>&1; then
        return 3
    fi
    out="$(timeout -k 5 "$BLUESTREAM_YTDLP_TIMEOUT" "${cmd[@]}" 2>/dev/null)" || return 3

    # Oversized output is rejected before any further processing.
    [ "${#out}" -le "$BLUESTREAM_RESOLVER_MAX_OUTPUT" ] || return 4

    # Exactly one non-empty output line is expected (a single playable media
    # URL). A multi-line output (e.g. a format merge that would print two URLs)
    # is rejected fail-closed rather than silently streaming video-only/audio.
    line_count="$(printf '%s\n' "$out" | grep -c . 2>/dev/null || printf '0')"
    [ "$line_count" -ge 1 ] || return 4
    [ "$line_count" -le "$BLUESTREAM_RESOLVER_MAX_LINES" ] || return 4
    [ "$line_count" = "1" ] || return 4

    first="$(printf '%s\n' "$out" | sed -n '1p')"
    # The resolved playable URL must itself be a well-formed http(s) URL of a
    # bounded size - never a local path, rtmp URL, or shell-hostile value.
    case "$first" in
        http://*|https://*) ;;
        *) return 4 ;;
    esac
    bs_valid_url "$first" || return 4

    printf '%s' "$first"
    return 0
}
