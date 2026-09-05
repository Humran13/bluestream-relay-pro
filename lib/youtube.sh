#!/usr/bin/env bash
# BlueStream Relay Pro - public YouTube Live source resolver (GUI-7A).
#
# Backward-compatibility wrapper for the ORIGINAL YouTube-only resolver API.
# All resolution logic now lives in the generic public-webpage resolver
# lib/resolver.sh (bs_web_resolve); this file keeps the historical
# bs_youtube_resolve() name/status codes working for existing callers and
# keeps TYPE=youtube relays fully compatible without any config migration.
#
# A YouTube watch/live PAGE URL (https://www.youtube.com/watch?v=...,
# https://youtu.be/..., https://www.youtube.com/live/...) is an HTML
# document, NOT a raw media stream, so FFmpeg cannot ingest it directly -
# that is exactly why pasting one as a plain source URL fails. The generic
# resolver turns a PUBLIC webpage URL into the CURRENT playable HLS media URL
# using yt-dlp, fresh on every start.
#
# Security (identical rules, enforced in lib/resolver.sh):
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

# Load the generic resolver that implements the shared yt-dlp command line.
# shellcheck source=lib/resolver.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/resolver.sh" 2>/dev/null || true

bs_have_ytdlp() {
    command -v yt-dlp >/dev/null 2>&1
}

# bs_youtube_resolve <public-youtube-page-url>
# Prints exactly ONE playable media URL on success (exit 0). On any failure
# nothing is printed and a small documented status is returned (kept identical
# to the original resolver for compatibility):
#   1 not a recognized public YouTube URL
#   2 yt-dlp is not installed
#   3 yt-dlp failed or timed out
#   4 output was empty, multi-URL, or not a valid http(s) URL
bs_youtube_resolve() {
    local page="$1"
    bs_is_youtube_url "$page" || return 1
    bs_web_resolve "$page" || return $?
}
