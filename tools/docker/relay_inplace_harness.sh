#!/usr/bin/env bash
# Runs the REAL lib/relay.sh (relay_build_ffmpeg_args incl. the in-place overlay hook) inside the REAL lib/supervise.sh loop, the way
# run-relay.sh does, without systemd/setpriv (container tests only).
# Env: BS_REPO, BS_RELAY, BS_RELAY_TYPE, BS_RELAY_URL, BS_RELAY_LOOP (+ BLUESTREAM_* dirs). Send SIGUSR1 to this process to swap the picture source.
set -u
: "${BS_REPO:?}" "${BS_RELAY:?}" "${BS_RELAY_TYPE:?}" "${BS_RELAY_URL:?}"
# common.sh unconditionally re-assigns these directories: capture first, restore (and export) after.
RUN_DIR="${BLUESTREAM_RUN_DIR:?}"; ETC_DIR="${BLUESTREAM_ETC_DIR:?}"; MEDIA_DIR="${BLUESTREAM_MEDIA_DIR:?}"; STUDIO_DIR="${BLUESTREAM_STUDIO_DIR:?}"
# shellcheck disable=SC1091
. "$BS_REPO/lib/common.sh"
# shellcheck disable=SC1091
. "$BS_REPO/lib/relay.sh"
# shellcheck disable=SC1091
. "$BS_REPO/lib/supervise.sh"
BLUESTREAM_RUN_DIR="$RUN_DIR"; BLUESTREAM_ETC_DIR="$ETC_DIR"; BLUESTREAM_MEDIA_DIR="$MEDIA_DIR"; BLUESTREAM_STUDIO_DIR="$STUDIO_DIR"
export BLUESTREAM_RUN_DIR BLUESTREAM_ETC_DIR BLUESTREAM_MEDIA_DIR BLUESTREAM_STUDIO_DIR

RELAY_NAME="$BS_RELAY"; RELAY_TYPE="$BS_RELAY_TYPE"; RELAY_URL="$BS_RELAY_URL"; RELAY_LOOP="${BS_RELAY_LOOP:-yes}"
build() { relay_build_ffmpeg_args; }
trap 'relay_overlay_stop_sidecar' EXIT
bs_supervise_run relay "$RELAY_NAME" 0 2 build 1
