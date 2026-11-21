#!/usr/bin/env bash
# Runs the REAL lib/supervise.sh loop around the Studio argv, the same way run-relay.sh does,
# without systemd/setpriv (container tests only). Env: BS_REPO, BS_RELAY, BS_PROJECT (+ BLUESTREAM_* dirs).
set -u
: "${BS_REPO:?}" "${BS_RELAY:?}" "${BS_PROJECT:?}"
# common.sh unconditionally re-assigns these directories: capture first, restore (and export) after.
RUN_DIR="${BLUESTREAM_RUN_DIR:?}"; ETC_DIR="${BLUESTREAM_ETC_DIR:?}"; MEDIA_DIR="${BLUESTREAM_MEDIA_DIR:?}"
# shellcheck disable=SC1091
. "$BS_REPO/lib/common.sh"
# shellcheck disable=SC1091
. "$BS_REPO/lib/supervise.sh"
BLUESTREAM_RUN_DIR="$RUN_DIR"; BLUESTREAM_ETC_DIR="$ETC_DIR"; BLUESTREAM_MEDIA_DIR="$MEDIA_DIR"
export BLUESTREAM_RUN_DIR BLUESTREAM_ETC_DIR BLUESTREAM_MEDIA_DIR

build() {
    local tmp
    tmp="$(mktemp)" || return 1
    if ! python3 -m studio.cli argv "$BS_RELAY" "$BS_PROJECT" > "$tmp"; then rm -f "$tmp"; return 1; fi
    FFMPEG_ARGS=()
    mapfile -d '' FFMPEG_ARGS < "$tmp"
    rm -f "$tmp"
    export BS_ALLOW_ENCODE=1
    [ "${#FFMPEG_ARGS[@]}" -ge 12 ]
}

python3 -m studio.cli widgets "$BS_RELAY" "$BS_PROJECT" &
SIDE=$!
trap 'kill "$SIDE" 2>/dev/null; wait "$SIDE" 2>/dev/null' EXIT
bs_supervise_run relay "$BS_RELAY" 0 2 build 1
