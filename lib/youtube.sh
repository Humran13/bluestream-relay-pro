#!/usr/bin/env bash
# BlueStream Relay Pro - public YouTube Live source resolver (GUI-7A).
#
# A YouTube watch/live PAGE URL (https://www.youtube.com/watch?v=...,
# https://youtu.be/..., https://www.youtube.com/live/...) is an HTML
# document, NOT a raw media stream, so FFmpeg cannot ingest it directly -
# that is exactly why pasting one as a plain source URL fails. This resolver
# turns a PUBLIC YouTube page URL into the CURRENT playable HLS media URL
# using yt-dlp, fresh on every start.
#
# Security:
#   - fixed executable argv, shell=False semantics (a bash array, never
#     `eval` / `bash -c` / a shell string); the page URL is passed as a
#     single positional argument after `--`, strictly as data
#   - yt-dlp's own config file is ignored (--ignore-config); NO cookies, NO
#     browser cookies, NO cache dir, NO --exec / postprocessors
#   - PUBLIC content only: no login, no session/account reuse, no
#     geo/access-control bypass, no DRM bypass, no credential scraping
#   - hard wall-clock timeout; fails closed on any error, empty output, or
#     output that is not a single valid http(s) URL
#   - resolved URLs are EPHEMERAL and are NEVER written to the relay config;
#     the configured source stays the original YouTube page URL and is
#     re-resolved on every start
#
# BlueStream Relay Pro 0.1.0 (foundation). See LICENSE for terms.

if [ -n "${BLUESTREAM_YOUTUBE_LOADED:-}" ]; then
    return 0 2>/dev/null || exit 0
fi
BLUESTREAM_YOUTUBE_LOADED=1

# Wall-clock ceiling for one resolve attempt (seconds).
BLUESTREAM_YTDLP_TIMEOUT="${BLUESTREAM_YTDLP_TIMEOUT:-45}"

bs_have_ytdlp() {
    command -v yt-dlp >/dev/null 2>&1
}

# bs_youtube_resolve <public-youtube-page-url>
# Prints exactly ONE playable media URL on success (exit 0). On any failure
# nothing is printed and a small documented status is returned:
#   1 not a recognized public YouTube URL
#   2 yt-dlp is not installed
#   3 yt-dlp failed or timed out
#   4 output was empty, multi-URL, or not a valid http(s) URL
bs_youtube_resolve() {
    local page="$1" out first n
    bs_is_youtube_url "$page" || return 1
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

    if command -v timeout >/dev/null 2>&1; then
        out="$(timeout -k 5 "$BLUESTREAM_YTDLP_TIMEOUT" "${cmd[@]}" 2>/dev/null)" || return 3
    else
        out="$("${cmd[@]}" 2>/dev/null)" || return 3
    fi

    # Exactly one non-empty output line is expected (a single combined HLS
    # URL). A '+'-merged format would print two URLs - reject that (fail
    # closed) rather than silently stream video-only.
    n="$(printf '%s\n' "$out" | grep -c . 2>/dev/null || printf '0')"
    [ "$n" = "1" ] || return 4

    first="$(printf '%s\n' "$out" | sed -n '1p')"
    case "$first" in
        http://*|https://*) ;;
        *) return 4 ;;
    esac
    bs_valid_url "$first" || return 4

    printf '%s' "$first"
    return 0
}
